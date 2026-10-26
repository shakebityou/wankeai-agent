"""ASGI 中间件（纯 ASGI 实现，不用 `BaseHTTPMiddleware`）。

**为什么不用 `starlette.middleware.base.BaseHTTPMiddleware`**：
它会用一个 anyio 内存流包装 `send`，在 SSE（`/bff/v1/chat/stream`、
`/internal/v1/chat/turn` 流式）场景下会把响应体缓冲住，导致流式输出
失去实时性，并在客户端断开时留下悬挂任务。纯 ASGI 中间件直接透传
`receive`/`send`，是 SSE 唯一安全的写法。

三个中间件：

| 中间件 | 职责 |
| --- | --- |
| `TraceMiddleware` | 注入 / 透传 `X-Trace-Id`、`X-Request-Id`，绑定 ContextVar |
| `AccessLogMiddleware` | 访问日志（**经 `mask_value` 脱敏**，BE-17 日志零敏感字段） |
| `MetricsMiddleware` | Prometheus HTTP 指标，**路径标签用路由模板**防基数爆炸 |
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.core.logging import get_logger, mask_text
from app.core.metrics import record_http
from app.core.trace import new_request_id, new_trace_id, request_context, trace_context

logger = get_logger("app.access")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TRACE_HEADER = "x-trace-id"
REQUEST_HEADER = "x-request-id"

#: 不记访问日志的路径（探针噪声）
_SILENT_PATHS: frozenset[str] = frozenset(
    {"/health", "/health/live", "/health/ready", "/health/deps", "/metrics", "/favicon.ico"}
)


def _header(scope: Scope, name: str) -> str | None:
    """从 ASGI scope 取请求头（大小写不敏感）。"""
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


class TraceMiddleware:
    """trace_id / request_id 注入与回写。

    入站优先复用上游 `X-Trace-Id`（跨服务链路串联），缺失则生成；
    出站一律回写 `X-Trace-Id` 与 `X-Request-Id`（任务书 DoD：所有接口携带 trace_id）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = _header(scope, TRACE_HEADER) or new_trace_id()
        request_id = _header(scope, REQUEST_HEADER) or new_request_id()
        scope.setdefault("state", {})
        scope["state"]["trace_id"] = trace_id
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-trace-id", trace_id.encode("latin-1")))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with trace_context(trace_id), request_context(request_id):
            await self.app(scope, receive, send_wrapper)


class AccessLogMiddleware:
    """访问日志（脱敏）。

    只记录方法、路径、状态码、耗时、客户端、trace_id。
    **不记录请求体 / 响应体 / query 值**——这些字段最容易夹带手机号、地址、
    订单号，且 BE-17 DoD 要求日志零敏感字段。确实需要排障时，业务层自行
    用 `mask_text` 处理后再显式记录。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _SILENT_PATHS:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            client = scope.get("client") or ("-", 0)
            logger.info(
                "%s %s %s",
                scope.get("method", "-"),
                mask_text(path),
                status_holder["status"],
                extra={
                    "extra": {
                        "method": scope.get("method", "-"),
                        "path": path,
                        "status": status_holder["status"],
                        "duration_ms": duration_ms,
                        "client": client[0],
                        "trace_id": (scope.get("state") or {}).get("trace_id"),
                    }
                },
            )


class MetricsMiddleware:
    """Prometheus HTTP 指标。

    路径标签优先取 FastAPI 写入 `scope["route"]` 的路由模板；
    未匹配路由（404）时回落到 `__unmatched__`，**绝不用真实路径**，
    否则 `/bff/v1/sessions/{session_id}` 会把基数打爆。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "__unmatched__"
            record_http(
                scope.get("method", "-"),
                template,
                status_holder["status"],
                time.perf_counter() - started,
            )


__all__ = [
    "AccessLogMiddleware",
    "MetricsMiddleware",
    "TraceMiddleware",
]
