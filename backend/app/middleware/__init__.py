"""ASGI 中间件包（纯 ASGI 实现，兼容 SSE 流式响应）。

导出顺序即 `app.main` 中的挂载顺序：**先挂的在最外层**。
`TraceMiddleware` 必须最外层——它建立 trace/request ContextVar，
内层中间件与业务代码都依赖它。
"""

from __future__ import annotations

from app.middleware.trace import (
    AccessLogMiddleware,
    MetricsMiddleware,
    TraceMiddleware,
)

__all__ = [
    "AccessLogMiddleware",
    "MetricsMiddleware",
    "TraceMiddleware",
]
