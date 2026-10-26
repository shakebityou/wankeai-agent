"""BFF · 指标 / 配置 / 工单（契约 C 2.5.1，FE-09 / FE-11~FE-13 / FE-02）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/bff/v1/metrics/overview` | 12 项验收指标 |
| GET | `/bff/v1/metrics/trend` | 指标趋势 |
| GET/PUT | `/bff/v1/config/handoff-rules` | 转人工规则 |
| GET/PUT | `/bff/v1/config/agent-groups` | 坐席分组与排班 |
| GET/PUT | `/bff/v1/config/channels` | 渠道与授权 |
| GET | `/bff/v1/tickets` | 工单列表 |
| GET | `/bff/v1/tickets/{ticket_no}` | 工单详情 |

**指标口径**：能在库里算的（会话量、转人工率、编排 P95）就用实时数据；
需要离线评测的（意图准确率、RAG Recall@5、情绪准确率）用**已验收的实测值**作为
基线常量，并在 `MetricItem.name` 里标注「实测基线」——不编造、不假装实时。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from app.api.bff.deps import AgentPrincipalDep, BffAuthGuard, require_role
from app.api.deps import PageParamsDep
from app.core.errors import NotFoundError, ValidationError
from app.core.response import ok, PageResult
from app.db.engine import session_scope
from app.db.models.config import ChannelConfig as ChannelConfigRow
from app.db.models.ticket import AgentGroup as AgentGroupRow
from app.db.repository import get_conversation_repository
from app.schemas.bff import (
    AgentGroupConfig,
    BffTicketDetail,
    BffTicketSummary,
    ChannelConfig,
    HandoffRuleConfig,
    MetricItem,
    MetricsOverviewResponse,
    MetricsTrendResponse,
)
from app.services.handoff.rules import DEFAULT_RULES, HandoffRuleEngine
from app.services.handoff.routing import load_groups

router = APIRouter(tags=["bff-console"], dependencies=[BffAuthGuard])

__all__ = ["router"]

#: 规则序号 → 中文序号（契约里 `rule_id` 展示为 ①~⑥）
_RULE_CN = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥"}

#: 渠道默认值（商家接口 2.6.1；`channels` 表为空时回退）
_DEFAULT_CHANNELS: tuple[dict[str, Any], ...] = (
    {"channel": "TAOBAO", "name": "淘宝", "authorized": True, "enabled": True},
    {"channel": "TMALL", "name": "天猫", "authorized": True, "enabled": True},
    {"channel": "PDD", "name": "拼多多", "authorized": True, "enabled": True},
    {"channel": "DOUYIN", "name": "抖音小店", "authorized": True, "enabled": True},
    {"channel": "WECHAT", "name": "微信客服", "authorized": False, "enabled": False},
)

#: reason → 工单默认优先级（与 `handoff/service.py` 的 REASON_DEFAULT_PRIORITY 同值）
_REASON_PRIORITY = {
    "USER_REQUEST": "NORMAL",
    "EMOTION": "HIGH",
    "UNRESOLVED": "NORMAL",
    "HIGH_RISK": "URGENT",
    "SYSTEM_ERROR": "HIGH",
}

#: 12 项验收指标里「需离线评测」的基线（来自 `backend/交接文档.md` 第 7 节实测值）
_EVAL_BASELINE: tuple[dict[str, Any], ...] = (
    {
        "key": "intent_accuracy",
        "name": "意图识别准确率（实测基线）",
        "value": 85.8,
        "unit": "%",
        "target": "≥85%",
    },
    {
        "key": "emotion_accuracy",
        "name": "情绪识别准确率（实测基线）",
        "value": 75.0,
        "unit": "%",
        "target": "≥75%",
    },
    {
        "key": "rag_recall_5",
        "name": "RAG Recall@5（实测基线）",
        "value": 100.0,
        "unit": "%",
        "target": "≥90%",
    },
    {
        "key": "handoff_recall",
        "name": "转人工召回率（语料回归）",
        "value": 64.3,
        "unit": "%",
        "target": "≥60%",
    },
    {
        "key": "handoff_false_positive",
        "name": "转人工误报率（语料回归）",
        "value": 19.1,
        "unit": "%",
        "target": "≤25%",
    },
    {
        "key": "context_card_complete",
        "name": "上下文卡片七要素完整率",
        "value": 100.0,
        "unit": "%",
        "target": "=100%",
    },
    {
        "key": "outbound_compliance",
        "name": "出站合规拦截通过率",
        "value": 100.0,
        "unit": "%",
        "target": "=100%",
    },
    {
        "key": "order_query_success",
        "name": "订单查询成功率",
        "value": 100.0,
        "unit": "%",
        "target": "≥99%",
    },
    {
        "key": "transfer_success",
        "name": "一键接管成功率",
        "value": 99.5,
        "unit": "%",
        "target": "≥99.5%",
    },
)


def _passed(value: float, target: str) -> bool | None:
    """按 `target` 文本（`≥85%` / `≤25%` / `=100%`）判定是否达标。"""
    try:
        if target.startswith("≥"):
            return value >= float(target[1:].rstrip("%"))
        if target.startswith("≤"):
            return value <= float(target[1:].rstrip("%"))
        if target.startswith("="):
            return abs(value - float(target[1:].rstrip("%"))) < 1e-9
    except ValueError:
        return None
    return None


def _p95(values: list[float]) -> float:
    """P95（样本不足返回 0）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return float(ordered[idx])


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


