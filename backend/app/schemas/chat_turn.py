"""对话核心契约（任务书 2.4.1）。

`POST /internal/v1/chat/turn` 的请求/响应模型。响应刻意保持
**五段平铺结构**（`reply` / `actions` / `rag_hits` / `handoff` / `latency_ms`），
与任务书示例逐字段对齐——前端与联调 Mock 都按这五段写死了字段路径，
多包一层 `data` 或少一个字段都会导致联调失败。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.umo import UnifiedMessageObject


class TurnOptions(BaseModel):
    """对话选项。"""

    stream: bool = Field(default=False, description="是否流式（本接口恒 false，流式走 SSE）")
    max_turns: int = Field(default=5, ge=1, le=50, description="本次会话最多允许的轮次")
    enable_tools: bool = Field(default=True, description="是否允许调用业务工具")


class ChatTurnRequest(BaseModel):
    """对话入口请求（2.4.1 ①）。"""

    umo: UnifiedMessageObject = Field(description="统一消息对象（2.2.2）")
    options: TurnOptions = Field(default_factory=TurnOptions, description="对话选项")


class ReplyPayload(BaseModel):
    """出站回复体。"""

    msg_type: Literal["TEXT", "IMAGE"] = Field(
        default="TEXT",
        description="出站消息类型。**本期只发 TEXT/IMAGE**，不发卡片不发链接（HC-1）",
    )
    content: str = Field(default="", description="回复正文，≤120 字（超长自动分段）")
    cards: list[dict[str, Any]] = Field(
        default_factory=list,
        description="卡片。本期恒为空列表——HC-1 禁止任何跳转卡片",
    )


class RagHitBrief(BaseModel):
    """检索命中摘要（**不返回全文**，避免把知识库内容直接泄漏给渠道）。"""

    chunk_id: str = Field(description="知识块 ID")
    score: float = Field(default=0.0, ge=0.0, description="得分")
    category: str | None = Field(default=None, description="知识类目")


class HandoffPayload(BaseModel):
    """转人工信息。"""

    reason: str = Field(description="转人工原因（5 值）")
    ticket_no: str | None = Field(default=None, description="工单号")
    queue_position: int | None = Field(default=None, description="排队位次")
    offline: bool = Field(default=False, description="是否处于非在线时段（走留资兜底）")
    # 以下两项是契约扩展（契约未禁止附加字段）：排障时最常问的一句是
    # 「这条为什么转人工」，只回 reason 只能答到「哪一类」，答不到「哪条规则」。
    # 后台工单列表与坐席卡片要按规则维度做统计，这里直接带出去省一次反查。
    rule_id: int | None = Field(default=None, description="命中的转人工规则编号（1-6）")
    rule_name: str = Field(default="", description="命中的转人工规则名称")


class LatencyPayload(BaseModel):
    """分步耗时（毫秒）。"""

    asr: int = Field(default=0, ge=0, description="语音识别耗时")
    intent: int = Field(default=0, ge=0, description="意图识别 + 指代消解耗时")
    retrieval: int = Field(default=0, ge=0, description="检索耗时")
    tool: int = Field(default=0, ge=0, description="业务工具耗时")
    llm: int = Field(default=0, ge=0, description="模型生成耗时")
    total: int = Field(default=0, ge=0, description="本轮总耗时")


class ChatTurnData(BaseModel):
    """对话入口响应 data（五段结构）。"""

    reply: ReplyPayload = Field(description="回复体")
    actions: list[str] = Field(default_factory=list, description="本轮动作（工具名/RAG/HANDOFF）")
    rag_hits: list[RagHitBrief] = Field(default_factory=list, description="检索命中")
    handoff: HandoffPayload | None = Field(default=None, description="转人工信息；未转人工为 null")
    latency_ms: LatencyPayload = Field(
        default_factory=LatencyPayload, description="分步耗时"
    )
