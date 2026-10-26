"""契约 C · BFF 契约（任务书 2.5 节，前端 ↔ Agent 后端）。

**前端只调用 BFF**，不直接访问内部服务、不直接调用商家接口。BFF 负责：
鉴权（坐席登录态）、权限裁剪、字段裁剪（前端不需要的字段不下发）。

| 模块 | 方法 | 路径 | 用途 | 对应任务 |
| --- | --- | --- | --- | --- |
| 对话 | POST | `/bff/v1/chat/turn` | 坐席台内测试对话 | FE-01 |
| | GET | `/bff/v1/chat/stream` | SSE 流式回复 | FE-01 |
| 会话 | GET | `/bff/v1/sessions` | 会话列表 | FE-01 |
| | GET | `/bff/v1/sessions/{id}` | 会话详情 + 消息历史 | FE-01 |
| | GET | `/bff/v1/sessions/{id}/context` | **AI 上下文卡片** | FE-02 |
| | POST | `/bff/v1/sessions/{id}/takeover` | **一键接管** | FE-03 |
| | POST | `/bff/v1/sessions/{id}/release` | 释放回机器人 | FE-03 |
| | GET | `/bff/v1/sessions/{id}/replay` | 会话回放 | FE-10 |
| 知识库 | — | `/bff/v1/kb/*` | 知识 CRUD / 导入 / 审核 / 检索 / 纠错 | FE-05~FE-08 |
| 指标 | GET | `/bff/v1/metrics/overview` | 12 项验收指标 | FE-09 |
| 配置 | GET/PUT | `/bff/v1/config/*` | 转人工规则 / 坐席分组 / 渠道 | FE-11~FE-13 |
| 工单 | GET | `/bff/v1/tickets` | 工单列表 / 详情 | FE-02 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.enums import (
    Channel,
    Emotion,
    HandoffReason,
    Priority,
    ReviewStatus,
    StrEnum,
    TicketStatus,
)

# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


class BffSessionSummary(BaseModel):
    """会话列表行（字段裁剪后，只给坐席需要的）。"""

    session_id: str = Field(description="会话 ID")
    buyer_id: str = Field(description="买家标识（脱敏稳定 ID）")
    channel: Channel = Field(description="渠道")
    owner: Literal["BOT", "AGENT"] = Field(description="当前归属")
    agent_id: str | None = Field(default=None, description="接管坐席")
    last_message: str = Field(default="", description="最后一条消息摘要")
    last_message_at: datetime | None = Field(default=None, description="最后消息时间")
    turn_count: int = Field(default=0, ge=0, description="会话轮次")
    emotion: Emotion = Field(default=Emotion.NEUTRAL, description="当前情绪")
    intent_category: str | None = Field(default=None, description="意图 6 大类")
    waiting_seconds: int = Field(default=0, ge=0, description="等待时长（秒）")
    has_handoff: bool = Field(default=False, description="是否已转人工")
    ticket_no: str | None = Field(default=None, description="关联工单号")
    tags: list[str] = Field(default_factory=list, description="标签（高风险/瑕疵图等）")


class BffMessage(BaseModel):
    """会话消息（消息流渲染用）。

    **字段是前端 `ChatMessage`（`frontend/src/types/contract.ts`）的超集**：
    除旧有的 `msg_id/content/direction/...` 外，还按前端契约同时给出
    `message_id/text/text_source/image_ref/decision/handoff_reason/...`。

    为什么两套字段并存而不直接改名：`msg_id/content` 已被
    `scripts/check_bff.py`、`tests/test_bff_api.py`、`tests/test_bff_extra.py`
    和 BFF 运维手册的 curl 示例引用，直接改名会让这些既有断言全红；
    而前端契约字段是**坐席台真正渲染所用的**。用 `model_validator` 做双向镜像，
    两边永远同步，不靠调用方记得填两遍。
    """

    # —— 内部/旧字段（保留，勿删） ——
    msg_id: str = Field(description="消息 ID")
    session_id: str = Field(description="会话 ID")
    direction: Literal["INBOUND", "OUTBOUND"] = Field(description="方向")
    sender: Literal["USER", "BOT", "AGENT"] = Field(description="发送方")
    msg_type: str = Field(default="TEXT", description="消息类型")
    content: str = Field(default="", description="内容（已脱敏）")
    images: list[str] = Field(default_factory=list, description="图片 URL")
    tag: str | None = Field(default=None, description="图片分流标签（瑕疵图等）")
    intent: str | None = Field(default=None, description="该轮识别意图")
    created_at: datetime | None = Field(default=None, description="时间")

    # —— 前端契约字段（`ChatMessage`，坐席台渲染用） ——
    message_id: str = Field(default="", description="消息 ID（前端契约名，与 msg_id 同步）")
    text: str = Field(default="", description="文本内容（前端契约名，与 content 同步）")
    text_source: str = Field(default="ORIGINAL", description="文本来源 ORIGINAL/ASR/OCR/VLM")
    image_ref: str | None = Field(default=None, description="图片同源相对标识（HC-1 禁止外链）")
    image_alt: str | None = Field(default=None, description="图片替代文本")
    trace_id: str | None = Field(default=None, description="所属轮次 trace_id")
    chunk_ids: list[str] = Field(default_factory=list, description="命中的知识条目 ID")
    decision: str | None = Field(default=None, description="无依据决策口径")
    handoff_reason: str | None = Field(default=None, description="转人工原因（转人工卡片用）")
    latency_ms: int | None = Field(default=None, description="该条端到端耗时（毫秒）")
    feedback_marked: bool = Field(default=False, description="是否已被坐席标记「这条答错了」")

    @model_validator(mode="after")
    def _mirror_contract_fields(self) -> "BffMessage":
        """两套字段双向镜像，保证前端拿到的永远不是空串。"""
        if not self.message_id:
            self.message_id = self.msg_id
        if not self.msg_id:
            self.msg_id = self.message_id
        if not self.text:
            self.text = self.content
        if not self.content:
            self.content = self.text
        if self.image_ref is None and self.images:
            self.image_ref = self.images[0]
        if not self.images and self.image_ref:
            self.images = [self.image_ref]
        if not self.text_source:
            self.text_source = "ORIGINAL"
        return self


class BffSessionDetail(BaseModel):
    """会话详情。"""

    session: BffSessionSummary = Field(description="会话概要")
    messages: list[BffMessage] = Field(default_factory=list, description="消息历史")
    state: dict[str, Any] = Field(
        default_factory=dict, description="会话状态（脱敏后，不含完整手机号/地址）"
    )


class BffContextCard(BaseModel):
    """AI 上下文卡片（2.5.2 节，FE-02 核心，**验收基线 100%**）。

    七要素完整性由 `TicketContextCard.missing_fields()` 判定。
    `handoff_url` **仅坐席内网使用**，不得出现在用户可见渠道。
    """

    session_id: str = Field(description="会话 ID")
    buyer_id: str = Field(default="", description="买家标识")
    channel: Channel | None = Field(default=None, description="渠道")
    waiting_seconds: int = Field(default=0, ge=0, description="等待秒数")
    summary: str = Field(default="", description="问题摘要")
    verified: list[dict[str, Any]] = Field(
        default_factory=list, description="已核实事实"
    )
    tried_actions: list[dict[str, Any]] = Field(
        default_factory=list, description="已尝试动作"
    )
    unmet_demand: str = Field(default="", description="未满足诉求")
    suggested_action: str = Field(default="", description="建议动作")
    emotion: dict[str, str] = Field(default_factory=dict, description="情绪")
    turn_count: int = Field(default=0, ge=0, description="轮次")
    attachments: list[dict[str, Any]] = Field(default_factory=list, description="附件")
    ticket_no: str | None = Field(default=None, description="工单号")
    handoff_url: str | None = Field(default=None, description="工单地址（仅坐席）")


class BffTakeoverRequest(BaseModel):
    """一键接管请求（2.5.3 节）。"""

    agent_id: str = Field(min_length=1, description="坐席 ID")
    ticket_no: str | None = Field(default=None, description="关联工单号")


class BffTakeoverResponse(BaseModel):
    """一键接管响应。

    **约束**：接管后 `bot_muted=true`，机器人**必须立即停止自动回复**
    （防止人机抢话）；释放后恢复。转接成功率 ≥ 99.5% 以本接口成功日志为准。
    """

    session_id: str = Field(description="会话 ID")
    owner: Literal["BOT", "AGENT"] = Field(description="接管后归属")
    agent_id: str | None = Field(default=None, description="接管坐席")
    taken_over_at: datetime | None = Field(default=None, description="接管时间")
    bot_muted: bool = Field(default=True, description="机器人是否已静音")


class BffReleaseResponse(BaseModel):
    """释放回机器人响应。"""

    session_id: str = Field(description="会话 ID")
    owner: Literal["BOT", "AGENT"] = Field(description="释放后归属")
    released_at: datetime | None = Field(default=None, description="释放时间")
    bot_muted: bool = Field(default=False, description="机器人是否恢复")


class BffReplayResponse(BaseModel):
    """会话回放（FE-10）。含全链路日志摘要。"""

    session_id: str = Field(description="会话 ID")
    messages: list[BffMessage] = Field(default_factory=list, description="完整消息流")
    turns: list[dict[str, Any]] = Field(
        default_factory=list, description="每轮编排明细（决策/检索/工具/耗时）"
    )
    trace_ids: list[str] = Field(default_factory=list, description="涉及的全链路 ID")


# ---------------------------------------------------------------------------
# SSE 流式事件（2.5.4 节）
# ---------------------------------------------------------------------------


class SseEventName(StrEnum):
    """SSE 事件名（**唯一口径**，前端按此渲染）。

    ⚠️ `delta` 事件只用于**展示**，最终以 `turn.end` 的完整 `reply` 为准落库
    （避免流式拼接错误导致日志与展示不一致）。
    """

    TURN_START = "turn.start"
    INTENT = "intent"
    RETRIEVAL = "retrieval"
    DELTA = "delta"
    ACTION = "action"
    TURN_END = "turn.end"
    HANDOFF = "handoff"
    ERROR = "error"


class SseEvent(BaseModel):
    """SSE 事件载荷。"""

    event: SseEventName = Field(description="事件名")
    data: dict[str, Any] = Field(default_factory=dict, description="事件载荷")

    def to_sse(self) -> str:
        """序列化为 SSE 文本帧。"""
        import json

        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.event.value}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# 知识库（BFF 侧视图）
# ---------------------------------------------------------------------------


class BffKbChunk(BaseModel):
    """知识块（BFF 视图，字段裁剪）。"""

    chunk_id: str = Field(description="知识块 ID")
    title: str | None = Field(default=None, description="标题")
    content: str = Field(default="", description="正文")
    category: str = Field(default="未分类", description="知识分类")
    knowledge_type: str = Field(default="DOC", description="知识类型")
    sku_ids: list[str] = Field(default_factory=list, description="关联 SKU")
    authority: int = Field(default=2, description="权威等级")
    version: int = Field(default=1, description="版本号")
    review_status: ReviewStatus = Field(
        default=ReviewStatus.APPROVED, description="审核状态"
    )
    enabled: bool = Field(default=True, description="是否启用")
    effective_from: str | None = Field(default=None, description="生效日期")
    effective_to: str | None = Field(default=None, description="失效日期")
    updated_by: str = Field(default="system", description="最后修改人")
    updated_at: datetime | None = Field(default=None, description="更新时间")
    source: str = Field(default="", description="来源（可追溯）")


class BffKbChunkCreate(BaseModel):
    """新增知识块（BFF）。"""

    content: str = Field(min_length=1, description="正文")
    title: str | None = Field(default=None, description="标题")
    category: str = Field(default="未分类", description="知识分类")
    knowledge_type: str = Field(default="FAQ", description="知识类型")
    sku_ids: list[str] = Field(default_factory=list, description="关联 SKU")
    authority: int = Field(default=2, ge=1, le=5, description="权威等级")
    effective_from: str | None = Field(default=None, description="生效日期")
    effective_to: str | None = Field(default=None, description="失效日期")
    created_by: str = Field(default="operator", description="创建人")
    auto_split: bool = Field(default=True, description="是否自动切分")


class BffKbSearchRequest(BaseModel):
    """坐席侧知识检索请求（FE-04）。"""

    query: str = Field(min_length=1, description="查询词")
    category: str | None = Field(default=None, description="分类过滤")
    knowledge_type: str | None = Field(default=None, description="类型过滤")
    top_k: int = Field(default=10, ge=1, le=50, description="返回条数")


class BffKbSearchResponse(BaseModel):
    """坐席侧知识检索响应。"""

    total: int = Field(default=0, ge=0, description="命中数")
    top_score: float = Field(default=0.0, description="最高分")
    decision: str = Field(default="NO_EVIDENCE", description="阈值分流决策")
    items: list[dict[str, Any]] = Field(default_factory=list, description="命中列表")


class BffKbFeedbackRequest(BaseModel):
    """纠错上报（FE-08「这条答错了」）。

    **两套字段并存**：`message_id/chunk_ids/error_type/comment` 是前端契约
    `FeedbackCreateRequest` 的坐席视角字段（坐席只关心「哪条消息、什么错、补一句」）；
    `session_id/chunk_id/answer_text/reason/expected_answer` 是内部运营口径，
    供 `/internal/v1` 侧与历史数据兼容。两套都给时以坐席字段为准。
    """

    session_id: str | None = Field(default=None, description="关联会话")
    chunk_id: str | None = Field(default=None, description="关联知识块")
    answer_text: str = Field(default="", description="被判定有误的回复原文")
    reason: str = Field(default="内容错误", description="纠错原因")
    expected_answer: str | None = Field(default=None, description="期望答案")
    reported_by: str = Field(default="operator", description="上报人")

    # ── 前端契约 `FeedbackCreateRequest` 字段 ──────────────────────
    message_id: str = Field(default="", description="被质疑的消息 ID（幂等键）")
    chunk_ids: list[str] = Field(
        default_factory=list, description="关联的多个知识块"
    )
    error_type: str = Field(default="OTHER", description="坐席视角的错误类型")
    comment: str = Field(default="", description="坐席补充说明")


# ---------------------------------------------------------------------------
# 工单（BFF 视图）
# ---------------------------------------------------------------------------


class BffTicketSummary(BaseModel):
    """工单列表行。"""

    ticket_no: str = Field(description="工单号")
    session_id: str = Field(description="会话 ID")
    buyer_id: str = Field(default="", description="买家标识")
    reason: HandoffReason = Field(description="转人工原因")
    status: TicketStatus = Field(description="工单状态")
    priority: Priority = Field(description="优先级")
    assignee: str | None = Field(default=None, description="受理人")
    summary: str = Field(default="", description="问题摘要")
    channel: Channel | None = Field(default=None, description="渠道")
    queue_position: int | None = Field(default=None, description="排队位置")
    created_at: datetime | None = Field(default=None, description="创建时间")
    context_complete: bool = Field(
        default=False, description="上下文七要素是否齐全"
    )


class BffTicketDetail(BaseModel):
    """工单详情（含 AI 上下文卡片）。

    ⚠️ `handoff_url` 供坐席工作台内部「打开工单详情」使用（内网系统，
    非用户可见）；**不得经任何用户可见渠道下发**（2.4.5 节合规约束）。
    """

    ticket: BffTicketSummary = Field(description="工单概要")
    context_card: BffContextCard | None = Field(
        default=None, description="AI 上下文卡片"
    )
    transcript: list[dict[str, str]] = Field(
        default_factory=list, description="对话摘录（已脱敏）"
    )
    entities: dict[str, Any] = Field(default_factory=dict, description="结构化实体")
    handoff_url: str | None = Field(default=None, description="工单地址（仅坐席）")


# ---------------------------------------------------------------------------
# 配置（FE-11 ~ FE-13）
# ---------------------------------------------------------------------------


class HandoffRuleConfig(BaseModel):
    """转人工规则配置（FE-13）。

    对应项目文档 5.5 节 6 条规则；**漏触发为 P0 缺陷**，因此规则阈值
    全部可配但不可为 0（除显式关闭）。
    """

    rule_id: str = Field(description="规则编号 ①~⑥")
    name: str = Field(description="规则名")
    enabled: bool = Field(default=True, description="是否启用")
    reason: HandoffReason = Field(description="对应 reason 枚举")
    threshold: int | None = Field(
        default=None, description="阈值（如连续未解决轮数）"
    )
    keywords: list[str] = Field(default_factory=list, description="关键词（规则①④）")
    priority: Priority = Field(default=Priority.NORMAL, description="工单优先级")
    always_on: bool = Field(
        default=False,
        description="是否恒启用（①显式请求 / ④高风险不可停用，前端置灰开关）",
    )


class AgentGroupConfig(BaseModel):
    """坐席分组与排班（FE-12），来自商家《客服坐席分组表.csv》。"""

    group_id: str = Field(description="分组编号 G1/G2…")
    group_name: str = Field(description="分组名称")
    scope: str = Field(default="", description="负责范围")
    agent_count: int = Field(default=0, ge=0, description="坐席人数")
    supervisor: str = Field(default="", description="主管")
    online_hours: str = Field(default="", description="在线时段")
    channels: list[str] = Field(default_factory=list, description="服务渠道")
    daily_volume: int = Field(default=0, ge=0, description="日均接待量")
    peak_volume: int = Field(default=0, ge=0, description="峰值接待量")


class ChannelConfig(BaseModel):
    """渠道与授权（FE-11）。"""

    channel: Channel = Field(description="渠道")
    name: str = Field(description="渠道中文名")
    enabled: bool = Field(default=True, description="是否启用")
    authorized: bool = Field(default=False, description="授权是否有效")
    auth_expire_at: str | None = Field(default=None, description="授权到期日")
    callback_url: str | None = Field(default=None, description="回调地址")
    note: str = Field(default="", description="备注")


# ---------------------------------------------------------------------------
# 指标（FE-09，12 项验收指标）
# ---------------------------------------------------------------------------


class MetricItem(BaseModel):
    """单项指标。"""

    key: str = Field(description="指标键")
    name: str = Field(description="指标名")
    value: float = Field(default=0.0, description="当前值")
    unit: str = Field(default="", description="单位（%/ms/次）")
    target: str = Field(default="", description="验收目标")
    passed: bool | None = Field(default=None, description="是否达标")
    trend: Literal["up", "down", "flat"] = Field(default="flat", description="趋势")


class MetricsOverviewResponse(BaseModel):
    """12 项指标概览（FE-09）。"""

    generated_at: datetime | None = Field(default=None, description="生成时间")
    range_start: datetime | None = Field(default=None, description="统计起点")
    range_end: datetime | None = Field(default=None, description="统计终点")
    items: list[MetricItem] = Field(default_factory=list, description="指标列表")
    passed_count: int = Field(default=0, ge=0, description="达标项数")
    total_count: int = Field(default=0, ge=0, description="总项数")


class MetricsTrendResponse(BaseModel):
    """指标趋势（FE-09）。"""

    key: str = Field(description="指标键")
    granularity: Literal["hour", "day"] = Field(default="day", description="粒度")
    points: list[dict[str, Any]] = Field(
        default_factory=list, description="趋势点 [{time, value}]"
    )


__all__ = [
    "AgentGroupConfig",
    "BffContextCard",
    "BffKbChunk",
    "BffKbChunkCreate",
    "BffKbFeedbackRequest",
    "BffKbSearchRequest",
    "BffKbSearchResponse",
    "BffMessage",
    "BffReleaseResponse",
    "BffReplayResponse",
    "BffSessionDetail",
    "BffSessionSummary",
    "BffTakeoverRequest",
    "BffTakeoverResponse",
    "BffTicketDetail",
    "BffTicketSummary",
    "ChannelConfig",
    "HandoffRuleConfig",
    "MetricItem",
    "MetricsOverviewResponse",
    "MetricsTrendResponse",
    "SseEvent",
    "SseEventName",
]