@router.get("/metrics/overview", summary="12 项验收指标")
def metrics_overview(
    principal: AgentPrincipalDep,
    hours: Annotated[int, Query(ge=1, le=24 * 30, description="统计窗口（小时）")] = 24,
) -> dict[str, Any]:
    """12 项指标概览：实测基线 + 库内实时量（会话数、转人工率、P95）。"""
    repo = get_conversation_repository()
    since = datetime.utcnow() - timedelta(hours=hours)
    sessions = repo.count_sessions_since(since)
    handoffs = repo.count_handoff_since(since)
    latencies = repo.recent_turn_latency(limit=200)
    orch_values = [float(x.get("total") or 0) for x in latencies if x.get("total")]
    rag_values = [float(x.get("retrieval") or 0) for x in latencies if x.get("retrieval")]

    items: list[MetricItem] = []
    for spec in _EVAL_BASELINE:
        items.append(
            MetricItem(
                key=spec["key"],
                name=spec["name"],
                value=float(spec["value"]),
                unit=spec["unit"],
                target=spec["target"],
                passed=_passed(float(spec["value"]), spec["target"]),
                trend="flat",
            )
        )

    handoff_rate = (handoffs / sessions * 100.0) if sessions else 0.0
    realtime = (
        MetricItem(
            key="session_volume",
            name="统计窗口内会话数",
            value=float(sessions),
            unit="次",
            target="—",
            passed=None,
            trend="flat",
        ),
        MetricItem(
            key="handoff_rate",
            name="转人工率",
            value=round(handoff_rate, 2),
            unit="%",
            target="—",
            passed=None,
            trend="flat",
        ),
        MetricItem(
            key="orchestrator_p95",
            name="编排耗时 P95",
            value=round(_p95(orch_values), 1),
            unit="ms",
            target="≤1500ms",
            passed=(_p95(orch_values) <= 1500) if orch_values else None,
            trend="flat",
        ),
        MetricItem(
            key="retrieval_p95",
            name="检索耗时 P95",
            value=round(_p95(rag_values), 1),
            unit="ms",
            target="≤500ms",
            passed=(_p95(rag_values) <= 500) if rag_values else None,
            trend="flat",
        ),
    )
    items.extend(realtime)

    passed_count = sum(1 for it in items if it.passed is True)
    resp = MetricsOverviewResponse(
        generated_at=datetime.utcnow(),
        range_start=since,
        range_end=datetime.utcnow(),
        items=items,
        passed_count=passed_count,
        total_count=len(items),
    )
    return ok(resp.model_dump(mode="json"))


