"""限流与熔断（BE-18）。

**限流**：令牌桶算法，按商家各接口 QPS 独立配置。

| 接口 | 生产 QPS | 测试环境 QPS（1/5） |
| --- | --- | --- |
| 订单查询 | 20 | 4 |
| 物流查询 | 20 | 4 |
| 库存查询 | 50 | 10 |
| 商品查询 | 30 | 6 |
| 工单创建 | 5 | 1 |
| 消息发送 | 30 | 6 |

**熔断**：连续失败达阈值 → 打开 30 秒（期间快速失败）→ 半开探测（放行 1 个请求，
成功则闭合，失败则重新打开）。对应 BE-18 DoD「熔断 30 秒内开」。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.errors import BffErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)


class RateLimitExceeded(Exception):
    """触发限流。

    Attributes:
        retry_after: 建议等待秒数，会写入 HTTP `Retry-After` 响应头。
    """

    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(f"rate limit exceeded, retry after {retry_after:.2f}s")
        self.retry_after = retry_after
        self.code = BffErrorCode.TOO_MANY_REQUESTS


@dataclass
class TokenBucketRateLimiter:
    """令牌桶限流器。

    每个限流维度（如 `order_query:{app_id}`）一个桶。桶容量 = QPS，
    每秒补充 QPS 个令牌，允许小幅突发（容量即突发上限）。

    Attributes:
        rate: 每秒补充令牌数（即 QPS）。
        capacity: 桶容量（突发上限），默认等于 rate。
    """

    rate: float
    capacity: float | None = None
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.capacity is None:
            self.capacity = float(self.rate)

    async def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        """尝试取令牌（不阻塞）。

        Args:
            key: 限流维度键，如 `order_query`。
            tokens: 本次消耗令牌数。

        Returns:
            True 表示放行。
        """
        async with self._lock:
            now = time.monotonic()
            capacity = float(self.capacity or self.rate)
            level, last = self._buckets.get(key, (capacity, now))
            # 按经过时间补充
            level = min(capacity, level + (now - last) * self.rate)
            if level >= tokens:
                self._buckets[key] = (level - tokens, now)
                return True
            self._buckets[key] = (level, now)
            return False

    async def acquire(self, key: str, tokens: float = 1.0) -> None:
        """取令牌，取不到则抛 `RateLimitExceeded`。

        Args:
            key: 限流维度键。
            tokens: 本次消耗令牌数。

        Raises:
            RateLimitExceeded: 桶内令牌不足。
        """
        if not await self.try_acquire(key, tokens):
            # 需要等待的时间：补满 tokens 个令牌所需时间
            deficit = tokens - self._buckets.get(key, (0.0, 0.0))[0]
            retry_after = max(0.05, deficit / self.rate)
            logger.warning(
                "触发限流",
                extra={"extra": {"bucket": key, "retry_after": round(retry_after, 3)}},
            )
            raise RateLimitExceeded(retry_after)

    def reset(self) -> None:
        """清空所有桶（测试用）。"""
        self._buckets.clear()


class CircuitBreaker:
    """熔断器。

    状态机：`CLOSED` --失败达阈值--> `OPEN` --等待 30s--> `HALF_OPEN`
    --探测成功--> `CLOSED` / --探测失败--> `OPEN`。

    Attributes:
        name: 熔断器名称（被保护的下游，如 `merchant.order_query`）。
        failure_threshold: 连续失败阈值。
        open_seconds: 打开时长（秒）。
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        open_seconds: int = 30,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()
        #: 半开状态下已放行的探测请求数
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        """当前状态（考虑时间推移自动从 OPEN 迁到 HALF_OPEN）。"""
        if (
            self._state == self.OPEN
            and time.monotonic() - self._opened_at >= self.open_seconds
        ):
            return self.HALF_OPEN
        return self._state

    async def allow(self) -> bool:
        """判断本次调用是否放行。

        Returns:
            True 放行；False 快速失败（熔断打开中）。
        """
        async with self._lock:
            state = self.state
            if state == self.CLOSED:
                return True
            if state == self.OPEN:
                logger.warning(
                    "熔断器打开，快速失败",
                    extra={"extra": {"breaker": self.name}},
                )
                return False
            # HALF_OPEN：只放行一个探测请求
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            self._state = self.HALF_OPEN
            return True

    async def record_success(self) -> None:
        """记录成功：闭合熔断器。"""
        async with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            if self._state != self.CLOSED:
                logger.info(
                    "熔断器闭合（下游恢复）",
                    extra={"extra": {"breaker": self.name}},
                )
            self._state = self.CLOSED

    async def record_failure(self) -> None:
        """记录失败：达阈值则打开。"""
        async with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "熔断器打开",
                    extra={
                        "extra": {
                            "breaker": self.name,
                            "failures": self._failures,
                            "open_seconds": self.open_seconds,
                        }
                    },
                )

    def reset(self) -> None:
        """重置为闭合（测试用）。"""
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        """导出当前状态，供 `/health` 与监控看板展示。"""
        return {
            "name": self.name,
            "state": self.state,
            "failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "open_seconds": self.open_seconds,
        }


