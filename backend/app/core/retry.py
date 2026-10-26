"""重试与退避（BE-18）。

退避策略分三类，由调用方按错误类型选择：

| 策略 | 序列 | 适用 |
| --- | --- | --- |
| `exponential` | 0.5s, 1s, 2s, 4s（带抖动） | 50001 / 50301 / 50401 系统级故障 |
| `fixed_backoff` | 1s, 2s, 4s, 8s, 16s | **工单创建**（商家 5 QPS 全局瓶颈，任务书明确序列） |
| `retry_after` | 按商家响应头 `Retry-After` | 42901 限流 |

重试次数硬上限：工单 5 次（对应 5 个退避值），其余默认 3 次。
"""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: 工单创建固定退避序列（秒），任务书 BE-14 明确指定
TICKET_BACKOFF: tuple[int, ...] = (1, 2, 4, 8, 16)


def backoff_delay(
    attempt: int, *, base: float = 0.5, cap: float = 8.0, jitter: bool = True
) -> float:
    """计算指数退避延迟。

    Args:
        attempt: 第几次重试（从 1 开始）。
        base: 基数秒。
        cap: 单次延迟上限秒。
        jitter: 是否加 ±20% 抖动，避免惊群。

    Returns:
        延迟秒数。
    """
    delay = min(cap, base * (2 ** (attempt - 1)))
    if jitter:
        delay *= 1 + random.uniform(-0.2, 0.2)  # noqa: S311 - 非加密用途
    return max(0.0, delay)


def delay_from_sequence(attempt: int, sequence: Sequence[int]) -> float:
    """从固定序列取第 attempt 次重试的延迟（超出则取最后一个）。"""
    if not sequence:
        return 0.0
    idx = min(attempt - 1, len(sequence) - 1)
    return float(sequence[idx])


def retry_sync(
    *,
    max_attempts: int = 3,
    base: float = 0.5,
    cap: float = 8.0,
    sequence: Sequence[int] | None = None,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """同步重试装饰器。

    Args:
        max_attempts: 最大尝试次数（含首次）。
        base: 指数退避基数秒。
        cap: 单次延迟上限秒。
        sequence: 若提供，则按此固定序列退避（如工单 1/2/4/8/16）。
        exceptions: 触发重试的异常类型。
        on_retry: 每次重试前的回调 `(attempt, exc)`。

    Returns:
        装饰器。
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt >= max_attempts:
                        break
                    delay = (
                        delay_from_sequence(attempt, sequence)
                        if sequence
                        else backoff_delay(attempt, base=base, cap=cap)
                    )
                    logger.warning(
                        "同步调用失败，准备重试",
                        extra={
                            "extra": {
                                "fn": getattr(fn, "__qualname__", str(fn)),
                                "attempt": attempt,
                                "delay": round(delay, 3),
                                "error": type(exc).__name__,
                            }
                        },
                    )
                    if on_retry is not None:
                        on_retry(attempt, exc)
                    asyncio_run_sleep(delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


def asyncio_run_sleep(seconds: float) -> None:
    """同步上下文里的 sleep（不依赖事件循环）。"""
    import time

    time.sleep(seconds)


def retry_async(
    *,
    max_attempts: int = 3,
    base: float = 0.5,
    cap: float = 8.0,
    sequence: Sequence[int] | None = None,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException], Awaitable[None] | None] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """异步重试装饰器。参数语义同 `retry_sync`。"""

    def decorator(
        fn: Callable[..., Awaitable[T]]
    ) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt >= max_attempts:
                        break
                    delay = (
                        delay_from_sequence(attempt, sequence)
                        if sequence
                        else backoff_delay(attempt, base=base, cap=cap)
                    )
                    logger.warning(
                        "异步调用失败，准备重试",
                        extra={
                            "extra": {
                                "fn": getattr(fn, "__qualname__", str(fn)),
                                "attempt": attempt,
                                "delay": round(delay, 3),
                                "error": type(exc).__name__,
                            }
                        },
                    )
                    if on_retry is not None:
                        result = on_retry(attempt, exc)
                        if asyncio.iscoroutine(result):
                            await result
                    await asyncio.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    sequence: Sequence[int] | None = None,
    base: float = 0.5,
    cap: float = 8.0,
    should_retry: Callable[[BaseException], bool] | None = None,
    name: str = "call",
) -> T:
    """函数式重试（比装饰器更灵活：可按异常内容决定是否重试）。

    Args:
        fn: 无参协程工厂，每次重试重新调用。
        max_attempts: 最大尝试次数（含首次）。
        sequence: 固定退避序列，优先于指数退避。
        base: 指数退避基数秒。
        cap: 单次延迟上限秒。
        should_retry: 判断某异常是否值得重试；None 表示除 `asyncio.CancelledError`
            外都重试。
        name: 日志中的调用名。

    Returns:
        `fn` 的返回值。

    Raises:
        最后一次尝试的异常。
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_attempts:
                break
            if should_retry is not None and not should_retry(exc):
                break
            delay = (
                delay_from_sequence(attempt, sequence)
                if sequence
                else backoff_delay(attempt, base=base, cap=cap)
            )
            logger.warning(
                "调用失败，准备重试",
                extra={
                    "extra": {
                        "name": name,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay": round(delay, 3),
                        "error": type(exc).__name__,
                    }
                },
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


__all__ = [
    "TICKET_BACKOFF",
    "backoff_delay",
    "call_with_retry",
    "delay_from_sequence",
    "retry_async",
    "retry_sync",
]
