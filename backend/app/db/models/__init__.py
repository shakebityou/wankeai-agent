"""ORM 模型包。

**集中 import 是必需的**：`Base.metadata` 只有在模型类被 import 后才会包含
对应表。Alembic 的 `env.py` 与 `create_all()` 都只 import 本模块，靠这里的
副作用把全部表注册进元数据。漏 import 一个模块的后果是「迁移脚本里少了整张表」，
而且不会报错——只在查那张表时炸。
"""

from __future__ import annotations

from app.db.models.auth import AuthSession, Role, User
from app.db.models.config import ChannelConfig
from app.db.models.conversation import AuditLog, Message, Session, SessionState, Turn
from app.db.models.knowledge import (
    DEFAULT_BRAND,
    KbChunk,
    KbChunkVersion,
    KbFeedback,
    KbImportJob,
)
from app.db.models.ticket import AgentGroup, HandoffRule, Ticket, TicketEvent

__all__ = [
    "DEFAULT_BRAND",
    "AgentGroup",
    "AuditLog",
    "AuthSession",
    "ChannelConfig",
    "HandoffRule",
    "KbChunk",
    "KbChunkVersion",
    "KbFeedback",
    "KbImportJob",
    "Message",
    "Role",
    "Session",
    "SessionState",
    "Ticket",
    "TicketEvent",
    "Turn",
    "User",
]