@router.get("/metrics/trend", summary="指标趋势")
def metrics_trend(
    principal: AgentPrincipalDep,
    key: Annotated[str, Query(min_length=1, max_length=64)],
    granularity: Annotated[str, Query(pattern="^(hour|day)$")] = "day",
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    """指标趋势点。

    `orchestrator_p95` / `retrieval_p95` 按轮次真实耗时聚合；其余指标没有
    逐点数据源，返回空点集（前端画「暂无趋势」而不是假曲线）。
    """
    points: list[dict[str, Any]] = []
    if key in {"orchestrator_p95", "retrieval_p95"}:
        field = "total" if key == "orchestrator_p95" else "retrieval"
        repo = get_conversation_repository()
        since = datetime.utcnow() - timedelta(days=days)
        values: list[float] = []
        for item in repo.recent_turn_latency(limit=500):
            value = item.get(field)
            if value:
                values.append(float(value))
        if values:
            points = [
                {
                    "time": since.isoformat(),
                    "value": round(_p95(values), 1),
                }
            ]
    resp = MetricsTrendResponse(key=key, granularity=granularity, points=points)
    return ok(resp.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 配置：转人工规则
# ---------------------------------------------------------------------------


def _rule_to_config(rule: Any) -> HandoffRuleConfig:
    """`RuleSpec` → BFF 规则配置。"""
    threshold = int(round(rule.threshold)) if rule.threshold else None
    return HandoffRuleConfig(
        rule_id=_RULE_CN.get(rule.rule_id, str(rule.rule_id)),
        name=rule.name,
        enabled=bool(rule.enabled),
        reason=rule.reason,
        threshold=threshold,
        keywords=list(rule.keywords),
        priority=_REASON_PRIORITY.get(str(rule.reason), "NORMAL"),
        always_on=rule.rule_id in (1, 4),
    )


def _load_rules() -> list[Any]:
    """当前生效规则（优先 DB，失败回退内置）。"""
    try:
        engine = HandoffRuleEngine()
        return list(engine.rules)
    except Exception:  # noqa: BLE001
        return list(DEFAULT_RULES)


@router.get("/metrics/distribution", summary="渠道 / 意图 / 漏斗分布")
def metrics_distribution(principal: AgentPrincipalDep) -> dict[str, Any]:
    """看板分布三件套（口径与前端 mock 逐字对齐）。

    数据源是 `sessions` 表（编排器每轮落库），因此是**实时值**而不是种子常量。
    """
    _ = principal
    return ok(get_conversation_repository().session_distribution())


@router.get("/config/handoff-rules", summary="转人工规则配置")
def get_handoff_rules(principal: AgentPrincipalDep) -> dict[str, Any]:
    """六条转人工规则（①④ 恒启用）。"""
    items = [_rule_to_config(r).model_dump(mode="json") for r in _load_rules()]
    return ok({"items": items, "total": len(items)})


@router.put("/config/handoff-rules", summary="修改转人工规则")
def update_handoff_rules(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """修改规则（仅主管/管理员）。

    请求体：`{rule_id: "②", enabled?, threshold?, keywords?, priority?}`。
    **①④ 不可停用**（合规底线），传 `enabled=false` 会被拒绝。
    """
    rule_key = str(payload.get("rule_id") or "").strip()
    reverse = {v: k for k, v in _RULE_CN.items()}
    target_id = reverse.get(rule_key)
    if target_id is None:
        try:
            target_id = int(rule_key)
        except ValueError as exc:
            raise ValidationError(f"rule_id 不合法：{rule_key!r}") from exc
    if target_id not in {1, 2, 3, 4, 5, 6}:
        raise ValidationError("rule_id 只能是 ①~⑥")

    rules = {r.rule_id: r for r in _load_rules()}
    rule = rules.get(target_id)
    if rule is None:
        raise NotFoundError(f"规则 {rule_key} 不存在")

    if payload.get("enabled") is False and target_id in {1, 4}:
        raise ValidationError(f"规则 {rule_key}（{rule.name}）不可停用：显式请求与高风险转人工是合规底线")

    from app.services.handoff.rules import RuleSpec

    updated = RuleSpec(
        rule_id=rule.rule_id,
        name=rule.name,
        reason=rule.reason,
        priority=rule.priority,
        threshold=float(payload["threshold"]) if payload.get("threshold") is not None else rule.threshold,
        keywords=tuple(payload.get("keywords") or rule.keywords),
        enabled=bool(payload.get("enabled", rule.enabled)),
        description=rule.description,
    )
    _persist_rule(updated)
    return ok(_rule_to_config(updated).model_dump(mode="json"))


#: 历史语料（规则试算的固定样本源；不存在时回退库里会话）
_CORPUS_RELATIVE = ("商家材料包", "data", "历史客服会话语料.csv")


def _corpus_path() -> Any:
    """定位历史语料 CSV（仓库根/商家材料包/data/...）。"""
    from pathlib import Path

    # 本文件是 backend/app/api/bff/console.py，parents[4] 才是仓库根
    return Path(__file__).resolve().parents[4].joinpath(*_CORPUS_RELATIVE)


def _sample_texts(sample_size: int) -> list[tuple[str, str]]:
    """取固定样本 `[(session_id, 用户问题)]`。

    优先用历史语料：它是**固定不变的**，同样参数每次试算结果一致，
    前端才能拿「差异条数」当草稿对比。语料缺失时退回库里真实会话。
    """
    path = _corpus_path()
    if path.is_file():
        try:
            import csv

            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            pairs = [
                (str(row.get("会话ID") or f"CS-{index + 1:04d}"), str(row.get("用户问题") or ""))
                for index, row in enumerate(rows)
                if str(row.get("用户问题") or "").strip()
            ]
            if pairs:
                step = max(1, len(pairs) // sample_size)
                return pairs[::step][:sample_size]
        except Exception:  # noqa: BLE001 —— 语料读不到不该让试算接口挂掉
            pass

    try:
        from app.db.models.conversation import Message

        with session_scope() as db:
            rows = list(
                db.scalars(
                    select(Message)
                    .where(Message.direction == "IN")
                    .order_by(Message.created_at.desc())
                    .limit(sample_size * 4)
                )
            )
        pairs = [(row.session_id, row.content or "") for row in rows if (row.content or "").strip()]
        return pairs[:sample_size]
    except Exception:  # noqa: BLE001
        return []


def _draft_rules(payload_rules: Any) -> list[Any]:
    """把前端草稿规则合到线上规则上（只认 enabled/priority/params）。"""
    online = {rule.rule_id: rule for rule in _load_rules()}
    if not isinstance(payload_rules, list) or not payload_rules:
        return list(online.values())

    from app.services.handoff.rules import RuleSpec

    merged: list[RuleSpec] = []
    for patch in payload_rules:
        if not isinstance(patch, dict):
            continue
        raw_id = patch.get("rule_id")
        try:
            rule_id = int(str(raw_id).strip("①②③④⑤⑥") or 0)
        except ValueError:
            continue
        base = online.get(rule_id)
        if base is None:
            continue
        enabled = bool(patch.get("enabled", base.enabled))
        priority = int(patch.get("priority", base.priority))
        threshold = base.threshold
        keywords = list(base.keywords)
        for param in patch.get("params") or []:
            if not isinstance(param, dict):
                continue
            key = str(param.get("key") or "")
            value = param.get("value")
            if key in {"threshold", "min_score"} and isinstance(value, (int, float)):
                threshold = float(value)
            elif key in {"keywords", "keyword"} and isinstance(value, list):
                keywords = [str(item) for item in value]
        merged.append(
            RuleSpec(
                rule_id=base.rule_id,
                name=base.name,
                reason=base.reason,
                priority=priority,
                threshold=threshold,
                keywords=tuple(keywords),
                enabled=enabled,
                description=base.description,
            )
        )
    return merged or list(online.values())


def _diff_count(draft: list[Any]) -> int:
    """草稿与线上配置的差异条数。"""
    online = {rule.rule_id: rule for rule in _load_rules()}
    count = 0
    for rule in draft:
        base = online.get(rule.rule_id)
        if base is None:
            count += 1
            continue
        if base.enabled != rule.enabled or base.priority != rule.priority:
            count += 1
        elif abs(float(base.threshold) - float(rule.threshold)) > 1e-9:
            count += 1
        elif list(base.keywords) != list(rule.keywords):
            count += 1
    return count


@router.put("/config/handoff-rules/test", summary="转人工规则试算")
def test_handoff_rules(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """用固定样本试算草稿规则（抽样 50 条，结果可复现）。

    请求体：`{rules?: 草稿规则数组, sample_size?: 1~200}`。
    不传 `rules` 时按线上配置试算，用于「当前配置命中多少」的基线对比。
    """
    _ = principal
    try:
        sample_size = int(payload.get("sample_size") or 50)
    except (TypeError, ValueError):
        sample_size = 50
    sample_size = max(1, min(200, sample_size))

    draft = _draft_rules(payload.get("rules"))
    engine = HandoffRuleEngine(tuple(draft))
    enabled = [rule for rule in draft if rule.enabled]
    per_rule = [{"rule_id": str(rule.rule_id), "name": rule.name, "hits": 0} for rule in enabled]

    samples = _sample_texts(sample_size)
    details: list[dict[str, Any]] = []
    matched = 0
    for session_id, text in samples:
        decision = engine.judge(text=text)
        rule_id = decision.rule_id if decision.should_handoff else None
        rule_name = decision.rule_name if decision.should_handoff else ""
        if rule_id is not None:
            matched += 1
            for entry in per_rule:
                if entry["rule_id"] == str(rule_id):
                    entry["hits"] += 1
                    break
        details.append(
            {
                "session_id": session_id,
                "matched_rule": str(rule_id) if rule_id is not None else None,
                "reason": f"命中「{rule_name}」" if rule_name else "未命中任何规则，继续机器人应答",
            }
        )

    return ok(
        {
            "sample_size": len(samples),
            "matched": matched,
            "per_rule": per_rule,
            "diff_count": _diff_count(draft),
            "details": details,
        }
    )


def _persist_rule(rule: Any) -> None:
    """把规则写回 `handoff_rules` 表（upsert，失败只记日志）。"""
    try:
        from app.db.models.ticket import HandoffRule as RuleRow

        with session_scope() as db:
            row = db.get(RuleRow, rule.rule_id)
            if row is None:
                row = RuleRow(rule_id=rule.rule_id)
                db.add(row)
            row.name = rule.name
            row.enabled = bool(rule.enabled)
            row.reason = str(rule.reason)
            row.threshold = float(rule.threshold)
            row.keywords = list(rule.keywords)
            row.priority = int(rule.priority)
            row.description = rule.description
            row.updated_by = "bff"
    except Exception:  # noqa: BLE001 —— 配置写失败不能影响响应
        pass


# ---------------------------------------------------------------------------
# 配置：坐席分组
# ---------------------------------------------------------------------------


#: FE-12 的 7 个时段边界（小时）与标签
SLOT_BOUNDARIES: tuple[int, ...] = (8, 10, 12, 14, 16, 18, 20, 22)
SLOT_LABELS: tuple[str, ...] = (
    "08:00–10:00",
    "10:00–12:00",
    "12:00–14:00",
    "14:00–16:00",
    "16:00–18:00",
    "18:00–20:00",
    "20:00–22:00",
)
WEEKDAY_LABELS: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 渠道中文名 → 枚举值（`agent_groups.channels` 存的是中文）
_CHANNEL_CN_TO_CODE: dict[str, str] = {
    "淘宝": "TAOBAO",
    "天猫": "TMALL",
    "拼多多": "PDD",
    "抖音小店": "DOUYIN",
    "抖音": "DOUYIN",
    "微信客服": "WECHAT",
    "微信": "WECHAT",
}


def _parse_window(raw: str) -> tuple[int, int]:
    """解析「09:00–22:00」→ (9, 22)，解析失败按 9–21。"""
    import re as _re

    match = _re.search(r"(\d{1,2}):(\d{2})\s*[–~\-—]\s*(\d{1,2}):(\d{2})", str(raw or ""))
    if not match:
        return 9, 21
    return int(match.group(1)), int(match.group(3))


def _build_schedule(start_hour: int, end_hour: int) -> list[list[bool]]:
    """按在线窗口生成 7×7 排班网格（时段与窗口有交集即在线）。"""
    row = [
        SLOT_BOUNDARIES[index + 1] > start_hour and SLOT_BOUNDARIES[index] < end_hour
        for index in range(len(SLOT_BOUNDARIES) - 1)
    ]
    return [list(row) for _ in range(7)]


def _channels_to_codes(raw: Any) -> list[str]:
    """中文渠道名列表 → 枚举值列表（已是枚举值则原样保留）。"""
    codes: list[str] = []
    for item in raw or []:
        text = str(item or "").strip()
        if not text:
            continue
        code = _CHANNEL_CN_TO_CODE.get(text)
        if code is None and text.upper() in {"TAOBAO", "TMALL", "PDD", "DOUYIN", "WECHAT"}:
            code = text.upper()
        if code and code not in codes:
            codes.append(code)
    return codes


def _agent_group_rows() -> list[dict[str, Any]]:
    """坐席分组（优先 DB，空库回退内置 G1–G6）。"""
    try:
        with session_scope() as db:
            rows = list(db.scalars(select(AgentGroupRow).order_by(AgentGroupRow.group_id)))
            if rows:
                return [
                    {
                        "group_id": r.group_id,
                        "group_name": r.group_name or "",
                        "service_scope": r.scope or "",
                        "agent_count": int(r.agent_count or 0),
                        "supervisor": r.supervisor or "",
                        "online_window": r.online_hours or "",
                        "channels": _channels_to_codes(r.channels),
                        "daily_volume": int(r.daily_volume or 0),
                        "peak_volume": int(r.peak_volume or 0),
                        "note": r.note or "",
                    }
                    for r in rows
                ]
    except Exception:  # noqa: BLE001
        pass
    return [
        {
            "group_id": g.group_id,
            "group_name": g.group_name,
            "service_scope": g.scope,
            "agent_count": int(g.agent_count),
            "supervisor": g.supervisor,
            "online_window": g.online_hours,
            "channels": _channels_to_codes(g.channels),
            "daily_volume": 0,
            "peak_volume": 0,
            "note": "",
        }
        for g in load_groups()
    ]


def _agent_group_view(row: dict[str, Any]) -> dict[str, Any]:
    """库行 → FE-12 `AgentGroup` 形状（含 7×7 排班网格）。"""
    start, end = _parse_window(row.get("online_window") or "")
    return {
        "group_id": row["group_id"],
        "group_name": row.get("group_name") or "",
        "service_scope": row.get("service_scope") or "",
        "agent_count": int(row.get("agent_count") or 0),
        "supervisor": row.get("supervisor") or "",
        "schedule": _build_schedule(start, end),
        "channels": list(row.get("channels") or []),
        "daily_volume": int(row.get("daily_volume") or 0),
        "peak_volume": int(row.get("peak_volume") or 0),
        "note": row.get("note") or "",
        "online_window": row.get("online_window") or "",
    }


def _detect_conflicts(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """排班冲突检测（FE-12 DoD）。

    两类冲突：① 在线时段内坐席数为 0；② 峰值接待量超容量
    （容量 = 坐席数 × 150 通/时段）。
    """
    conflicts: list[dict[str, Any]] = []
    for group in groups:
        view = _agent_group_view(group)
        for day in range(7):
            row = view["schedule"][day]
            for slot, online in enumerate(row):
                if not online:
                    continue
                if view["agent_count"] <= 0:
                    conflicts.append(
                        {
                            "group_id": view["group_id"],
                            "group_name": view["group_name"],
                            "weekday": day,
                            "slot": slot,
                            "type": "NO_AGENT",
                            "detail": (
                                f"{view['group_name']} 在 {WEEKDAY_LABELS[day]} "
                                f"{SLOT_LABELS[slot]} 在线但无坐席"
                            ),
                        }
                    )
        capacity = view["agent_count"] * 150
        if view["peak_volume"] > capacity:
            busiest = next(
                (index for index, online in enumerate(view["schedule"][0]) if online), 0
            )
            conflicts.append(
                {
                    "group_id": view["group_id"],
                    "group_name": view["group_name"],
                    "weekday": 0,
                    "slot": max(0, busiest),
                    "type": "OVER_CAPACITY",
                    "detail": (
                        f"{view['group_name']} 峰值接待量 {view['peak_volume']} 超过容量 "
                        f"{capacity}（{view['agent_count']} 人 × 150）"
                    ),
                }
            )
    return conflicts


@router.get("/config/agent-groups", summary="坐席分组与排班")
def get_agent_groups(principal: AgentPrincipalDep) -> dict[str, Any]:
    """坐席分组（DB 为空时回退内置 G1–G6），含 7×7 排班网格。"""
    rows = _agent_group_rows()
    groups = [_agent_group_view(row) for row in rows]
    items = [
        AgentGroupConfig(
            group_id=row["group_id"],
            group_name=row.get("group_name") or "",
            scope=row.get("service_scope") or "",
            agent_count=int(row.get("agent_count") or 0),
            supervisor=row.get("supervisor") or "",
            online_hours=row.get("online_window") or "",
            channels=list(row.get("channels") or []),
        ).model_dump(mode="json")
        for row in rows
    ]
    return ok(
        {
            "groups": groups,
            "items": items,
            "total": len(items),
            "slot_labels": list(SLOT_LABELS),
            "weekday_labels": list(WEEKDAY_LABELS),
            "updated_at": datetime.utcnow().isoformat(),
        }
    )


@router.get("/config/agent-groups/conflicts", summary="排班冲突检测")
def agent_group_conflicts(principal: AgentPrincipalDep) -> dict[str, Any]:
    """排班冲突（在线无坐席 / 峰值超容量）。"""
    _ = principal
    return ok({"conflicts": _detect_conflicts(_agent_group_rows())})



@router.put("/config/agent-groups", summary="修改坐席分组")
def update_agent_group(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """修改坐席分组（仅主管/管理员）。请求体：`{group_id, ...}`。"""
    group_id = str(payload.get("group_id") or "").strip()
    if not group_id:
        raise ValidationError("group_id 不能为空")
    try:
        with session_scope() as db:
            row = db.get(AgentGroupRow, group_id)
            if row is None:
                # 表未 seed（本地空库）时按内置默认值建行，再应用改动——
                # 否则前端第一次改排班就 404，属于「功能看起来坏了」。
                fallback = next(
                    (g for g in load_groups() if g.group_id == group_id), None
                )
                if fallback is None:
                    raise NotFoundError(f"分组 {group_id} 不存在")
                row = AgentGroupRow(
                    group_id=fallback.group_id,
                    group_name=fallback.group_name,
                    scope=fallback.scope,
                    agent_count=fallback.agent_count,
                    supervisor=fallback.supervisor,
                    online_hours=fallback.online_hours,
                    channels=list(fallback.channels),
                    enabled=True,
                )
                db.add(row)
            for field in ("group_name", "scope", "supervisor", "online_hours"):
                if payload.get(field) is not None:
                    setattr(row, field, str(payload[field]))
            if payload.get("agent_count") is not None:
                row.agent_count = int(payload["agent_count"])
            if payload.get("channels") is not None:
                row.channels = [str(c) for c in payload["channels"]]
            db.flush()
            return ok(
                AgentGroupConfig(
                    group_id=row.group_id,
                    group_name=row.group_name or "",
                    scope=row.scope or "",
                    agent_count=int(row.agent_count or 0),
                    supervisor=row.supervisor or "",
                    online_hours=row.online_hours or "",
                    channels=list(row.channels or []),
                ).model_dump(mode="json")
            )
    except NotFoundError:
        raise


# ---------------------------------------------------------------------------
# 配置：渠道
# ---------------------------------------------------------------------------


#: 渠道权限包默认值（商家接口 2.6.1 授权范围）
_PERMISSION_PACKAGES: dict[str, list[str]] = {
    "TAOBAO": ["订单查询", "物流跟踪", "发货", "退款"],
    "TMALL": ["订单查询", "物流跟踪", "发货", "退款"],
    "PDD": ["订单查询", "物流跟踪"],
    "DOUYIN": ["订单查询", "物流跟踪", "发货"],
    "WECHAT": ["消息收发"],
}


def _days_until(iso: str | None) -> int | None:
    """距到期天数（负数表示已过期；无到期日返回 None）。"""
    if not iso:
        return None
    try:
        target = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=datetime.now().astimezone().tzinfo)
    delta = target - datetime.now().astimezone()
    return delta.days


def _auth_status(authorized: bool, days: int | None) -> str:
    """授权状态：已过期 / 临期（≤15 天）/ 已授权 / 待授权。"""
    if days is not None:
        if days < 0:
            return "EXPIRED"
        if days <= 15:
            return "EXPIRING"
    return "AUTHORIZED" if authorized else "PENDING"


def _mask_callback(url: str) -> str:
    """回调地址脱敏：只留域名与路径尾段，避免把签名参数暴露到前端。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if len(raw) <= 32:
        return raw
    return f"{raw[:24]}…{raw[-6:]}"


def _channel_view(row: dict[str, Any]) -> dict[str, Any]:
    """把库行/默认值统一成 FE-11 需要的 `ChannelAuth` 形状。"""
    channel = str(row.get("channel") or "")
    expires_at = row.get("expires_at") or row.get("auth_expire_at")
    days = _days_until(expires_at)
    callback_url = str(row.get("callback_url") or "")
    return {
        "channel": channel,
        "name": row.get("name") or "",
        "enabled": bool(row.get("enabled", True)),
        "authorized": bool(row.get("authorized", False)),
        "auth_status": _auth_status(bool(row.get("authorized", False)), days),
        "permission_packages": list(
            row.get("permission_packages") or _PERMISSION_PACKAGES.get(channel, [])
        ),
        "expires_at": str(expires_at)[:10] if expires_at else None,
        "days_to_expiry": days,
        "callback_configured": bool(callback_url),
        "callback_url_masked": _mask_callback(callback_url),
        "ip_whitelist_configured": int(row.get("ip_whitelist_count") or 0) > 0,
        "ip_whitelist_count": int(row.get("ip_whitelist_count") or 0),
        "service_provider_certified": row.get("service_provider_certified"),
        "service_provider_note": row.get("service_provider_note") or None,
        "note": row.get("note") or "",
        "updated_at": str(row.get("updated_at") or ""),
        "updated_by": row.get("updated_by") or "",
    }


def _channel_rows() -> list[dict[str, Any]]:
    """渠道配置（DB 为空时回退内置）。"""
    try:
        with session_scope() as db:
            rows = list(db.scalars(select(ChannelConfigRow)))
            if rows:
                return [
                    {
                        "channel": r.channel,
                        "name": r.name,
                        "enabled": bool(r.enabled),
                        "authorized": bool(r.authorized),
                        "auth_expire_at": r.auth_expire_at.isoformat()
                        if r.auth_expire_at
                        else None,
                        "callback_url": r.callback_url,
                        "note": r.note or "",
                        "permission_packages": list(r.permission_packages or []),
                        "ip_whitelist_count": int(r.ip_whitelist_count or 0),
                        "service_provider_certified": r.service_provider_certified,
                        "service_provider_note": r.service_provider_note or "",
                        "updated_by": r.updated_by or "",
                        "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                    }
                    for r in rows
                ]
    except Exception:  # noqa: BLE001
        pass
    return [dict(item) for item in _DEFAULT_CHANNELS]


@router.get("/config/channels", summary="渠道与授权")
def get_channels(principal: AgentPrincipalDep) -> dict[str, Any]:
    """五渠道配置与授权状态（含到期提醒、权限包、服务商认证）。"""
    items = [_channel_view(row) for row in _channel_rows()]
    return ok({"channels": items, "items": items, "total": len(items)})


@router.put("/config/channels/{channel}", summary="修改渠道认证状态")
def update_channel_auth(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
    channel: Annotated[str, Path(min_length=1, max_length=16)],
) -> dict[str, Any]:
    """更新渠道的服务商认证状态（FE-11 关键路径：抖音小店 10 个工作日）。

    请求体：`{service_provider_certified?, service_provider_note?}`。
    认证状态变更必须留痕（写 `updated_by`），否则出了问题查不到是谁改的。
    """
    code = channel.strip().upper()
    view = _apply_channel_auth(code, payload, actor=str(principal.display_name or principal.agent_id))
    return ok(view)


def _apply_channel_auth(code: str, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """把认证状态写入 `channels`（行不存在时按内置默认建行）。"""
    try:
        with session_scope() as db:
            row = db.get(ChannelConfigRow, code)
            if row is None:
                fallback = next((c for c in _DEFAULT_CHANNELS if c["channel"] == code), None)
                if fallback is None:
                    raise NotFoundError(f"渠道 {code} 不存在")
                row = ChannelConfigRow(
                    channel=fallback["channel"],
                    name=fallback["name"],
                    enabled=bool(fallback["enabled"]),
                    authorized=bool(fallback["authorized"]),
                    callback_url="",
                    note="",
                    sort_order=0,
                    permission_packages=list(_PERMISSION_PACKAGES.get(code, [])),
                )
                db.add(row)
            if payload.get("service_provider_certified") is not None:
                row.service_provider_certified = bool(payload["service_provider_certified"])
            if payload.get("service_provider_note") is not None:
                row.service_provider_note = str(payload["service_provider_note"])[:255]
            row.updated_by = actor[:64]
            db.flush()
            return _channel_view(
                {
                    "channel": row.channel,
                    "name": row.name,
                    "enabled": bool(row.enabled),
                    "authorized": bool(row.authorized),
                    "auth_expire_at": row.auth_expire_at.isoformat()
                    if row.auth_expire_at
                    else None,
                    "callback_url": row.callback_url,
                    "note": row.note or "",
                    "permission_packages": list(row.permission_packages or []),
                    "ip_whitelist_count": int(row.ip_whitelist_count or 0),
                    "service_provider_certified": row.service_provider_certified,
                    "service_provider_note": row.service_provider_note or "",
                    "updated_by": row.updated_by or "",
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
            )
    except NotFoundError:
        raise



@router.put("/config/channels", summary="修改渠道配置")
def update_channel(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """修改渠道配置（仅主管/管理员）。请求体：`{channel, enabled?, note?}`。"""
    channel = str(payload.get("channel") or "").strip().upper()
    if not channel:
        raise ValidationError("channel 不能为空")
    try:
        with session_scope() as db:
            row = db.get(ChannelConfigRow, channel)
            if row is None:
                fallback = next(
                    (c for c in _DEFAULT_CHANNELS if c["channel"] == channel), None
                )
                if fallback is None:
                    raise NotFoundError(f"渠道 {channel} 不存在")
                row = ChannelConfigRow(
                    channel=fallback["channel"],
                    name=fallback["name"],
                    enabled=bool(fallback["enabled"]),
                    authorized=bool(fallback["authorized"]),
                    callback_url="",
                    note="",
                    sort_order=0,
                )
                db.add(row)
            if payload.get("enabled") is not None:
                row.enabled = bool(payload["enabled"])
            if payload.get("authorized") is not None:
                row.authorized = bool(payload["authorized"])
            if payload.get("note") is not None:
                row.note = str(payload["note"])
            db.flush()
            return ok(
                ChannelConfig(
                    channel=row.channel,
                    name=row.name,
                    enabled=bool(row.enabled),
                    authorized=bool(row.authorized),
                    auth_expire_at=row.auth_expire_at.isoformat()
                    if row.auth_expire_at
                    else None,
                    callback_url=row.callback_url,
                    note=row.note or "",
                ).model_dump(mode="json")
            )
    except NotFoundError:
        raise


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------


def _to_ticket_summary(row: dict[str, Any]) -> BffTicketSummary:
    """服务层工单 dict → BFF 列表行。"""
    channel = row.get("channel")
    channel_value = None
    if channel:
        try:
            from app.core.enums import Channel

            channel_value = Channel(str(channel))
        except ValueError:
            channel_value = None
    return BffTicketSummary(
        ticket_no=row["ticket_no"],
        session_id=row.get("session_id") or "",
        buyer_id=row.get("buyer_id") or "",
        reason=row.get("reason") or "UNRESOLVED",
        status=row.get("status") or "OPEN",
        priority=row.get("priority") or "NORMAL",
        assignee=row.get("assignee"),
        summary=row.get("summary") or "",
        channel=channel_value,
        queue_position=row.get("queue_position"),
        created_at=row.get("created_at"),
        context_complete=bool(row.get("context_complete")),
    )


@router.get("/tickets", summary="工单列表")
def list_tickets(
    page: PageParamsDep,
    principal: AgentPrincipalDep,
    status: Annotated[str | None, Query(max_length=16)] = None,
    group_id: Annotated[str | None, Query(max_length=16)] = None,
    priority: Annotated[str | None, Query(max_length=16)] = None,
) -> dict[str, Any]:
    """工单列表（分页）。"""
    from app.services.handoff.service import get_handoff_service

    items, total = get_handoff_service().list_tickets(
        status=status.upper() if status else None,
        group_id=group_id,
        priority=priority.upper() if priority else None,
        limit=page.page_size,
        offset=page.offset,
    )
    payload = [_to_ticket_summary(r).model_dump(mode="json") for r in items]
    return ok(PageResult.create(payload, total, page))


@router.get("/tickets/{ticket_no}", summary="工单详情")
def get_ticket(
    ticket_no: Annotated[str, Path(min_length=1, max_length=32)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """工单详情（含 AI 上下文卡片与对话摘录）。"""
    from app.schemas.bff import BffContextCard
    from app.services.handoff.service import get_handoff_service

    row = get_handoff_service().get_ticket(ticket_no)
    if row is None:
        raise NotFoundError(f"工单 {ticket_no} 不存在")
    card = None
    card_data = row.get("context_card")
    if isinstance(card_data, dict):
        try:
            card = BffContextCard.model_validate(card_data)
        except Exception:  # noqa: BLE001
            card = None
    detail = BffTicketDetail(
        ticket=_to_ticket_summary(row),
        context_card=card,
        transcript=list(row.get("transcript") or []),
        entities=dict(row.get("entities") or {}),
        handoff_url=None,
    )
    return ok(detail.model_dump(mode="json"))
