"""BFF · 会话与对话（契约 C 2.5.1 / 2.5.2 / 2.5.3 / 2.5.4）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/bff/v1/sessions` | 会话列表 |
| GET | `/bff/v1/sessions/{id}` | 会话详情 + 消息历史 |
| GET | `/bff/v1/sessions/{id}/context` | AI 上下文卡片（七要素） |
| POST | `/bff/v1/sessions/{id}/takeover` | 一键接管 |
| POST | `/bff/v1/sessions/{id}/release` | 释放回机器人 |
| GET | `/bff/v1/sessions/{id}/replay` | 会话回放 |
| POST | `/bff/v1/chat/turn` | 坐席台内测试对话 |
| GET | `/bff/v1/chat/stream` | SSE 流式回复 |

**字段裁剪**：一律返回 `app.schemas.bff` 里的 BFF 模型，DB 里有但契约没有的
字段（如 `negative_streak`、`intent_stack`）不下发。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import StreamingResponse

from app.api.bff.deps import AgentPrincipalDep, BffAuthGuard
from app.api.deps import MockScenarioDep, PageParamsDep
from app.core.errors import NotFoundError, ValidationError
from app.core.response import ApiResponse, PageResult, ok
from app.db.repository import get_conversation_repository
from app.schemas.bff import (
    BffContextCard,
    BffMessage,
    BffReleaseResponse,
    BffReplayResponse,
    BffSessionDetail,
    BffSessionSummary,
    BffTakeoverRequest,
    BffTakeoverResponse,
    SseEvent,
    SseEventName,
)

router = APIRouter(tags=["bff-session"], dependencies=[BffAuthGuard])

__all__ = ["router", "build_context_card", "session_to_summary"]


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def _waiting_seconds(row: dict[str, Any]) -> int:
    """等待秒数：优先用库里记的，其次按最后消息时间推算。"""
    explicit = int(row.get("waiting_seconds") or 0)
    if explicit > 0:
        return explicit
    last = row.get("last_message_at")
    if isinstance(last, datetime) and row.get("owner") == "BOT":
        delta = (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds()
        return max(0, int(delta))
    return 0


def session_to_summary(row: dict[str, Any]) -> BffSessionSummary:
    """会话 dict → BFF 列表行。"""
    return BffSessionSummary(
        session_id=row["session_id"],
        buyer_id=row.get("buyer_id") or "unknown",
        channel=row.get("channel") or "TAOBAO",
        owner="AGENT" if row.get("owner") == "AGENT" else "BOT",
        agent_id=row.get("agent_id"),
        last_message=row.get("last_message") or "",
        last_message_at=row.get("last_message_at"),
        turn_count=int(row.get("turn_count") or 0),
        emotion=row.get("emotion") or "NEUTRAL",
        intent_category=row.get("intent_category"),
        waiting_seconds=_waiting_seconds(row),
        has_handoff=bool(row.get("has_handoff")),
        ticket_no=row.get("ticket_no"),
        tags=list(row.get("tags") or []),
    )


def message_to_bff(row: dict[str, Any]) -> BffMessage:
    """消息 dict → BFF 消息。

    前端契约（`ChatMessage`）与内部字段都要填：旧名 `msg_id/content` 供既有脚本与
    curl 示例，新名 `message_id/text` 供坐席台渲染（由 `BffMessage` 的
    `model_validator` 双向镜像保底）。这里只补那些**镜像不出来**的字段。
    """
    direction = "OUTBOUND" if row.get("direction") == "OUTBOUND" else "INBOUND"
    sender = str(row.get("sender") or "USER").upper()
    if sender not in {"USER", "BOT", "AGENT"}:
        sender = "USER"
    images: list[str] = []
    media_url = row.get("media_url")
    if media_url:
        images.append(media_url)
    content = row.get("content") or ""
    return BffMessage(
        msg_id=row["msg_id"],
        session_id=row["session_id"],
        direction=direction,  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        msg_type=row.get("msg_type") or "TEXT",
        content=content,
        images=images,
        tag=row.get("tag"),
        intent=row.get("intent"),
        created_at=row.get("created_at"),
        # 前端契约字段（镜像不出来的部分显式填）
        message_id=row["msg_id"],
        text=content,
        text_source=row.get("text_source") or "ORIGINAL",
        image_alt=row.get("tag") or None,
        trace_id=row.get("trace_id"),
        decision=row.get("decision"),
        handoff_reason=row.get("handoff_reason"),
        latency_ms=row.get("latency_ms"),
        feedback_marked=bool(row.get("feedback_marked", False)),
    )


def build_context_card(session_id: str) -> BffContextCard:
    """组装 AI 上下文卡片（七要素，2.5.2 节）。

    数据来源优先级：

    1. 最近一次转人工轮次里落库的 `context_card`（建单时已补全七要素）；
    2. 没有工单时，用会话汇总 + 最近一轮的意图/情绪现场拼一张
       （保证坐席点开任何会话都有东西看，不是空白卡片）。

    Args:
        session_id: 会话 ID。

    Returns:
        `BffContextCard`（七要素字段可能为空串/空列表，但对象一定返回）。

    Raises:
        NotFoundError: 会话不存在。
    """
    repo = get_conversation_repository()
    row = repo.get_session(session_id)
    if row is None:
        raise NotFoundError(f"会话 {session_id} 不存在")

    handoff_turn = repo.latest_handoff_turn(session_id)
    card_data: dict[str, Any] = {}
    if handoff_turn:
        for hit in handoff_turn.get("rag_hits") or []:
            if isinstance(hit, dict) and isinstance(hit.get("context_card"), dict):
                card_data = hit["context_card"]
                break

    if card_data:
        card_data.setdefault("session_id", session_id)
        card_data.setdefault("buyer_id", row.get("buyer_id") or "")
        card_data.setdefault("channel", row.get("channel"))
        card_data.setdefault("waiting_seconds", _waiting_seconds(row))
        card_data.setdefault("ticket_no", row.get("ticket_no"))
        try:
            return BffContextCard.model_validate(card_data)
        except Exception:  # noqa: BLE001 —— 历史卡片结构漂移时退化为现场拼装
            pass

    turns = repo.list_turns(session_id, limit=5)
    last = turns[-1] if turns else {}
    emotion_value = str(row.get("emotion") or "NEUTRAL")
    return BffContextCard(
        session_id=session_id,
        buyer_id=row.get("buyer_id") or "",
        channel=row.get("channel") or None,
        waiting_seconds=_waiting_seconds(row),
        summary=(last.get("user_text") or row.get("last_message") or "")[:200],
        verified=[],
        tried_actions=[
            {"action": a} for a in (last.get("actions") or []) if isinstance(a, str)
        ],
        unmet_demand="",
        suggested_action="",
        emotion={"emotion": emotion_value},
        turn_count=int(row.get("turn_count") or 0),
        attachments=[],
        ticket_no=row.get("ticket_no"),
        handoff_url=None,
    )


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@router.get("/sessions", summary="会话列表")
def list_sessions(
    page: PageParamsDep,
    principal: AgentPrincipalDep,
    scenario: MockScenarioDep = "normal",
    channel: Annotated[str | None, Query(description="渠道过滤")] = None,
    owner: Annotated[str | None, Query(description="BOT/AGENT")] = None,
    has_handoff: Annotated[bool | None, Query(description="是否已转人工")] = None,
    keyword: Annotated[str | None, Query(max_length=64, description="关键词")] = None,
    start_time: Annotated[datetime | None, Query(description="起始时间")] = None,
    end_time: Annotated[datetime | None, Query(description="结束时间")] = None,
) -> dict[str, Any]:
    """会话列表（分页，按最后消息时间倒序）。"""
    if owner and owner.upper() not in {"BOT", "AGENT"}:
        raise ValidationError("owner 只能是 BOT 或 AGENT")
    rows, total = get_conversation_repository().list_sessions(
        channel=channel.upper() if channel else None,
        owner=owner.upper() if owner else None,
        has_handoff=has_handoff,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        offset=page.offset,
        limit=page.page_size,
    )
    items = [session_to_summary(r).model_dump(mode="json") for r in rows]
    return ok(PageResult.create(items, total, page))


@router.get("/sessions/summary", summary="会话概览")
def sessions_summary(principal: AgentPrincipalDep) -> dict[str, Any]:
    """会话概览四元组：总量 / 待接待 / 人工接管 / 已转人工。

    **必须注册在 `/sessions/{session_id}` 之前**：FastAPI 按注册顺序匹配，
    否则 `summary` 会被当成会话 ID 吃进详情接口。
    """
    _ = principal
    return ok(get_conversation_repository().session_summary())


@router.get("/sessions/{session_id}", summary="会话详情")
def get_session_detail(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """会话详情 + 消息历史 + 脱敏状态。"""
    repo = get_conversation_repository()
    row = repo.get_session(session_id)
    if row is None:
        raise NotFoundError(f"会话 {session_id} 不存在")
    messages = [message_to_bff(m).model_dump(mode="json") for m in repo.list_messages(session_id)]
    state = {
        "intent_category": row.get("intent_category"),
        "emotion": row.get("emotion"),
        "turn_count": row.get("turn_count"),
        "has_handoff": row.get("has_handoff"),
        "bot_muted": row.get("bot_muted"),
        "slots": row.get("slots") or {},
    }
    detail = BffSessionDetail(
        session=session_to_summary(row), messages=[BffMessage(**m) for m in messages], state=state
    )
    return ok(detail.model_dump(mode="json"))


@router.get("/sessions/{session_id}/context", summary="AI 上下文卡片")
def get_context(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """AI 上下文卡片（七要素，FE-02 验收基线 100%）。"""
    return ok(build_context_card(session_id).model_dump(mode="json"))


@router.post("/sessions/{session_id}/takeover", summary="一键接管")
def takeover(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    payload: BffTakeoverRequest,
    principal: AgentPrincipalDep,
    scenario: MockScenarioDep = "normal",
) -> dict[str, Any]:
    """一键接管：`bot_muted=true`，机器人立即停止自动回复。

    并发冲突（场景⑦ `error_10005`）返回 `10005`，前端提示「已被 XXX 接管」。
    """
    agent_id = payload.agent_id or principal.agent_id
    if scenario in ("error_10005", "error_40901"):
        # 场景⑦ 并发接管冲突：不真的改库，只按契约吐 10005，前端演「已被 XXX 接管」
        from app.core.errors import ConflictError

        raise ConflictError("该会话已被坐席 A002 接管，请刷新后重试")
    row = get_conversation_repository().takeover(
        session_id, agent_id=agent_id, ticket_no=payload.ticket_no
    )
    resp = BffTakeoverResponse(
        session_id=session_id,
        owner="AGENT",
        agent_id=row.get("agent_id"),
        taken_over_at=row.get("taken_over_at"),
        bot_muted=True,
    )
    return ok(resp.model_dump(mode="json"))


@router.post("/sessions/{session_id}/release", summary="释放回机器人")
def release(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """释放回机器人：`bot_muted=false`。"""
    row = get_conversation_repository().release(session_id, agent_id=principal.agent_id)
    resp = BffReleaseResponse(
        session_id=session_id,
        owner="BOT",
        released_at=row.get("released_at"),
        bot_muted=False,
    )
    return ok(resp.model_dump(mode="json"))


@router.get("/sessions/{session_id}/replay", summary="会话回放")
def replay(
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """会话回放：完整消息流 + 每轮编排明细 + 全链路 trace_id。"""
    repo = get_conversation_repository()
    row = repo.get_session(session_id)
    if row is None:
        raise NotFoundError(f"会话 {session_id} 不存在")
    messages = [message_to_bff(m) for m in repo.list_messages(session_id)]
    turns = repo.list_turns(session_id)
    trace_ids = [t["trace_id"] for t in turns if t.get("trace_id")]
    resp = BffReplayResponse(
        session_id=session_id,
        messages=messages,
        turns=turns,
        trace_ids=trace_ids,
    )
    return ok(resp.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 对话（坐席台内测试）
# ---------------------------------------------------------------------------


def _run_turn(session_id: str, text: str, channel: str, buyer_id: str, scenario: str):
    """跑一轮编排，并按 Mock 场景做降级注入。

    Args:
        session_id: 会话 ID。
        text: 用户文本。
        channel: 渠道。
        buyer_id: 买家 ID。
        scenario: `X-Mock-Scenario` 头。

    Returns:
        `TurnResult`。

    Raises:
        TooManyRequestsError: 场景 `error_42901`。
        DownstreamUnavailableError: 场景 `error_50301`。
    """
    from app.core.errors import DownstreamUnavailableError, TooManyRequestsError
    from app.services.orchestrator import get_orchestrator

    if scenario == "error_42901":
        raise TooManyRequestsError("请求过于频繁，请 5 秒后重试", retry_after=5)
    if scenario == "error_50301":
        raise DownstreamUnavailableError("下游服务暂不可用，请稍后重试")

    result = get_orchestrator().handle_turn(
        session_id=session_id,
        text=text,
        channel=channel,
        buyer_id=buyer_id,
    )
    if scenario == "no_evidence":
        result.decision = dict(result.decision or {})
        result.decision["forced_no_evidence"] = True
        if not result.handoff:
            result.reply_content = "马上为您转接人工客服～"
    elif scenario == "error_50401":
        result.degraded = list(result.degraded) + ["MOCK_ORDER_TIMEOUT"]
        result.reply_content = "系统查询中，请稍候～您也可以点右侧「转人工」由坐席帮您查。"
    elif scenario == "error_40101":
        result.degraded = list(result.degraded) + ["MOCK_SIGNATURE_RETRY"]
    return result


@router.post("/chat/turn", summary="坐席台内测试对话")
def chat_turn(
    payload: dict[str, Any],
    principal: AgentPrincipalDep,
    scenario: MockScenarioDep = "normal",
) -> dict[str, Any]:
    """跑一轮对话（内部编排器），返回契约 2.4.1 五段结构。

    请求体：`{session_id, text, channel?, buyer_id?}`。
    """
    session_id = str(payload.get("session_id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not session_id:
        raise ValidationError("session_id 不能为空")
    if not text:
        raise ValidationError("text 不能为空")
    result = _run_turn(
        session_id=session_id,
        text=text,
        channel=str(payload.get("channel") or "TAOBAO").upper(),
        buyer_id=str(payload.get("buyer_id") or ""),
        scenario=scenario,
    )
    return ok(result.to_contract())


def _sse_frames(result: Any, session_id: str) -> list[str]:
    """把一轮结果拆成 8 类 SSE 事件帧。

    **没有真流式**：本地生成器一次出全文，这里按契约事件序把结果
    「重放」成流——`delta` 只用于展示，`turn.end` 才是落库口径（2.5.4 节）。
    """
    frames: list[str] = []
    reply = str(getattr(result, "reply_content", "") or "")
    intent = getattr(result, "intent", {}) or {}
    rag_hits = getattr(result, "rag_hits", []) or []
    handoff = getattr(result, "handoff", None)
    trace_id = getattr(result, "trace_id", "")

    frames.append(
        SseEvent(
            event=SseEventName.TURN_START,
            data={"session_id": session_id, "trace_id": trace_id},
        ).to_sse()
    )
    frames.append(
        SseEvent(
            event=SseEventName.INTENT,
            data={
                "category": intent.get("category"),
                "label": intent.get("label"),
                "confidence": intent.get("confidence"),
            },
        ).to_sse()
    )
    frames.append(
        SseEvent(
            event=SseEventName.RETRIEVAL,
            data={"hits": rag_hits[:5], "count": len(rag_hits)},
        ).to_sse()
    )
    # 每 12 个字一帧，避免前端渲染太碎
    step = 12
    for i in range(0, len(reply), step):
        frames.append(
            SseEvent(event=SseEventName.DELTA, data={"text": reply[i : i + step]}).to_sse()
        )
    frames.append(
        SseEvent(
            event=SseEventName.ACTION,
            data={
                "actions": getattr(result, "actions", []) or [],
                "degraded": getattr(result, "degraded", []) or [],
            },
        ).to_sse()
    )
    if handoff:
        frames.append(
            SseEvent(event=SseEventName.HANDOFF, data=dict(handoff)).to_sse()
        )
    frames.append(
        SseEvent(
            event=SseEventName.TURN_END,
            data={
                "session_id": session_id,
                "trace_id": trace_id,
                "reply": reply,
                "latency_ms": getattr(result, "latency_ms", {}) or {},
            },
        ).to_sse()
    )
    return frames


@router.get("/chat/stream", summary="SSE 流式回复")
def chat_stream(
    request: Request,
    principal: AgentPrincipalDep,
    session_id: Annotated[str, Query(min_length=1, max_length=64)],
    text: Annotated[str, Query(min_length=1, max_length=4000)],
    channel: Annotated[str, Query()] = "TAOBAO",
    buyer_id: Annotated[str, Query()] = "",
    scenario: MockScenarioDep = "normal",
) -> StreamingResponse:
    """SSE 流：`turn.start → intent → retrieval → delta* → action → [handoff] → turn.end`。

    出错时发一条 `error` 事件再结束（**不抛 HTTP 错误**，否则前端 EventSource
    拿不到任何帧，只能看到连接断开）。
    """

    def _gen():
        try:
            result = _run_turn(
                session_id=session_id,
                text=text,
                channel=channel.upper(),
                buyer_id=buyer_id,
                scenario=scenario,
            )
            for frame in _sse_frames(result, session_id):
                yield frame
        except Exception as exc:  # noqa: BLE001 —— SSE 里必须转成 error 事件
            payload = json.dumps(
                {"code": 30001, "message": str(exc), "data": None}, ensure_ascii=False
            )
            yield f"event: {SseEventName.ERROR.value}\ndata: {payload}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