#: 限流器单例（按接口维度分桶）
_LIMITER: TokenBucketRateLimiter | None = None
#: 熔断器注册表
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_rate_limiter() -> TokenBucketRateLimiter:
    """获取全局限流器。

    注意：不同接口 QPS 差异很大（库存 50 vs 工单 5），因此**桶键必须带接口名**，
    速率取该接口配置值 —— 这里用最大速率建桶，实际速率由 `acquire_for()` 查表决定。
    """
    global _LIMITER  # noqa: PLW0603
    if _LIMITER is None:
        settings = get_settings()
        _LIMITER = TokenBucketRateLimiter(
            rate=float(max(settings.effective_qps.values()) or 1)
        )
    return _LIMITER


class PerEndpointRateLimiter:
    """按接口维度分别建桶的限流器（生产使用）。

    与 `TokenBucketRateLimiter` 的区别：每个接口独立速率与容量，
    避免「库存 50 QPS 把工单 5 QPS 的桶撑大」这类串扰。
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._qps = settings.effective_qps
        self._buckets: dict[str, TokenBucketRateLimiter] = {}

    def limiter_for(self, endpoint: str) -> TokenBucketRateLimiter:
        """获取某接口的限流器，不存在则按配置创建。

        Args:
            endpoint: 接口键，如 `order_query`。

        Returns:
            该接口专属的令牌桶。
        """
        if endpoint not in self._buckets:
            qps = float(self._qps.get(endpoint, 10))
            self._buckets[endpoint] = TokenBucketRateLimiter(rate=qps, capacity=qps)
        return self._buckets[endpoint]

    async def acquire(self, endpoint: str, tokens: float = 1.0) -> None:
        """按接口限流，超出则抛 `RateLimitExceeded`。"""
        await self.limiter_for(endpoint).acquire(endpoint, tokens)

    async def try_acquire(self, endpoint: str, tokens: float = 1.0) -> bool:
        """按接口限流，返回是否放行（不抛异常）。"""
        return await self.limiter_for(endpoint).try_acquire(endpoint, tokens)

    def snapshot(self) -> dict[str, int]:
        """导出各接口配置 QPS。"""
        return dict(self._qps)


_PER_ENDPOINT: PerEndpointRateLimiter | None = None


def get_per_endpoint_limiter() -> PerEndpointRateLimiter:
    """获取按接口分桶的限流器单例。"""
    global _PER_ENDPOINT  # noqa: PLW0603
    if _PER_ENDPOINT is None:
        _PER_ENDPOINT = PerEndpointRateLimiter()
    return _PER_ENDPOINT


def get_breaker(name: str) -> CircuitBreaker:
    """获取（或创建）命名熔断器。

    Args:
        name: 熔断器名，建议格式 `{下游}.{操作}`，如 `merchant.order_query`。

    Returns:
        该名称对应的熔断器单例。
    """
    if name not in _BREAKERS:
        settings = get_settings()
        _BREAKERS[name] = CircuitBreaker(
            name,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            open_seconds=settings.circuit_breaker_open_seconds,
        )
    return _BREAKERS[name]


def all_breakers_snapshot() -> list[dict[str, Any]]:
    """导出所有熔断器状态，供健康检查与运维看板。"""
    return [b.snapshot() for b in _BREAKERS.values()]


def reset_all() -> None:
    """重置全部限流与熔断状态（测试用）。"""
    global _LIMITER, _PER_ENDPOINT  # noqa: PLW0603
    _LIMITER = None
    _PER_ENDPOINT = None
    for breaker in _BREAKERS.values():
        breaker.reset()


__all__ = [
    "CircuitBreaker",
    "PerEndpointRateLimiter",
    "RateLimitExceeded",
    "TokenBucketRateLimiter",
    "all_breakers_snapshot",
    "get_breaker",
    "get_per_endpoint_limiter",
    "get_rate_limiter",
    "reset_all",
]
