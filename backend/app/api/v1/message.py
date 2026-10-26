"""契约 A/B · 出站消息发送（BE-02）。

两个入口：

| 路由 | 归属 | 调用方 | 鉴权 |
| --- | --- | --- | --- |
| `POST /internal/v1/message/send` | 契约 B 内部服务 | 编排层、坐席台后端、重放任务 | `X-Internal-Token` |
| `GET  /internal/v1/message/failures` | 契约 B 内部服务 | 坐席台「发送失败」列表 | `X-Internal-Token` |
| `POST /internal/v1/message/failures/{id}/resend` | 契约 B 内部服务 | 坐席台「重发」 | `X-Internal-Token` |
| `GET  /internal/v1/message/stats` | 契约 B 内部服务 | 指标看板 / 验收 | `X-Internal-Token` |

**为什么不直接暴露商家的 `POST /api/v1/message/send`**：厂商路径是**出站**方向
（我方 → 商家）。若把本服务也挂在 `/api/v1/message/send` 上，同一个路径既是
「商家的接口」又是「我们的接口」，联调时极容易把两边搞混，且我们的路由会
被当成「商家回调」验签中间件误拦。因此对内统一走 `/internal/v1/*`，
契约 A 的路径只保留给「我方作为调用方」使用（见 `adapters/merchant_message.py`）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field, field_validator

from app.api.deps import InternalAuthGuard
from app.core.enums import Channel, OutboundMsgType, Sender
from app.core.errors import ValidationError
from app.core.response import ok
from app.services.message_send import (
    MAX_SEGMENTS,
    get_message_send_service,
    outbound_message_from_reply,
)

router = APIRouter(
    prefix="/message", tags=["message"], dependencies=[InternalAuthGuard]
)


class SendMessageRequest(BaseModel):
    """出站发送请求（对齐商家说明书 6.3 节，去掉本期不接入的字段）。

    Attributes:
        msg_id: 幂等键。**留空由服务端生成**；由下单条消息对同一入站消息
            重放时，调用方必须复用原 `msg_id`，否则会发出两条回复。
        session_id: 会话标识。
        buyer_id: 买家标识。
        channel: 渠道。
        content: 消息内容。超 120 字自动分段。
        msg_type: 仅 `TEXT` / `IMAGE`；`CARD` / `GOODS` 本期不接入（商家 0.8 节）。
        sender: `BOT` 或 `AGENT`。
        reply_to: 被回复的消息 ID。
        allow_order_nos: 本会话已核验的订单号白名单（这些号不脱敏）。
    """

    msg_id: str | None = Field(default=None, description="幂等键，留空自动生成")
    session_id: str = Field(min_length=1, max_length=128, description="会话标识")
    buyer_id: str = Field(min_length=1, max_length=128, description="买家标识")
    channel: Channel = Field(description="渠道")
    content: str = Field(max_length=4000, description="消息内容")
    msg_type: OutboundMsgType = Field(
        default=OutboundMsgType.TEXT, description="消息类型，仅 TEXT/IMAGE"
    )
    sender: Sender = Field(default=Sender.BOT, description="发送方")
    reply_to: str | None = Field(default=None, description="被回复消息 ID")
    allow_order_nos: list[str] = Field(
        default_factory=list, description="已核验订单号白名单"
    )

    @field_validator("sender")
    @classmethod
    def _sender_not_user(cls, v: Sender) -> Sender:
        """出站消息的发送方不能是 USER。"""
        if v == Sender.USER:
            raise ValueError("出站消息的 sender 只能是 BOT 或 AGENT")
        return v


class SegmentView(BaseModel):
    """单个分段的发送结果（给坐席台看的）。"""

    segment_id: str
    attempts: int
    ok: bool
    platform_msg_id: str = ""
    error_code: int | None = None
    error_message: str = ""
    blocked: bool
    failed_record_id: str | None = None
    content_length: int
    retries: list[dict[str, Any]] = Field(default_factory=list)


class SendMessageResponse(BaseModel):
    """出站发送响应。"""

    msg_id: str
    ok: bool
    duplicate: bool = Field(description="是否重复提交（复用首次结果，未再次发送）")
    in_progress: bool = Field(description="首次提交仍在处理中（并发提交）")
    blocked: bool = Field(description="是否被合规闸拦截并替换为兜底话术")
    overflow: bool = Field(description=f"内容是否超过 {MAX_SEGMENTS} 段（偏长提示）")
    sent_count: int
    segment_count: int
    failed_record_id: str | None = None
    segments: list[SegmentView] = Field(default_factory=list)
    latency_ms: float = 0.0
    trace_id: str = ""


@router.post(
    "/send",
    summary="出站消息发送（幂等 + 合规 + 自动分段 + 重试）",
    response_model=None,
)
def send_message(payload: SendMessageRequest) -> dict[str, Any]:
    """发送一条出站消息。

    Args:
        payload: 发送请求。

    Returns:
        统一响应包装，`data` 为 `SendMessageResponse`。

    Raises:
        ValidationError: 请求非法（如 `msg_type=CARD`）。重试无意义的错误
            必须立刻返回 400，不能当成发送失败悄悄落表。
    """
    service = get_message_send_service()
    message = outbound_message_from_reply(
        session_id=payload.session_id,
        buyer_id=payload.buyer_id,
        channel=payload.channel,
        content=payload.content,
        msg_id=payload.msg_id,
        sender=payload.sender,
        reply_to=payload.reply_to,
        allow_order_nos=set(payload.allow_order_nos),
    )
    message.msg_type = payload.msg_type
    try:
        outcome = service.send(message)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return ok(outcome.to_dict())


@router.get("/failures", summary="发送失败消息列表（坐席台可见失败标记）")
def list_failures(
    session_id: Annotated[str | None, Query(description="按会话过滤")] = None,
    unresolved_only: Annotated[bool, Query(description="只看未处理")] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """列出失败消息（含内容与错误码，供坐席核对后重发）。"""
    from app.services.message_send import FailedMessageStore

    store = get_message_send_service().failed_store or FailedMessageStore()
    items = store.list_failed(
        session_id=session_id, unresolved_only=unresolved_only, limit=limit
    )
    return ok({"total": len(items), "items": items})


@router.post(
    "/failures/{record_id}/resend",
    summary="重发一条失败消息",
    response_model=None,
)
def resend_failure(
    record_id: Annotated[str, Path(description="失败记录 ID")],
) -> dict[str, Any]:
    """重发失败消息（自动生成新 `msg_id`，成功后标记原记录已处理）。

    Raises:
        ValidationError: 记录不存在（400 而不是 404，与内部接口口径一致）。
    """
    try:
        outcome = get_message_send_service().resend(record_id)
    except LookupError as exc:
        raise ValidationError(f"失败记录不存在：{record_id}") from exc
    return ok(outcome.to_dict())


@router.get("/stats", summary="发送成功率统计（三个口径）")
def send_stats() -> dict[str, Any]:
    """发送统计：消息成功率 / 投递成功率 / 一次通过率。"""
    return ok(get_message_send_service().stats())


__all__ = [
    "SendMessageRequest",
    "SendMessageResponse",
    "router",
    "send_message",
]
