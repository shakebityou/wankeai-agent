"""全链路追踪（NFR-06 可观测）。

一次用户请求从渠道回调到最终出站，**所有服务、所有日志必须携带同一个 `trace_id`**，
`request_id` 则标识单次下游调用（写操作幂等键之一）。二者通过 `contextvars` 传递，
无需逐层透传参数。

用法：
    from app.core.trace import trace_context, new_trace_id

    with trace_context() as tid:
        ...            # 本作用域内 get_trace_id() 返回 tid
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: 全链路追踪 ID。一次请求内所有模块共享
_trace_id: ContextVar[str | None] = ContextVar("wk_trace_id", default=None)
#: 单次下游调用 ID（幂等键维度之一）
_request_id: ContextVar[str | None] = ContextVar("wk_request_id", default=None)
#: 当前会话 ID，便于日志聚合
_session_id: ContextVar[str | None] = ContextVar("wk_session_id", default=None)
#: 当前买家 ID（脱敏后稳定 ID），便于审计
_buyer_id: ContextVar[str | None] = ContextVar("wk_buyer_id", default=None)


def _rand(prefix: str) -> str:
    """生成 `prefix_` + 16 位十六进制随机串。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    """生成新的 trace_id。"""
    return _rand("trace")


def new_request_id() -> str:
    """生成新的 request_id。"""
    return _rand("req")


def new_msg_id() -> str:
    """生成新的 msg_id（本地出站消息幂等键，重复提交复用）。"""
    return _rand("msg")


def new_session_id() -> str:
    """生成新的 session_id。"""
    return _rand("sess")


def new_ticket_no() -> str:
    """生成工单号，格式 `TS` + 12 位数字。"""
    return "TS" + uuid.uuid4().int.__str__()[:12].ljust(12, "0")


def get_trace_id() -> str:
    """获取当前 trace_id；不存在时**自动生成并写入上下文**（保证日志总有 trace_id）。"""
    tid = _trace_id.get()
    if tid is None:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid


def get_request_id() -> str | None:
    """获取当前 request_id，可能为 None（非下游调用场景）。"""
    return _request_id.get()


def get_session_id() -> str | None:
    """获取当前 session_id。"""
    return _session_id.get()


def get_buyer_id() -> str | None:
    """获取当前 buyer_id。"""
    return _buyer_id.get()


def set_session_id(session_id: str | None) -> None:
    """设置当前 session_id（进入会话处理时调用）。"""
    _session_id.set(session_id)


def set_buyer_id(buyer_id: str | None) -> None:
    """设置当前 buyer_id（进入会话处理时调用）。"""
    _buyer_id.set(buyer_id)


@contextmanager
def trace_context(
    trace_id: str | None = None,
    *,
    session_id: str | None = None,
    buyer_id: str | None = None,
) -> Iterator[str]:
    """开启一个追踪作用域，退出时自动还原上一层上下文（支持嵌套）。

    Args:
        trace_id: 复用已有 trace_id（跨进程/跨服务传递）；None 则新建。
        session_id: 可选，绑定会话。
        buyer_id: 可选，绑定买家。

    Yields:
        本作用域生效的 trace_id。
    """
    tid = trace_id or new_trace_id()
    t_token = _trace_id.set(tid)
    s_token = _session_id.set(session_id) if session_id is not None else None
    b_token = _buyer_id.set(buyer_id) if buyer_id is not None else None
    try:
        yield tid
    finally:
        _trace_id.reset(t_token)
        if s_token is not None:
            _session_id.reset(s_token)
        if b_token is not None:
            _buyer_id.reset(b_token)


@contextmanager
def request_context(request_id: str | None = None) -> Iterator[str]:
    """开启一次下游调用的 request_id 作用域。"""
    rid = request_id or new_request_id()
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


__all__ = [
    "get_buyer_id",
    "get_request_id",
    "get_session_id",
    "get_trace_id",
    "new_msg_id",
    "new_request_id",
    "new_session_id",
    "new_ticket_no",
    "new_trace_id",
    "request_context",
    "set_buyer_id",
    "set_session_id",
    "trace_context",
]
