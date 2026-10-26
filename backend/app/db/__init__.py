"""数据访问层（`BE-03` / `BE-06` / `BE-14`）。

对外暴露：
* `Base` / `TimestampMixin` / `SoftDeleteMixin` —— 声明式基类
* `get_engine` / `get_session_factory` / `session_scope` —— 引擎与会话
* `create_all` / `ping` / `dispose_engine` —— 建表与探活
* 全部 ORM 模型

**为什么不在这里放 Repository 类**：数据访问方法按业务归属分散在
`app/services/*/repository.py` 里（如 `knowledge/repository.py`）。集中放一个
巨型 `repository.py` 会让任何一处改动都触碰同一个文件，多人并行必然冲突。
"""

from __future__ import annotations

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, utcnow
from app.db.engine import (
    create_all,
    dispose_engine,
    get_engine,
    get_session_factory,
    ping,
    reset_engine,
    session_scope,
)
from app.db.models import (
    AgentGroup,
    AuditLog,
    HandoffRule,
    KbChunk,
    KbChunkVersion,
    KbFeedback,
    KbImportJob,
    Message,
    Session,
    SessionState,
    Ticket,
    TicketEvent,
    Turn,
)
from app.db.types import JSONType, UTCDateTime, dt_col, json_col, pk_col, str_col, text_col

__all__ = [
    "AgentGroup",
    "AuditLog",
    "Base",
    "HandoffRule",
    "JSONType",
    "KbChunk",
    "KbChunkVersion",
    "KbFeedback",
    "KbImportJob",
    "Message",
    "Session",
    "SessionState",
    "SoftDeleteMixin",
    "Ticket",
    "TicketEvent",
    "TimestampMixin",
    "Turn",
    "UTCDateTime",
    "create_all",
    "dispose_engine",
    "dt_col",
    "get_engine",
    "get_session_factory",
    "json_col",
    "ping",
    "pk_col",
    "reset_engine",
    "session_scope",
    "str_col",
    "text_col",
    "utcnow",
]
