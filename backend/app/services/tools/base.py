"""工具层统一返回对象与重试/降级骨架。

`ToolResult` 是编排层与 HTTP 层共用的返回类型：`data` 给结构化消费，
`text` 给送模型的 prompt（orchestrator 两个都取，缺一个就静默降级）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import MerchantApiError
from app.core.logging import mask_order_no

#: 各工具的重试次数上限（**不含首次**），依据 `MERCHANT_ERROR_POLICIES`。
#: 读接口天然幂等（GET + 同参数），重试不会产生重复写（BE-18 DoD③）。
MAX_RETRIES: dict[str, int] = {
    "order_query": 3,
    "logistics_query": 3,
    "inventory_query": 3,
    "product_query": 3,
}


@dataclass
class ToolResult:
    """一次工具调用的结果。

    Attributes:
        tool: 工具名（`order_query` 等）。
        ok: 是否成功。
        data: 结构化业务数据（成功时）。
        text: 供拼 prompt 的自然语言摘要（成功时）。
        error_code: 商家错误码（失败时）。
        user_message: 用户侧降级话术（失败时）。
        degraded: 降级标识列表。
        latency_ms: 耗时（含重试）。
        attempts: 实际请求次数（含重试，≥1）。
    """

    tool: str
    ok: bool = False
    data: dict[str, Any] | None = None
    text: str = ""
    error_code: int | None = None
    user_message: str | None = None
    degraded: list[str] = field(default_factory=list)
    latency_ms: int = 0
    attempts: int = 1


def result_from_error(
    tool: str,
    exc: MerchantApiError,
    *,
    latency_ms: int = 0,
    attempts: int = 1,
) -> ToolResult:
    """把 `MerchantApiError` 翻译成失败的 `ToolResult`。

    话术与降级标识**全部来自** `MERCHANT_ERROR_POLICIES`，工具层不自行编造，
    保证「商家改错误码语义时只需改一处」。
    """
    policy = exc.policy
    degraded = [f"{tool.upper()}_FAILED"]
    if policy and policy.degrade:
        degraded.append(str(policy.degrade))
    return ToolResult(
        tool=tool,
        ok=False,
        error_code=int(exc.code),
        user_message=(policy.user_message if policy else None) or None,
        degraded=degraded,
        latency_ms=latency_ms,
        attempts=max(1, attempts),
    )


def call_with_retry(
    tool: str,
    port_call: Any,
    *,
    sleep: Any = time.sleep,
    max_retries: int | None = None,
) -> tuple[Any, int]:
    """带退避地调用只读端口。

    重试策略严格对齐商家说明书与 `MERCHANT_ERROR_POLICIES`：

    * `RATE_LIMITED`（429）：优先服从 `Retry-After`；
    * `SERVICE_UNAVAILABLE` / `UPSTREAM_TIMEOUT`（503/504）：指数退避；
    * `INTERNAL_ERROR`：指数退避 1 次；
    * 其余（参数错 / 不存在 / 无权限）：**不重试**，直接失败。

    Args:
        tool: 工具名，用于取重试上限与打日志。
        port_call: 无参可调用，返回 `MerchantQueryResult` 或抛
            `MerchantChannelError`。
        sleep: 退避函数（测试注入假时钟）。
        max_retries: 覆盖默认重试上限。

    Returns:
        `(结果, 尝试次数)`。

    Raises:
        MerchantApiError: 重试耗尽或不可重试的错误。
    """
    limit = MAX_RETRIES.get(tool, 3) if max_retries is None else max_retries
    attempts = 0
    started = time.perf_counter()
    delay = 0.2
    while True:
        attempts += 1
        try:
            return port_call(), attempts
        except Exception as exc:  # noqa: BLE001 - 统一翻译成 MerchantApiError
            from app.adapters.merchant_message import MerchantChannelError

            if not isinstance(exc, MerchantChannelError):
                raise MerchantApiError(
                    code=_INTERNAL,
                    message=f"{type(exc).__name__}: {exc}",
                ) from exc
            if not exc.retryable or attempts > limit:
                raise _to_api_error(exc, attempts=attempts) from exc
            wait = exc.retry_after if exc.retry_after is not None else delay
            wait = min(float(wait), 5.0)
            sleep(wait)
            delay = min(delay * 2, 5.0)
    # 不可达
    raise AssertionError  # pragma: no cover


from app.core.errors import MerchantErrorCode as _EC  # noqa: E402

_INTERNAL = _EC.INTERNAL_ERROR


def _to_api_error(exc: Any, *, attempts: int = 1) -> MerchantApiError:
    """`MerchantChannelError` → `MerchantApiError`（保留 retry_after 与尝试次数）。"""
    error = MerchantApiError(
        code=exc.code,
        message=exc.message,
        http_status=exc.http_status,
        retry_after=exc.retry_after,
    )
    # `MerchantApiError` 没有 attempts 字段，但工具层需要它做可观测
    # （重试几次才失败直接决定「该不该告警」）。用属性挂上去，不改契约。
    error.attempts = attempts  # type: ignore[attr-defined]
    return error


def attempts_of(exc: Any, default: int = 1) -> int:
    """从异常上取尝试次数（没有就回退默认值）。"""
    return int(getattr(exc, "attempts", default) or default)


def masked_order_no(order_no: str) -> str:
    """订单号日志口径：前 4 后 4（BE-08 DoD③）。"""
    return mask_order_no(order_no)


def elapsed_ms(started: float) -> int:
    """从 `perf_counter` 起点算耗时毫秒。"""
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "MAX_RETRIES",
    "ToolResult",
    "attempts_of",
    "call_with_retry",
    "elapsed_ms",
    "masked_order_no",
    "result_from_error",
]
