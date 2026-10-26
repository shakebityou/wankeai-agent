"""契约 B · 转人工服务内部接口（BE-13 / BE-14 / BE-15）。

| 路由 | 用途 |
| --- | --- |
| `POST /internal/v1/handoff` | 创建（或幂等返回）转人工工单 |
| `GET /internal/v1/handoff/rules` | 六条触发规则（后台展示） |
| `PATCH /internal/v1/handoff/rules/{rule_id}` | 改规则（启用/阈值/优先级/词表） |
| `POST /internal/v1/handoff/rules/evaluate` | 规则试算（不建单） |
| `GET /internal/v1/handoff/tickets` | 工单列表（坐席工作台） |
| `GET /internal/v1/handoff/tickets/{ticket_no}` | 工单详情 + 事件时间线 |

## 为什么规则改动要写审计

`handoff_rules` 直接影响「用户喊转人工会不会真的转」。谁在什么时候把规则②
的阈值从 3 改成 10，事后必须能查——因此每次 `PATCH` 都写一条
`audit_logs(action=RULE_UPDATE)`，`detail` 里存 before/after 全量快照。

## 为什么 ①④ 不能停用

规则①（显式请求人工）和规则④（高风险）停用等于让 AI 在用户明确求助或
威胁投诉时继续自顾自答，这是合规事故。`PATCH` 对这两条直接 400。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel

from app.api.deps import InternalAuthGuard
from app.core.errors import NotFoundError, ValidationError
from app.core.response import ok
from app.schemas.handoff import (
    HandoffRequest,
    HandoffResponse,
    HandoffRuleOut,
    HandoffRuleUpdate,
    RuleEvalRequest,
)
from app.services.handoff.rules import ALWAYS_ON_REASONS, RuleSpec
from app.services.handoff.service import get_handoff_service

router = APIRouter(prefix="/handoff", tags=["handoff"], dependencies=[InternalAuthGuard])


def _rule_out(rule: RuleSpec) -> dict[str, Any]:
    """`RuleSpec` → `HandoffRuleOut` 字典。"""
    return HandoffRuleOut(
        rule_id=rule.rule_id,
        name=rule.name,
        reason=rule.reason,
        priority=rule.priority,
        threshold=rule.threshold,
        keywords=list(rule.keywords),
        enabled=rule.enabled,
        description=rule.description,
        always_on=rule.reason in ALWAYS_ON_REASONS,
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# 建单
# ---------------------------------------------------------------------------


@router.post("", summary="创建转人工工单（幂等）")
def create_handoff(payload: HandoffRequest) -> dict[str, Any]:
    """创建转人工工单。

    **幂等键是 `session_id`**：同一会话重复提交返回同一张工单，
    `idempotent_hit=true`，不会在商家侧产生多张单。

    ⚠️ 响应里的 `user_message` 是给用户看的纯文字话术，**不含任何链接**
    （HC-1）；`handoff_url` 只给坐席内网工作台用。
    """
    service = get_handoff_service()
    outcome = service.handoff(
        session_id=payload.session_id,
        buyer_id=payload.buyer_id,
        channel=payload.channel,
        reason=payload.reason,
        summary=payload.summary,
        entities=payload.entities,
        tried_actions=payload.tried_actions,
        priority=payload.priority,
        transcript=payload.transcript,
        context_card=payload.context_card,
    )
    body = HandoffResponse.model_validate(outcome.to_dict())
    data = body.model_dump(mode="json")
    # 契约外的两个运维字段（前端可忽略，监控要用）
    data["context_complete"] = outcome.context_complete
    data["degraded"] = list(outcome.degraded)
    return ok(data)


# ---------------------------------------------------------------------------
# 规则管理
# ---------------------------------------------------------------------------


@router.get("/rules", summary="转人工触发规则列表")
def list_rules() -> dict[str, Any]:
    """六条触发规则（含阈值、词表、优先级、是否可关闭）。"""
    engine = get_handoff_service().engine
    return ok([_rule_out(r) for r in engine.rules])


@router.patch("/rules/{rule_id}", summary="更新转人工规则")
def update_rule(
    rule_id: Annotated[int, Path(ge=1, le=6, description="规则号 1~6")],
    payload: HandoffRuleUpdate,
) -> dict[str, Any]:
    """更新一条规则的启用状态 / 阈值 / 优先级 / 词表。

    Raises:
        NotFoundError: 规则号不存在。
        ValidationError: 试图停用规则①或④。
    """
    engine = get_handoff_service().engine
    current = next((r for r in engine.rules if r.rule_id == rule_id), None)
    if current is None:
        raise NotFoundError(f"规则 {rule_id} 不存在")
    if payload.enabled is False and current.reason in ALWAYS_ON_REASONS:
        raise ValidationError(
            f"规则 {rule_id}（{current.name}）不可停用："
            "显式请求与高风险转人工是合规底线"
        )

    before = _rule_out(current)
    updated = RuleSpec(
        rule_id=current.rule_id,
        name=current.name,
        reason=current.reason,
        priority=(
            payload.priority if payload.priority is not None else current.priority
        ),
        threshold=(
            payload.threshold if payload.threshold is not None else current.threshold
        ),
        keywords=(
            tuple(payload.keywords)
            if payload.keywords is not None
            else current.keywords
        ),
        enabled=payload.enabled if payload.enabled is not None else current.enabled,
        description=current.description,
    )
    _persist_rule(updated)
    _audit_rule_update(before, _rule_out(updated))
    # 让内存引擎立刻生效（不重启进程）
    engine._rules = tuple(  # noqa: SLF001 - 同包内受控写入
        updated if r.rule_id == rule_id else r for r in engine.rules
    )
    return ok(_rule_out(updated))


@router.post("/rules/evaluate", summary="规则试算（不建单）")
def evaluate_rules(payload: RuleEvalRequest) -> dict[str, Any]:
    """按当前规则试算「会不会转、按哪条转、为什么」，**不产生工单**。

    用于后台调阈值时的即时预览，也用于线上问题复盘。
    """
    engine = get_handoff_service().engine
    decision = engine.judge(
        text=payload.text,
        intent_label=payload.intent_label,
        intent_category=payload.intent_category,
        emotion=payload.emotion,
        negative_streak=payload.negative_streak,
        unresolved_count=payload.unresolved_count,
        turn_count=payload.turn_count,
        retrieval_decision=payload.retrieval_decision,
        top_score=payload.top_score,
        system_error=payload.system_error,
    )
    return ok(decision.to_dict())


# ---------------------------------------------------------------------------
# 工单查询（坐席工作台）
# ---------------------------------------------------------------------------


@router.get("/tickets", summary="工单列表")
def list_tickets(
    status: Annotated[str | None, Query(description="OPEN/PROCESSING/CLOSED")] = None,
    group_id: Annotated[str | None, Query(description="坐席分组 ID")] = None,
    priority: Annotated[str | None, Query(description="LOW/NORMAL/HIGH/URGENT")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """工单列表（按创建时间倒序）。"""
    service = get_handoff_service()
    items, total = service.list_tickets(
        status=status,
        group_id=group_id,
        priority=priority,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return ok(
        {"total": total, "page": page, "page_size": page_size, "items": items}
    )


@router.get("/tickets/{ticket_no}", summary="工单详情")
def get_ticket(
    ticket_no: Annotated[str, Path(min_length=1, description="工单号")],
) -> dict[str, Any]:
    """工单详情（含 `ticket_events` 时间线，坐席卡片七要素）。

    Raises:
        NotFoundError: 工单不存在。
    """
    detail = get_handoff_service().get_ticket(ticket_no)
    if detail is None:
        raise NotFoundError(f"工单 {ticket_no} 不存在")
    return ok(detail)


# ---------------------------------------------------------------------------
# 内部：规则持久化与审计
# ---------------------------------------------------------------------------


def _persist_rule(rule: RuleSpec) -> None:
    """把规则写回 `handoff_rules` 表（DB 不可用时静默跳过）。"""
    try:
        from app.db.engine import session_scope
        from app.db.models.ticket import HandoffRule

        with session_scope() as session:
            row = (
                session.query(HandoffRule)
                .filter(HandoffRule.rule_id == rule.rule_id)
                .one_or_none()
            )
            if row is None:
                session.add(
                    HandoffRule(
                        rule_id=rule.rule_id,
                        name=rule.name,
                        enabled=rule.enabled,
                        reason=rule.reason.value,
                        threshold=rule.threshold,
                        keywords=list(rule.keywords),
                        priority=rule.priority,
                        description=rule.description,
                        updated_by="api",
                    )
                )
            else:
                row.enabled = rule.enabled
                row.threshold = rule.threshold
                row.keywords = list(rule.keywords)
                row.priority = rule.priority
                row.updated_by = "api"
    except Exception:  # noqa: BLE001 - 规则改动在内存已生效，落库失败不阻断
        pass


def _audit_rule_update(before: dict[str, Any], after: dict[str, Any]) -> None:
    """写一条 `RULE_UPDATE` 审计（含 before/after 全量快照）。"""
    try:
        from app.db.engine import session_scope
        from app.db.models.conversation import AuditLog

        with session_scope() as session:
            session.add(
                AuditLog(
                    actor="api",
                    action="RULE_UPDATE",
                    target_type="handoff_rule",
                    target_id=str(after.get("rule_id")),
                    detail={"before": before, "after": after},
                )
            )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "create_handoff",
    "evaluate_rules",
    "get_ticket",
    "list_rules",
    "list_tickets",
    "router",
    "update_rule",
]
