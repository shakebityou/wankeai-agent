"""核心基础组件（L7 基础层）。

| 模块 | 对应任务 | 说明 |
| --- | --- | --- |
| `enums` | 2.6.1 | 数据字典与枚举总表，前后端唯一口径 |
| `errors` | 2.3.3 / 2.5.6 | 商家错误码策略 + BFF 错误码 + 统一异常 |
| `config` | 3.1 | 全部可配置项，密钥只从环境变量/KMS 注入 |
| `trace` | NFR-06 | trace_id / request_id / session_id 上下文 |
| `response` | 2.5.5 | 统一响应包装与分页 |
| `logging` | BE-17 | 结构化日志 + 日志落盘脱敏 |
| `idempotency` | BE-18 | 幂等框架（Redis SETNX，key 维度可配） |
| `ratelimit` | BE-18 | 令牌桶限流 + 熔断器 |
| `retry` | BE-18 | 可配退避序列的重试装饰器 |
"""

from app.core.config import Settings, get_settings
from app.core.errors import (
    AppError,
    BffErrorCode,
    MerchantApiError,
    MerchantErrorCode,
)
from app.core.idempotency import IdempotencyStore, idempotent
from app.core.logging import get_logger, mask_text, setup_logging
from app.core.ratelimit import CircuitBreaker, TokenBucketRateLimiter
from app.core.response import ApiResponse, PageParams, PageResult
from app.core.retry import retry_async, retry_sync
from app.core.trace import (
    get_trace_id,
    new_request_id,
    new_trace_id,
    request_context,
    trace_context,
)

__all__ = [
    "ApiResponse",
    "AppError",
    "BffErrorCode",
    "CircuitBreaker",
    "IdempotencyStore",
    "MerchantApiError",
    "MerchantErrorCode",
    "PageParams",
    "PageResult",
    "Settings",
    "TokenBucketRateLimiter",
    "get_logger",
    "get_settings",
    "get_trace_id",
    "idempotent",
    "mask_text",
    "new_request_id",
    "new_trace_id",
    "request_context",
    "retry_async",
    "retry_sync",
    "setup_logging",
    "trace_context",
]
