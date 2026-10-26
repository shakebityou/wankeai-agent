"""对话核心路由（BE-04，契约 2.4.1）。

`POST /internal/v1/chat/turn` —— 唯一的前端/渠道 → 机器人入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import InternalAuthGuard
from app.core.logging import get_logger
from app.core.response import ok
from app.schemas.chat_turn import (
    ChatTurnData,
    ChatTurnRequest,
    HandoffPayload,
    LatencyPayload,
    RagHitBrief,
    ReplyPayload,
)
from app.services.orchestrator import get_orchestrator

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[InternalAuthGuard])


@router.post(
    "/turn",
    summary="对话入口：处理一轮用户输入",
    response_model=dict,
)
def chat_turn(payload: ChatTurnRequest) -> dict[str, Any]:
    """处理一轮对话。

    **入站消息类型必须是已经归一化的**：语音走 BE-10/BE-11 转成文本、
    图片走 BE-11/BE-12 分流后置 `normalized.text`（`text_source` 标明来源）。
    本接口不再重复做多模态处理，避免同一个文件被下载两次。

    请求体中的 `umo.trace_id` 会透传到所有下游日志，前端可用同一个
    trace_id 拉取完整链路（DoD ④ 一键聚合）。
    """
    umo = payload.umo
    text = (umo.normalized.text or "").strip()

    if not text:
        # 归一化后为空：可能是纯图片/纯语音尚未处理完，或者噪声消息。
        # 不能返回空回复——渠道侧会认为发送失败并重推。
        logger.info(
            "空归一化文本，返回澄清提示 session=%s msg_id=%s",
            umo.session_id,
            umo.msg_id,
        )
        data = ChatTurnData(
            reply=ReplyPayload(
                content="我这边没收到文字内容，方便打字描述一下您的需求吗？"
            ),
            actions=[],
            rag_hits=[],
            handoff=None,
            latency_ms=LatencyPayload(),
        )
        return ok(data.model_dump(mode="json"))

    result = get_orchestrator().handle_turn(
        session_id=umo.session_id,
        text=text,
        channel=umo.channel.value,
        buyer_id=umo.buyer_id,
        msg_type=umo.raw.msg_type.value,
        enable_tools=payload.options.enable_tools,
    )

    data = ChatTurnData(
        reply=ReplyPayload(
            msg_type=result.reply_msg_type,
            content=result.reply_content,
            cards=[],  # HC-1：永不返回跳转卡片
        ),
        actions=list(result.actions),
        rag_hits=[RagHitBrief(**h) for h in result.rag_hits if "chunk_id" in h],
        handoff=HandoffPayload(**result.handoff) if result.handoff else None,
        latency_ms=LatencyPayload(**{
            k: v for k, v in result.latency_ms.items() if k in LatencyPayload.model_fields
        }),
    )

    body = data.model_dump(mode="json")
    # 排障与坐席上下文卡片需要的附加信息，契约未禁止扩展
    body["intent"] = result.intent
    body["emotion"] = result.emotion
    body["decision"] = result.decision
    body["degraded"] = list(result.degraded)
    body["trace_id"] = result.trace_id
    return ok(body)


@router.get("/health", summary="对话核心健康检查")
def chat_health() -> dict[str, Any]:
    """健康检查：确认编排器可实例化且依赖可解析。"""
    orch = get_orchestrator()
    return ok(
        {
            "orchestrator": "ready",
            "has_rag": orch.answer_service is not None,
            "has_tools": orch.tool_registry is not None,
            "has_handoff": orch.handoff_service is not None,
        }
    )
