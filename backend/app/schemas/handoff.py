"""契约 B · 转人工服务（任务书 2.4.5 节）。

    POST /internal/v1/handoff

**`reason` 枚举（5 值，须与人工工作台一致，属待商家确认项 C-01）**：

| `reason` | 触发来源 | 对应触发规则 | `priority` 建议 |
| --- | --- | --- | --- |
| `USER_REQUEST` | 用户显式要求 | 规则① 显式请求 | `HIGH` |
| `EMOTION` | 情绪激动/辱骂/威胁 | 规则② 情绪 | `HIGH` |
| `UNRESOLVED` | 同一问题连续 2 轮兜底 | 规则③ 未解决 | `NORMAL` |
| `HIGH_RISK` | 投诉/赔付/法律/平台介入/媒体曝光 | 规则④ 高风险意图 | `URGENT` |
| `SYSTEM_ERROR` | LLM/检索/工具超时或报错 | 规则⑥ 系统异常 | `HIGH` |

> 规则⑤（检索失败，最高分 < 0.55 且问题为业务类）归入 `UNRESOLVED` 或
> `HIGH_RISK` 由业务决定，需在 M3 前与客服主管确认。

⚠️ **`handoff_url` 合规约束**：商家 0.8 节明确该字段**仅作字段占位**，
不接入小程序/H5 唤起。对用户**不得发送该链接**，只能发纯文字提示；
对坐席可在内网工作台作为「打开工单详情」跳转使用。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import Channel, HandoffReason, Priority, TicketStatus

#: `reason` → 建议 `priority` 映射（BE-13/BE-14 依据）
REASON_DEFAULT_PRIORITY: dict[HandoffReason, Priority] = {
    HandoffReason.USER_REQUEST: Priority.HIGH,
    HandoffReason.EMOTION: Priority.HIGH,
    HandoffReason.UNRESOLVED: Priority.NORMAL,
    HandoffReason.HIGH_RISK: Priority.URGENT,
    HandoffReason.SYSTEM_ERROR: Priority.HIGH,
}

#: `reason` → 中文说明（坐席工作台展示）
REASON_CN: dict[HandoffReason, str] = {
    HandoffReason.USER_REQUEST: "用户主动要求人工",
    HandoffReason.EMOTION: "用户情绪激动",
    HandoffReason.UNRESOLVED: "连续未解决",
    HandoffReason.HIGH_RISK: "高风险诉求",
    HandoffReason.SYSTEM_ERROR: "系统异常",
}


class TranscriptTurn(BaseModel):
    """工单对话摘录的一轮。

    ⚠️ 商家未定义 `transcript` 数组上限（待确认项 C-04），
    本模型按**最多 20 轮**做防御性截断，并在摘要中说明。
    """

    role: str = Field(description="角色：user / assistant / agent")
    content: str = Field(description="内容（**落库前必须脱敏**）")


class TicketContextCard(BaseModel):
    """AI 上下文卡片（**转人工上下文完整率 100%** 的判定对象）。

    字段完整性校验（BE-14 DoD）：以下**七项缺一即为缺陷**：
        `summary`、`verified`、`tried_actions`、`unmet_demand`、
        `suggested_action`、`emotion`、`turn_count`。
    """

    #: 可省略：建单接口顶层已有 `session_id`，服务层会把它写回卡片，
    #: 调用方不必重复传（重复传也允许，以顶层为准）。
    session_id: str = Field(default="", description="会话 ID")
    buyer_id: str = Field(default="", description="买家标识")
    channel: Channel | None = Field(default=None, description="渠道")
    waiting_seconds: int = Field(default=0, ge=0, description="已等待秒数")
    summary: str = Field(default="", description="① 问题摘要（≤500 字）")
    verified: list[dict[str, Any]] = Field(
        default_factory=list, description="② 已核实事实（订单/物流/库存）"
    )
    tried_actions: list[dict[str, Any]] = Field(
        default_factory=list, description="③ 已尝试动作及结果"
    )
    unmet_demand: str = Field(default="", description="④ 未满足的诉求")
    suggested_action: str = Field(default="", description="⑤ 建议处理动作")
    emotion: dict[str, str] = Field(
        default_factory=dict, description="⑥ 情绪 {level, intensity}"
    )
    turn_count: int = Field(default=0, ge=0, description="⑦ 会话轮次")
    attachments: list[dict[str, Any]] = Field(
        default_factory=list, description="附件（图片等）"
    )
    ticket_no: str | None = Field(default=None, description="关联工单号")
    handoff_url: str | None = Field(
        default=None, description="工单跳转地址，**仅坐席内网可用，不得发给用户**"
    )

    def missing_fields(self) -> list[str]:
        """返回缺失的七要素字段名（用于 BE-14 完整性校验）。

        `verified` / `tried_actions` / `emotion` 允许为空集合，
        但必须是**显式空**（前端展示「无」），因此只校验字段存在性由
        Pydantic 保证；此处校验**内容非空**的语义要求。
        """
        missing: list[str] = []
        if not self.summary.strip():
            missing.append("summary")
        if not self.verified:
            missing.append("verified")
        if not self.tried_actions:
            missing.append("tried_actions")
        if not self.unmet_demand.strip():
            missing.append("unmet_demand")
        if not self.suggested_action.strip():
            missing.append("suggested_action")
        if not self.emotion:
            missing.append("emotion")
        if self.turn_count <= 0:
            missing.append("turn_count")
        return missing

    def is_complete(self) -> bool:
        """七要素是否齐全。"""
        return not self.missing_fields()


class HandoffRequest(BaseModel):
    """转人工请求（2.4.5 节）。"""

    session_id: str = Field(min_length=1, description="会话 ID，工单幂等键")
    buyer_id: str = Field(default="", description="买家标识")
    reason: HandoffReason = Field(description="转人工原因（5 值）")
    summary: str = Field(
        default="", max_length=500, description="问题摘要，≤500 字"
    )
    entities: dict[str, Any] = Field(
        default_factory=dict, description="结构化实体（order_no / sku_id 等）"
    )
    tried_actions: list[str] = Field(
        default_factory=list, description="已尝试动作名列表"
    )
    priority: Priority | None = Field(
        default=None, description="优先级；None 时按 reason 取默认值"
    )
    channel: Channel | None = Field(default=None, description="渠道")
    transcript: list[TranscriptTurn] = Field(
        default_factory=list, description="对话摘录"
    )
    context_card: TicketContextCard | None = Field(
        default=None, description="AI 上下文卡片（七要素）"
    )

    @field_validator("transcript")
    @classmethod
    def _trim_transcript(
        cls, v: list[TranscriptTurn]
    ) -> list[TranscriptTurn]:
        """防御性截断：只保留最近 20 轮（C-04 未确认前的保守值）。"""
        return v[-20:]

    def effective_priority(self) -> Priority:
        """生效优先级：显式指定优先，否则按 reason 推导。"""
        return self.priority or REASON_DEFAULT_PRIORITY.get(
            self.reason, Priority.NORMAL
        )


class HandoffResponse(BaseModel):
    """转人工响应（对齐商家 5.4 节）。

    Attributes:
        ticket_no: 工单号，格式 `TS{yyyyMMdd}{4 位序号}`。
        status: 工单状态。
        priority: 优先级。
        assignee: 分配到的坐席/主管。
        queue_position: 排队位置。
        estimated_wait_seconds: 预计等待秒数。
        created_at: 创建时间。
        handoff_url: 工单跳转地址，**仅坐席内网可用**。
        idempotent_hit: 是否命中幂等（同 session_id 重复提交）。
        user_message: 给用户看的纯文字提示（**不含链接**）。
        offline: 是否走了「人工不在线」兜底（BE-15）。
    """

    ticket_no: str = Field(description="工单号")
    status: TicketStatus = Field(default=TicketStatus.OPEN, description="工单状态")
    priority: Priority = Field(default=Priority.NORMAL, description="优先级")
    assignee: str | None = Field(default=None, description="受理人")
    queue_position: int | None = Field(default=None, ge=0, description="排队位置")
    estimated_wait_seconds: int | None = Field(
        default=None, ge=0, description="预计等待秒数"
    )
    created_at: datetime | None = Field(default=None, description="创建时间")
    handoff_url: str | None = Field(default=None, description="工单地址（仅坐席）")
    idempotent_hit: bool = Field(default=False, description="是否幂等命中")
    user_message: str | None = Field(default=None, description="用户侧纯文字提示")
    offline: bool = Field(default=False, description="是否人工不在线兜底")

    @field_validator("user_message")
    @classmethod
    def _no_link(cls, v: str | None) -> str | None:
        """用户侧话术**严禁**出现链接或跳转引导（HC-1 硬约束）。"""
        if v is None:
            return None
        forbidden = ("http://", "https://", "点击", "点这里", "跳转", "小程序")
        for token in forbidden:
            if token in v:
                raise ValueError(
                    f"用户侧话术不得包含「{token}」（HC-1：不生成外链与跳转引导）"
                )
        return v


# ---------------------------------------------------------------------------
# BE-13 规则管理（后台「转人工规则」页）
# ---------------------------------------------------------------------------


class HandoffRuleOut(BaseModel):
    """一条转人工规则（只读展示）。"""

    rule_id: int = Field(description="规则号 1~6")
    name: str = Field(description="规则名")
    reason: HandoffReason = Field(description="命中后写入工单的 reason")
    priority: int = Field(description="仲裁优先级，数值越大越优先")
    threshold: float = Field(default=0.0, description="阈值（语义随规则而变）")
    keywords: list[str] = Field(default_factory=list, description="关键词表")
    enabled: bool = Field(default=True, description="是否启用")
    description: str = Field(default="", description="说明")
    always_on: bool = Field(
        default=False, description="是否不可关闭（①显式请求 / ④高风险）"
    )


class HandoffRuleUpdate(BaseModel):
    """规则更新请求（**只允许改这四个字段**）。

    `reason` 与 `rule_id` 不可改：改了等于换了一条规则，审计链会断。
    """

    enabled: bool | None = Field(default=None, description="是否启用")
    threshold: float | None = Field(default=None, description="阈值")
    priority: int | None = Field(default=None, description="仲裁优先级")
    keywords: list[str] | None = Field(default=None, description="关键词表")


class RuleEvalRequest(BaseModel):
    """规则试算请求（**不建单**，只回答「会不会转、为什么转」）。"""

    text: str = Field(default="", description="用户原话")
    intent_label: str = Field(default="", description="意图细分标签")
    intent_category: str | None = Field(default=None, description="意图大类")
    emotion: str | None = Field(default=None, description="当前情绪")
    negative_streak: int = Field(default=0, ge=0, description="连续负面轮次")
    unresolved_count: int = Field(default=0, ge=0, description="连续兜底轮次")
    turn_count: int = Field(default=0, ge=0, description="会话轮次")
    retrieval_decision: str | None = Field(
        default=None, description="RAG 决策（NO_EVIDENCE 时规则⑤可能命中）"
    )
    top_score: float | None = Field(default=None, description="检索最高分")
    system_error: bool = Field(default=False, description="本轮是否系统异常")


__all__ = [
    "HandoffRequest",
    "HandoffResponse",
    "HandoffRuleOut",
    "HandoffRuleUpdate",
    "REASON_CN",
    "REASON_DEFAULT_PRIORITY",
    "RuleEvalRequest",
    "TicketContextCard",
    "TranscriptTurn",
]
