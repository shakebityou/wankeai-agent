"""FastAPI 异常处理器（统一响应格式，契约 C 2.5.5）。

**为什么单独成模块**：异常到响应的映射是契约的一部分，散落在 `main.py`
里会让「某类异常返回哪个错误码」变得不可检索。这里集中一张表，并在
`app/tests` 里逐条断言。

映射规则：

| 异常 | BFF 错误码 | HTTP | 说明 |
| --- | --- | --- | --- |
| `AppError` | 自带 `code` | 自带 `http_status` | 业务显式抛出 |
| `MerchantApiError` | `30001` | 503 | 商家接口不可用（含 `retry_after`） |
| `RequestValidationError` | `10001` | 400 | Pydantic 校验失败，**逐字段展开** |
| `RateLimitExceeded` | `20001` | 429 | 带 `Retry-After` 头 |
| `HTTPException` | 按状态码反查 | 原状态码 | FastAPI 内置（404 路由不存在等） |
| `Exception` | `30001` | 503 | 兜底，**不回显异常消息**（防信息泄露） |

安全要点：兜底分支只返回通用话术，绝不把异常字符串回显给前端——
异常消息常含 DSN、内网地址、SQL 片段。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, BffErrorCode, MerchantApiError
from app.core.logging import get_logger, mask_text
from app.core.ratelimit import RateLimitExceeded
from app.core.response import fail
from app.core.trace import get_trace_id

logger = get_logger(__name__)

#: HTTP 状态码 → BFF 错误码（`HTTPException` 反查用）
_HTTP_TO_BFF: dict[int, BffErrorCode] = {
    400: BffErrorCode.PARAM_INVALID,
    401: BffErrorCode.UNAUTHENTICATED,
    403: BffErrorCode.FORBIDDEN,
    404: BffErrorCode.NOT_FOUND,
    405: BffErrorCode.PARAM_INVALID,
    409: BffErrorCode.CONFLICT,
    422: BffErrorCode.PARAM_INVALID,
    429: BffErrorCode.TOO_MANY_REQUESTS,
    503: BffErrorCode.DOWNSTREAM_UNAVAILABLE,
}

#: 兜底话术（**不含任何内部信息**）
_GENERIC_MESSAGE = "服务暂时不可用，请稍后重试"


def _body(code: int, message: str) -> dict[str, Any]:
    """构造统一失败响应体。"""
    return fail(code, message)


def _app_error_headers(exc: AppError) -> dict[str, str]:
    """`AppError` 需要额外下发的响应头（目前只有限流的 `Retry-After`）。"""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return {"Retry-After": str(max(1, int(retry_after)))}
    return {}


def register_exception_handlers(app: FastAPI) -> None:
    """把所有异常处理器注册到 FastAPI 应用。

    Args:
        app: FastAPI 实例。
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """业务显式异常（错误码由异常自带）。"""
        logger.warning(
            "业务异常：code=%s message=%s",
            int(exc.code),
            mask_text(exc.message),
            extra={
                "extra": {
                    "bff_code": int(exc.code),
                    "detail": mask_text(exc.detail) if exc.detail else None,
                    "trace_id": get_trace_id(),
                }
            },
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=_body(int(exc.code), exc.message),
            headers=_app_error_headers(exc),
        )

    @app.exception_handler(MerchantApiError)
    async def _handle_merchant_error(_: Request, exc: MerchantApiError) -> JSONResponse:
        """商家接口异常 → 统一收敛为 `30001`（下游不可用）。

        商家错误码**不外泄给前端**：前端只需知道「下游挂了、走降级」，
        具体是 40102 还是 50401 属于后端排障信息，落日志即可。
        """
        logger.error(
            "商家接口异常：merchant_code=%s",
            int(exc.code),
            extra={
                "extra": {
                    "merchant_code": int(exc.code),
                    "request_id": exc.request_id,
                    "retryable": exc.is_retryable,
                    "trace_id": get_trace_id(),
                }
            },
        )
        headers: dict[str, str] = {}
        if exc.retry_after:
            headers["Retry-After"] = str(int(exc.retry_after))
        message = exc.policy.user_message or _GENERIC_MESSAGE
        return JSONResponse(
            status_code=503,
            content=_body(int(BffErrorCode.DOWNSTREAM_UNAVAILABLE), message),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Pydantic 校验失败 → `10001`，逐字段给出可读提示。"""
        fields: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            msg = err.get("msg", "参数不合法")
            fields.append(f"{loc or 'body'}: {msg}")
        message = "；".join(fields[:5]) or "参数校验失败"
        logger.info(
            "参数校验失败：%s",
            mask_text(message),
            extra={"extra": {"trace_id": get_trace_id()}},
        )
        return JSONResponse(
            status_code=400,
            content=_body(int(BffErrorCode.PARAM_INVALID), message),
        )

    @app.exception_handler(RateLimitExceeded)
    async def _handle_ratelimit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        """限流 → `20001` + `Retry-After`（前端按钮禁用 + 倒计时）。"""
        retry_after = max(1, int(round(exc.retry_after)))
        return JSONResponse(
            status_code=429,
            content=_body(
                int(BffErrorCode.TOO_MANY_REQUESTS),
                f"请求过于频繁，请 {retry_after} 秒后再试",
            ),
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """FastAPI / Starlette 内置 HTTP 异常（404 路由、405 方法等）。"""
        code = _HTTP_TO_BFF.get(exc.status_code, BffErrorCode.PARAM_INVALID)
        detail = exc.detail if isinstance(exc.detail, str) else ""
        message = detail or "请求无法处理"
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(int(code), message),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底：**不回显异常消息**，只记日志。

        未处理异常意味着代码缺陷，日志要尽可能全（含路径与 trace_id），
        但对前端只给通用话术。
        """
        logger.exception(
            "未处理异常：%s %s",
            request.method,
            request.url.path,
            extra={
                "extra": {
                    "exception": type(exc).__name__,
                    "path": request.url.path,
                    "trace_id": get_trace_id(),
                }
            },
        )
        return JSONResponse(
            status_code=503,
            content=_body(
                int(BffErrorCode.DOWNSTREAM_UNAVAILABLE), _GENERIC_MESSAGE
            ),
        )


__all__ = ["register_exception_handlers"]
