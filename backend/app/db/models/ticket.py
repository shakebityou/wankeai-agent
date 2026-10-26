"""工单与坐席模型（`BE-14` / `BE-15` / `FE-12` / `FE-13`）。

* `tickets` —— 工单主表（按 `session_id` 幂等）
* `agent_groups` —— 坐席分组（来自商家 `客服坐席分组表.csv` 的 6 个分组）
* `handoff_rules` —— 转人工规则配置（`FE-13` 可改，`BE-13` 读取）
* `ticket_events` —— 工单事件流水（创建/入队/重试/分配，用于排查「丢单」）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.db.types import dt_col, json_col, str_col, text_col


class Ticket(TimestampMixin, Base):
    """工单主表。

    **`session_id` 唯一约束是幂等的最终保障**。上游（Redis）可能因为重启丢失
    幂等键，但数据库唯一键不会。DoD「同 session_id 重复调用只生成 1 张工单」
    靠这条约束兜底——应用层查重是第一道，唯一键是第二道。

    Attributes:
        ticket_no: 工单号（主键），格式 `TS{yyyyMMdd}{4位序号}`。
        session_id: 关联会话（唯一，幂等键）。
        buyer_id: 买家 ID。
        channel: 渠道。
        reason: 转人工原因（USER_REQUEST / EMOTION / UNRESOLVED / HIGH_RISK / SYSTEM_ERROR）。
        summary: 问题摘要（≤ 500 字）。
        entities: 相关实体（订单号/SKU）。
        tried_actions: 已尝试动作。
        priority: 优先级。
        status: 工单状态（仅可创建，不可改状态——权限边界）。
        assignee: 分配坐席。
        group_id: 分配分组。
        queue_position: 排队位置。
        estimated_wait_seconds: 预计等待秒数。
        transcript: 对话记录（JSON 数组，最多 20 轮）。
        context_card: AI 上下文卡片（七要素，JSON）。
        context_complete: 七要素是否完整（DoD 完整率 100%）。
        offline: 是否走了离线兜底（BE-15）。
        merchant_ticket_no: 商家侧工单号（回调填充）。
        retry_count: 入队重试次数（退避序列 1/2/4/8/16s）。
        synced_at: 成功同步到商家侧的时间。
        created_at: 创建时间。
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_status_priority", "status", "priority"),
        Index("ix_tickets_group_status", "group_id", "status"),
        Index("ix_tickets_created", "created_at"),
        {"comment": "工单主表，按 session_id 幂等（BE-14）"},
    )

    ticket_no: Mapped[str] = mapped_column(str_col(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        str_col(64), unique=True, nullable=False, index=True
    )
    buyer_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    channel: Mapped[str] = mapped_column(str_col(16), default="TAOBAO", nullable=False)
    reason: Mapped[str] = mapped_column(
        str_col(32), default="USER_REQUEST", nullable=False
    )
    summary: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    entities: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )
    tried_actions: Mapped[list[str]] = mapped_column(
        json_col(), default=list, nullable=False
    )
    priority: Mapped[str] = mapped_column(str_col(16), default="NORMAL", nullable=False)
    status: Mapped[str] = mapped_column(str_col(16), default="OPEN", nullable=False)
    assignee: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    group_id: Mapped[str] = mapped_column(str_col(16), default="", nullable=False)
    queue_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_wait_seconds: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    transcript: Mapped[list[Any]] = mapped_column(json_col(), default=list, nullable=False)
    context_card: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )
    context_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    merchant_ticket_no: Mapped[str | None] = mapped_column(str_col(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)

    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketEvent.created_at",
    )

    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_no} {self.reason} {self.status} {self.priority}>"


class TicketEvent(Base):
    """工单事件流水。

    DoD 要求「5 QPS 限流下 200 并发不丢单」，排查丢单必须能看到每个工单经历了
    什么：入队 → 限流等待 → 重试第 N 次 → 成功/落本地队列。没有这张表，
    「丢了」和「还在队列里」无法区分。

    Attributes:
        id: 自增主键。
        ticket_no: 关联工单。
        event: 事件类型（CREATED / ENQUEUED / RATE_LIMITED / RETRY / SYNCED / FAILED / FALLBACK_LOCAL）。
        attempt: 第几次尝试。
        detail: 明细（JSON，含错误码与消息）。
        created_at: 事件时间。
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("ix_ticket_events_ticket_created", "ticket_no", "created_at"),
        {"comment": "工单事件流水，丢单排查依据（BE-14）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(
        str_col(32), ForeignKey("tickets.ticket_no", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(str_col(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(json_col(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<TicketEvent {self.ticket_no} {self.event} #{self.attempt}>"


class AgentGroup(Base):
    """坐席分组（来自商家 `客服坐席分组表.csv`）。

    **字段名对齐商家 CSV 列名**（`分组编号` → `group_id`、`在线时段` → `online_hours`），
    这样导入脚本是纯映射，不需要人工判断字段对应关系。

    Attributes:
        group_id: 分组编号（G1–G6）。
        group_name: 分组名称。
        scope: 负责范围。
        agent_count: 坐席人数。
        supervisor: 主管。
        online_hours: 在线时段（如 `09:00–22:00`，注意是**全角连接号**）。
        channels: 服务渠道列表。
        daily_volume: 日均接待量。
        peak_volume: 峰值接待量。
        note: 备注。
        enabled: 是否启用。
    """

    __tablename__ = "agent_groups"
    __table_args__ = ({"comment": "坐席分组，来自商家客服坐席分组表（BE-14 / FE-12）"},)

    group_id: Mapped[str] = mapped_column(str_col(16), primary_key=True)
    group_name: Mapped[str] = mapped_column(str_col(64), nullable=False)
    scope: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    agent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    supervisor: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    online_hours: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    channels: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    daily_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    peak_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<AgentGroup {self.group_id} {self.group_name} {self.online_hours}>"


class HandoffRule(Base):
    """转人工规则配置（`FE-13` 可改，`BE-13` 读取）。

    6 条规则对应 `rule_id` 1–6。**规则可配置但不能被前端任意篡改**：改规则写
    `audit_logs`，且 `USER_REQUEST`/`HIGH_RISK` 两条规则的 `enabled` 强制为 True
    （DoD「显式请求与高风险意图不得被挽留」——关掉这两条等于违反 DoD）。

    Attributes:
        rule_id: 规则编号（1–6）。
        name: 规则名。
        enabled: 是否启用。
        reason: 命中后映射的转人工原因。
        threshold: 阈值（如连续负面轮数 3）。
        keywords: 关键词列表（高风险意图用）。
        priority: 规则优先级（数值越大越优先）。
        description: 规则说明。
        updated_by: 最后修改人。
    """

    __tablename__ = "handoff_rules"
    __table_args__ = ({"comment": "转人工规则配置（BE-13 / FE-13）"},)

    rule_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str] = mapped_column(str_col(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(str_col(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(str_col(64), default="system", nullable=False)

    def __repr__(self) -> str:
        return f"<HandoffRule {self.rule_id} {self.name} enabled={self.enabled}>"


__all__ = ["AgentGroup", "HandoffRule", "Ticket", "TicketEvent"]
