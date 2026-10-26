"""契约 B · 对话核心（任务书 2.4.1 节）。

    请求：POST /internal/v1/chat/turn
          { "umo": {...}, "options": { "stream": false, "max_turns": 5,
                                       "enable_tools": true } }
    响应：{ "reply": {...}, "actions": [...], "rag_hits": [...],
            "handoff": {...} | null, "latency_ms": {...} }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import OutboundMsgType, ToolName
from app.schemas.handoff import HandoffResponse
from app.schemas.umo import UnifiedMessageObject


class ChatTurnOptions(BaseModel):
    """对话轮次选项。

    Attributes:
        stream: 是否流式（内部服务一般用 false，SSE 走 BFF）。
        max_turns: 本轮允许的最大子轮数（防止单轮内工具链无限循环）。
        enable_tools: 是否允许调用业务工具。灰度或降级时可关闭。
        force_handoff: 强制转人工（坐席台测试用）。
        enable_llm: 是否允许调用 LLM。关闭时用确定性兜底生成（降级演练用）。
    """

    stream: bool = Field(default=False, description="是否流式")
    max_turns: int = Field(default=5, ge=1, le=20, description="最大子轮数")
    enable_tools: bool = Field(default=True, description="是否允许调用工具")
    force_handoff: bool = Field(default=False, description="强制转人工")
    enable_llm: bool = Field(default=True, description="是否允许调用 LLM")


class ChatTurnRequest(BaseModel):
    """对话入口请求。"""

    umo: UnifiedMessageObject = Field(description="统一消息对象")
    options: ChatTurnOptions = Field(default_factory=ChatTurnOptions)


class ReplyPayload(BaseModel):
    """回复载荷。

    Attributes:
        msg_type: 出站类型，**本期只用 `TEXT` / `IMAGE`**。
        content: 文本内容，**单条 ≤ 120 字**（提示词约束第 5 条）；
            超长由 BE-02 自动分段，本字段为分段后的第一段。
        segments: 分段结果（内容超 120 字时由发送服务填充）。
        cards: 卡片数组。**本期恒为空数组**（商家 0.8 节不接入卡片）。
        images: 图片 URL 列表（仅发送站内已有图片）。
    """

    msg_type: OutboundMsgType = Field(
        default=OutboundMsgType.TEXT, description="出站消息类型"
    )
    content: str = Field(default="", description="文本内容")
    segments: list[str] = Field(default_factory=list, description="分段后的文本")
    cards: list[dict[str, Any]] = Field(
        default_factory=list, description="卡片数组，本期恒为空"
    )
    images: list[str] = Field(default_factory=list, description="图片 URL")


class RagHitBrief(BaseModel):
    """对话响应中的 RAG 命中摘要（只给必要字段，不给全文）。"""

    chunk_id: str = Field(description="知识块 ID")
    score: float = Field(description="相关性得分")
    category: str | None = Field(default=None, description="知识分类")


class LatencyBreakdown(BaseModel):
    """各阶段耗时（毫秒），对应 2.4.1 响应示例。

    Attributes:
        asr: 语音识别耗时。
        ocr: OCR 耗时。
        vlm: 图像理解耗时。
        intent: 意图识别耗时。
        retrieval: 检索耗时。
        tools: 工具调用总耗时。
        llm: 大模型生成耗时。
        gates: 三道闸校验耗时。
        total: 端到端总耗时。
    """

    asr: int = Field(default=0, ge=0, description="ASR 耗时 ms")
    ocr: int = Field(default=0, ge=0, description="OCR 耗时 ms")
    vlm: int = Field(default=0, ge=0, description="VLM 耗时 ms")
    intent: int = Field(default=0, ge=0, description="意图耗时 ms")
    retrieval: int = Field(default=0, ge=0, description="检索耗时 ms")
    tools: int = Field(default=0, ge=0, description="工具耗时 ms")
    llm: int = Field(default=0, ge=0, description="LLM 耗时 ms")
    gates: int = Field(default=0, ge=0, description="合规闸耗时 ms")
    total: int = Field(default=0, ge=0, description="总耗时 ms")

    def finalize(self) -> "LatencyBreakdown":
        """若未显式设置 total，则按各阶段求和。"""
        if not self.total:
            self.total = (
                self.asr
                + self.ocr
                + self.vlm
                + self.intent
                + self.retrieval
                + self.tools
                + self.llm
                + self.gates
            )
        return self


class ChatTurnResponse(BaseModel):
    """对话入口响应（2.4.1 节）。

    Attributes:
        reply: 回复载荷。
        actions: 本轮执行的动作序列（工具名 / `rag_answer` / `handoff`）。
        rag_hits: RAG 命中摘要。
        handoff: 转人工结果，未转人工为 None。
        latency_ms: 各阶段耗时。
        decision: RAG 阈值分流决策（便于排障与指标统计）。
        degraded: 本轮命中的降级标识列表。
        blocked: 是否被合规闸拦截并替换为兜底话术。
    """

    reply: ReplyPayload = Field(description="回复载荷")
    actions: list[ToolName | str] = Field(
        default_factory=list, description="动作序列"
    )
    rag_hits: list[RagHitBrief] = Field(default_factory=list, description="RAG 命中")
    handoff: HandoffResponse | None = Field(default=None, description="转人工结果")
    latency_ms: LatencyBreakdown = Field(
        default_factory=LatencyBreakdown, description="各阶段耗时"
    )
    decision: str | None = Field(default=None, description="RAG 分流决策")
    degraded: list[str] = Field(default_factory=list, description="降级标识")
    blocked: bool = Field(default=False, description="是否被合规闸拦截")


__all__ = [
    "ChatTurnOptions",
    "ChatTurnRequest",
    "ChatTurnResponse",
    "LatencyBreakdown",
    "RagHitBrief",
    "ReplyPayload",
]
