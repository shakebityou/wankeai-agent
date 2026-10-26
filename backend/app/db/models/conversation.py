"""会话与消息模型（`BE-03` / `BE-04` / `FE-01` / `FE-10`）。

**关键合规边界**：Redis 存「热状态」（TTL 30 分钟，脱敏），本表存「冷归档」
（会话与消息留痕，供坐席工作台与回放）。两者的字段口径必须一致——都**不含
完整手机号/地址/身份证**，入库前已过 `app.core.logging.mask_text`。

* `sessions` —— 会话主表（坐席工作台列表的数据源）
* `session_states` —— 会话状态快照（Redis 丢数据时的重建依据）
* `messages` —— 消息流水（`FE-10` 会话回放）
* `turns` —— 单轮对话记录（含延迟分解与决策结果，`FE-09` 指标看板数据源）
* `audit_logs` —— 操作审计（接管/释放/改状态，留存 ≥ 1 年）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.db.types import dt_col, json_col, str_col, text_col


class Session(TimestampMixin, Base):
    """会话主表。

    `session_id` 是商家回调里的 `session_id`，**不自造**——否则与商家侧对不上账。

    Attributes:
        session_id: 业务主键（商家侧会话 ID）。
        buyer_id: 买家 ID（商家侧匿名 ID，非手机号）。
        channel: 渠道枚举。
        owner: 会话归属（BOT / AGENT）。
        agent_id: 接管坐席工号。
        bot_muted: 机器人是否静音（一键接管后必须为 True，BE-04 DoD）。
        taken_over_at: 接管时间。
        released_at: 释放时间。
        turn_count: 累计轮次（BE-03 用它判断 > 10 强制转人工）。
        unresolved_count: 连续未解决次数。
        emotion: 最近情绪。
        negative_streak: 连续负面次数。
        intent_category: 最近意图类目。
        intent_stack: 意图栈（JSON 数组）。
        slots: 槽位（JSON 对象）。
        entities: 实体（JSON 对象）。
        buyer_bound: 是否已完成身份绑定（BE-08 越权防护依据）。
        bound_order_no: 已绑定订单号（脱敏后的前 4 后 4）。
        has_handoff: 是否已转人工。
        ticket_no: 关联工单号。
        waiting_seconds: 排队等待秒数（坐席工作台显示）。
        tags: 标签（JSON 数组）。
        last_message: 最后一条消息摘要（列表页展示用，已脱敏）。
        last_message_at: 最后消息时间。
        closed_at: 会话结束时间。
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_channel_last_message", "channel", "last_message_at"),
        Index("ix_sessions_owner_last_message", "owner", "last_message_at"),
        Index("ix_sessions_has_handoff", "has_handoff", "last_message_at"),
        {"comment": "会话主表（BE-03 / FE-01）"},
    )

    session_id: Mapped[str] = mapped_column(str_col(64), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    channel: Mapped[str] = mapped_column(str_col(16), default="TAOBAO", nullable=False)
    owner: Mapped[str] = mapped_column(str_col(8), default="BOT", nullable=False)
    agent_id: Mapped[str | None] = mapped_column(str_col(64), nullable=True)
    bot_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    taken_over_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emotion: Mapped[str] = mapped_column(str_col(16), default="NEUTRAL", nullable=False)
    negative_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    intent_category: Mapped[str] = mapped_column(
        str_col(32), default="兜底", nullable=False
    )
    intent_stack: Mapped[list[str]] = mapped_column(
        json_col(), default=list, nullable=False
    )
    slots: Mapped[dict[str, Any]] = mapped_column(json_col(), default=dict, nullable=False)
    entities: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )
    buyer_bound: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bound_order_no: Mapped[str | None] = mapped_column(str_col(32), nullable=True)
    has_handoff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ticket_no: Mapped[str | None] = mapped_column(str_col(32), nullable=True)
    waiting_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    last_message: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)

    messages: Mapped[list[Message]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    turns: Mapped[list[Turn]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Session {self.session_id} owner={self.owner} turns={self.turn_count}>"


class SessionState(Base):
    """会话状态快照（Redis 的持久化备份）。

    **为什么要冗余一份**：Redis 是内存存储，重启/主从切换会丢数据。会话状态
    丢了意味着机器人「忘记」前面聊过什么，用户体验断崖式下跌。每次状态变更后
    异步落一份快照，Redis 空了可从本表恢复。

    Attributes:
        session_id: 主键，与 `sessions` 一对一。
        state: 完整状态 JSON（结构对齐 `SessionState` schema）。
        turn_count: 冗余字段，便于统计。
        snapshot_at: 快照时间。
    """

    __tablename__ = "session_states"
    __table_args__ = ({"comment": "会话状态快照，Redis 丢失时的恢复依据（BE-03）"},)

    session_id: Mapped[str] = mapped_column(
        str_col(64),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[dict[str, Any]] = mapped_column(json_col(), default=dict, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<SessionState {self.session_id} turns={self.turn_count}>"


class Message(Base):
    """消息流水（双向）。

    `msg_id` 唯一约束是**回调幂等的第二道防线**：Redis SETNX 是第一道（快、
    但会过期），数据库唯一键是最后一道（持久、但慢）。两者都要有——只靠 Redis
    时，TTL 到期后的重推会重复入库。

    Attributes:
        id: 自增主键。
        msg_id: 商家侧消息 ID（幂等键）。
        session_id: 关联会话。
        direction: INBOUND / OUTBOUND。
        sender: USER / BOT / AGENT。
        msg_type: TEXT / IMAGE / VOICE（收）/ TEXT / IMAGE（发）。
        content: 文本内容（已脱敏）。
        text_source: 文本来源（ORIGINAL / ASR / OCR / VLM / CLASSIFIER）。
        confidence: 归一化置信度。
        media_url: 媒体地址（OSS/本地）。
        tag: 图片标签（商品图 / 订单截图 / 瑕疵图 / 尺码表 / 表情包）。
        intent: 该消息的意图。
        trace_id: 全链路追踪 ID。
        latency_ms: 该消息处理耗时。
        extra: 扩展字段（延迟分解等）。
        created_at: 消息时间。
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        Index("ix_messages_trace", "trace_id"),
        {"comment": "消息流水，会话回放数据源（FE-10）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(str_col(64), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(
        str_col(64),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(str_col(8), default="INBOUND", nullable=False)
    sender: Mapped[str] = mapped_column(str_col(8), default="USER", nullable=False)
    msg_type: Mapped[str] = mapped_column(str_col(16), default="TEXT", nullable=False)
    content: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    text_source: Mapped[str] = mapped_column(
        str_col(16), default="ORIGINAL", nullable=False
    )
    confidence: Mapped[float] = mapped_column(default=1.0, nullable=False)
    media_url: Mapped[str | None] = mapped_column(str_col(500), nullable=True)
    tag: Mapped[str | None] = mapped_column(str_col(32), nullable=True)
    intent: Mapped[str | None] = mapped_column(str_col(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(json_col(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    session: Mapped[Session] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message {self.msg_id} {self.direction} {self.msg_type}>"


class Turn(Base):
    """单轮对话记录（`BE-04` 编排结果 + `FE-09` 指标数据源）。

    **为什么要单独一张表而不是塞进 `messages`**：一轮用户输入可能产生多条
    出站消息（分段发送），但只对应一次决策。指标（首响时长、转人工率、
    决策准确率）都按「轮」而非「条」统计。

    Attributes:
        id: 自增主键。
        turn_id: 轮次业务 ID。
        session_id: 关联会话。
        trace_id: 全链路追踪 ID。
        turn_index: 会话内第几轮。
        user_text: 用户输入（脱敏后）。
        reply_text: 机器人回复（脱敏后）。
        decision: RAG 决策（DIRECT_ANSWER / WEAK / NO_EVIDENCE）。
        top_score: 检索最高分。
        actions: 执行的动作列表（JSON 数组）。
        tool_calls: 工具调用记录（JSON 数组）。
        rag_hits: 命中的 chunk（JSON 数组）。
        handoff_reason: 转人工原因（未转人工则为 None）。
        latency: 延迟分解（JSON，对应 `LatencyBreakdown`）。
        degraded: 降级标记列表。
        blocked: 是否被合规闸拦截。
        created_at: 轮次时间。
    """

    __tablename__ = "turns"
    __table_args__ = (
        Index("ix_turns_session_index", "session_id", "turn_index"),
        Index("ix_turns_trace", "trace_id"),
        Index("ix_turns_created_decision", "created_at", "decision"),
        {"comment": "单轮对话记录，指标看板数据源（BE-04 / FE-09）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(str_col(64), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(
        str_col(64),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    user_text: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    reply_text: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    decision: Mapped[str] = mapped_column(str_col(16), default="NO_EVIDENCE", nullable=False)
    top_score: Mapped[float] = mapped_column(default=0.0, nullable=False)
    actions: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    tool_calls: Mapped[list[Any]] = mapped_column(json_col(), default=list, nullable=False)
    rag_hits: Mapped[list[Any]] = mapped_column(json_col(), default=list, nullable=False)
    handoff_reason: Mapped[str | None] = mapped_column(str_col(32), nullable=True)
    latency: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )
    degraded: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    session: Mapped[Session] = relationship(back_populates="turns")

    def __repr__(self) -> str:
        return f"<Turn {self.turn_id} #{self.turn_index} {self.decision}>"


class AuditLog(Base):
    """操作审计日志（留存 ≥ 1 年，任务书 3.8 节 BE-14 与商家附录 D 要求）。

    记录所有**写操作**：接管/释放会话、创建工单、知识库增删改、规则变更。
    只记「谁在什么时候对什么做了什么」，不记业务正文（正文在各自业务表里）。

    Attributes:
        id: 自增主键。
        actor: 操作人（坐席工号 / system / agent）。
        action: 动作（TAKEOVER / RELEASE / TICKET_CREATE / KB_UPDATE / RULE_UPDATE …）。
        target_type: 对象类型（session / ticket / chunk / rule）。
        target_id: 对象 ID。
        detail: 变更明细（JSON，含 before/after 摘要）。
        ip: 来源 IP。
        user_agent: 客户端标识。
        trace_id: 追踪 ID。
        created_at: 操作时间。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_target", "target_type", "target_id", "created_at"),
        Index("ix_audit_logs_actor_created", "actor", "created_at"),
        {"comment": "写操作审计日志，留存 ≥ 1 年（BE-14 / 商家附录 D）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(str_col(64), default="system", nullable=False)
    action: Mapped[str] = mapped_column(str_col(32), nullable=False)
    target_type: Mapped[str] = mapped_column(str_col(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(json_col(), default=dict, nullable=False)
    ip: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(str_col(255), default="", nullable=False)
    trace_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.target_type}:{self.target_id}>"


__all__ = ["AuditLog", "Message", "Session", "SessionState", "Turn"]
