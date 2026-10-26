"""契约 B · 统一消息对象 UMO（任务书 2.2.2 节，逐字段对齐）。

**设计目标**（2.2.1）：文本 / 语音 / 图片三种输入，经多模态预处理层后必须输出
**结构完全同构**的对象，使对话编排层、工具层、日志层都不必为输入类型写分支。

这是「一次编排、三路输入」的架构关键，因此本文件的字段增删属于**破坏性变更**，
必须走 2.7.2 节契约变更流程。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.enums import (
    Channel,
    Direction,
    Emotion,
    InboundMsgType,
    RiskLevel,
    Sender,
    TextSource,
)


class RawMessage(BaseModel):
    """原始消息载体（未归一化）。

    Attributes:
        msg_type: 原始类型。商家 `msg_type` 为 `TEXT`/`IMAGE`/`CARD`/`GOODS`；
            语音是平台下发的扩展类型，**需在接入层映射为 `VOICE`**。
        media_url: 媒体文件 URL（语音 / 图片）。**内部地址，不得下发给用户**。
        media_format: 媒体格式，如 `amr` / `silk` / `mp3` / `jpg`。
        media_duration_ms: 语音时长（毫秒）。
        media_size_bytes: 媒体文件大小（字节）。
        extra: 平台原始字段透传（排障用，落日志前必须脱敏）。
    """

    msg_type: InboundMsgType = Field(description="原始消息类型")
    media_url: str | None = Field(default=None, description="媒体文件 URL")
    media_format: str | None = Field(default=None, description="媒体格式")
    media_duration_ms: int | None = Field(default=None, ge=0, description="语音时长 ms")
    media_size_bytes: int | None = Field(default=None, ge=0, description="文件大小 B")
    extra: dict[str, Any] = Field(default_factory=dict, description="平台原始字段")


class UmoEntities(BaseModel):
    """归一化后抽取的结构化实体（意图 / 槽位 / 单号）。

    Attributes:
        sku_id: 已确定的 SKU 编码。
        sku_hint: SKU 模糊提示（用户口语描述，如「小熊」）。
        order_no: 订单号（12 位数字）。
        tracking_no: 运单号。
        intent: 意图标签（细粒度，如 `order_logistics_query`）。
        intent_category: 意图 6 大类之一。
        slots: 槽位字典，如 `{"product_hint": "小熊", "time_hint": "上周"}`。
        image_route: 图片五路分流结果（仅图片输入）。
        product_matches: 商品图 Top-3 匹配结果（仅商品图）。
        ocr_raw: OCR 原始输出（仅订单截图 / 尺码表）。
        need_clarify: 是否需要向用户澄清。
    """

    sku_id: str | None = Field(default=None, description="确定的 SKU 编码")
    sku_hint: str | None = Field(default=None, description="SKU 模糊提示")
    order_no: str | None = Field(default=None, description="订单号")
    tracking_no: str | None = Field(default=None, description="运单号")
    intent: str | None = Field(default=None, description="细粒度意图标签")
    intent_category: str | None = Field(default=None, description="意图 6 大类")
    slots: dict[str, Any] = Field(default_factory=dict, description="槽位字典")
    image_route: str | None = Field(default=None, description="图片五路分流结果")
    product_matches: list[dict[str, Any]] = Field(
        default_factory=list, description="商品图 Top-3 匹配"
    )
    ocr_raw: dict[str, Any] = Field(default_factory=dict, description="OCR 原始输出")
    need_clarify: bool = Field(default=False, description="是否需要澄清")


class NormalizedText(BaseModel):
    """归一化文本与置信度（三路输入的统一出口）。

    | 输入 | text_source | confidence 来源 | 低置信处理 |
    | --- | --- | --- | --- |
    | 文本 | `ORIGINAL` | 恒为 1.0 | 无 |
    | 语音 | `ASR` | ASR 逐句置信度 | < 0.6 → need_clarify，回问「您是说 XXX 吗」 |
    | 商品图 | `VLM` | VLM 匹配分 | Top-3 均低于阈值 → 追问 |
    | 订单截图 | `OCR` | OCR 置信度 | < 0.95 → 引导手动输入订单号 |
    | 瑕疵图 | `VLM` | VLM 描述置信度 | **不做责任判定**，给政策 + 建议转人工 |
    | 尺码表 | `OCR` | OCR 置信度 | 结构化失败 → 引导文字描述 |
    | 表情包 | `CLASSIFIER` | 分类置信度 | 礼貌回应并引导回正题 |
    """

    text: str = Field(default="", description="归一化文本")
    text_source: TextSource = Field(
        default=TextSource.ORIGINAL, description="文本来源，区分置信度来源"
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="置信度 0-1"
    )
    language: str = Field(default="zh-CN", description="语言")
    entities: UmoEntities = Field(
        default_factory=UmoEntities, description="结构化实体"
    )


class SessionState(BaseModel):
    """多轮会话状态（字段对齐项目文档 5.2 节）。

    **存储约束**：Redis 只存本对象，TTL 30 分钟，且**不含完整手机号 / 地址 /
    身份证**（BE-03 DoD，有自动化扫描用例）。

    Attributes:
        intent_stack: 意图栈，栈顶为当前意图。
        slots: 累积槽位。
        entities: 累积实体（如 `last_order_no`）。
        turn_count: 当前会话轮次，**> 10 强制引导转人工**（成本控制）。
        unresolved_count: 连续未解决轮数，达阈值触发规则③ 转人工。
        emotion: 当前情绪。
        negative_streak: 连续负面情绪轮数，达 3 触发规则② 转人工。
        channel: 渠道。
        buyer_bound: 买家身份是否已绑定。
        owner: 会话归属，`BOT` 或 `AGENT`。
        bot_muted: 是否已静音机器人（坐席接管后为 True）。
    """

    intent_stack: list[str] = Field(default_factory=list, description="意图栈")
    slots: dict[str, Any] = Field(default_factory=dict, description="累积槽位")
    entities: dict[str, Any] = Field(default_factory=dict, description="累积实体")
    turn_count: int = Field(default=0, ge=0, description="会话轮次")
    unresolved_count: int = Field(default=0, ge=0, description="连续未解决轮数")
    emotion: Emotion = Field(default=Emotion.NEUTRAL, description="当前情绪")
    negative_streak: int = Field(default=0, ge=0, description="连续负面情绪轮数")
    channel: Channel | None = Field(default=None, description="渠道")
    buyer_bound: bool = Field(default=False, description="买家是否已绑定")
    owner: Literal["BOT", "AGENT"] = Field(default="BOT", description="会话归属")
    bot_muted: bool = Field(default=False, description="机器人是否被静音")

    def push_intent(self, intent: str, *, max_depth: int = 8) -> None:
        """压入意图（栈顶为当前意图，限制深度避免无限增长）。

        同名意图只保留一份：先移除栈中已有的同值，再压到栈顶，
        使 `current_intent` 永远指向最近一次被识别的意图。
        """
        if intent in self.intent_stack:
            self.intent_stack.remove(intent)
        self.intent_stack.append(intent)
        if len(self.intent_stack) > max_depth:
            del self.intent_stack[0]

    @property
    def current_intent(self) -> str | None:
        """当前意图（栈顶）。"""
        return self.intent_stack[-1] if self.intent_stack else None


class UmoFlags(BaseModel):
    """降级与风险标记。

    Attributes:
        low_confidence: 归一化置信度低于该路径阈值。
        is_noise: 噪声消息（表情包 / 无关内容 / 纯符号）。
        is_oversize: 超长或超大（文本超长 / 图片 > 10MB / 语音超 60s）。
        need_clarify: 需要向用户澄清。
        risk_level: 风险等级。
        degraded: 命中的降级标识（如 `MERCHANT_TIMEOUT`）。
        is_duplicate: 是否为重复消息（幂等命中）。
        blocked: 是否被合规闸拦截（出站用）。
    """

    low_confidence: bool = Field(default=False, description="低置信")
    is_noise: bool = Field(default=False, description="噪声消息")
    is_oversize: bool = Field(default=False, description="超长/超大")
    need_clarify: bool = Field(default=False, description="需要澄清")
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="风险等级")
    degraded: list[str] = Field(default_factory=list, description="命中的降级标识")
    is_duplicate: bool = Field(default=False, description="重复消息")
    blocked: bool = Field(default=False, description="被合规闸拦截")


class UnifiedMessageObject(BaseModel):
    """统一消息对象 UMO（契约 B，2.2.2 节）。

    文本 / 语音 / 图片三路输入经多模态预处理后**结构完全同构**，
    下游无需按输入类型分支。
    """

    msg_id: str = Field(description="消息唯一标识，幂等键")
    session_id: str = Field(description="会话标识，工单幂等键")
    trace_id: str = Field(description="全链路追踪 ID，贯穿一次请求所有服务")
    buyer_id: str = Field(description="买家标识（脱敏后稳定 ID）")
    channel: Channel = Field(description="渠道")
    direction: Direction = Field(
        default=Direction.INBOUND, description="消息方向"
    )
    sender: Sender = Field(default=Sender.USER, description="发送方")
    sent_at: datetime = Field(description="发送时间")
    raw: RawMessage = Field(description="原始消息")
    normalized: NormalizedText = Field(
        default_factory=NormalizedText, description="归一化文本"
    )
    session_state: SessionState = Field(
        default_factory=SessionState, description="多轮状态"
    )
    flags: UmoFlags = Field(default_factory=UmoFlags, description="降级与风险标记")

    @field_validator("msg_id", "session_id", "trace_id", "buyer_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """四个标识字段不得为空字符串。"""
        if not v or not v.strip():
            raise ValueError("标识字段不得为空")
        return v

    @property
    def text(self) -> str:
        """归一化文本快捷访问（编排层高频使用）。"""
        return self.normalized.text

    @property
    def is_inbound(self) -> bool:
        """是否入站消息。"""
        return self.direction == Direction.INBOUND

    def model_dump_umo(self) -> dict[str, Any]:
        """导出为字典，时间按商家口径 `yyyy-MM-dd HH:mm:ss` 格式化。"""
        data = self.model_dump(mode="json")
        data["sent_at"] = self.sent_at.strftime("%Y-%m-%d %H:%M:%S")
        return data


__all__ = [
    "NormalizedText",
    "RawMessage",
    "SessionState",
    "UmoEntities",
    "UmoFlags",
    "UnifiedMessageObject",
]
