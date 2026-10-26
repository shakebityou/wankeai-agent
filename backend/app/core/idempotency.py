"""幂等框架（BE-18）。

三类幂等场景，**key 维度不同**（这是实现要点，不要混用）：

| 场景 | key 组成 | TTL | 重复时行为 |
| --- | --- | --- | --- |
| 渠道回调去重 | `callback:{channel}:{msg_id}` | 24h | 直接返回 200，**不重复处理** |
| 出站消息幂等 | `outbound:{msg_id}` | 24h | 复用首次结果，不重复发送 |
| 工单创建幂等 | `ticket:{session_id}` | 7d | 返回**首次**工单号，不新建 |

实现用 Redis `SET NX EX`（原子）。首次写入者成为"处理者"，其余视为重复。
处理结果缓存后，重复请求可直接返回首次结果（`IdempotencyStore.get_result`）。
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.adapters.ports import CachePort
from app.core.logging import get_logger
from app.core.trace import new_request_id

logger = get_logger(__name__)

T = TypeVar("T")

#: 幂等状态：处理中（尚未写入结果）
STATUS_PROCESSING = "PROCESSING"
#: 幂等状态：已完成
STATUS_DONE = "DONE"


def build_key(namespace: str, *parts: Any) -> str:
    """构造幂等键：`{namespace}:{sha1(parts)[:16]}`。

    对参与键的各部分做稳定拼接后取哈希，避免 key 过长或含非法字符。

    Args:
        namespace: 命名空间，如 `callback` / `outbound` / `ticket`。
        *parts: 参与键的字段，按顺序拼接（None 被跳过）。

    Returns:
        幂等键字符串。
    """
    raw = "|".join(str(p) for p in parts if p is not None)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


class IdempotencyStore:
    """幂等存储。

    Attributes:
        cache: 底层缓存端口。
        ttl: 默认 TTL（秒）。
    """

    def __init__(self, cache: CachePort | None = None, *, ttl: int = 86400) -> None:
        if cache is None:
            # 延迟导入：避免 app.core ↔ app.adapters 的循环导入
            from app.adapters.cache import get_cache

            cache = get_cache()
        self.cache = cache
        self.ttl = ttl

    async def acquire(self, key: str, *, ttl: int | None = None) -> bool:
        """尝试取得处理权。

        Args:
            key: 幂等键（用 `build_key` 构造）。
            ttl: 覆盖默认 TTL。

        Returns:
            True 表示首次（本次应处理）；False 表示重复（应跳过处理）。
        """
        payload = json.dumps(
            {"status": STATUS_PROCESSING, "request_id": new_request_id()},
            ensure_ascii=False,
        )
        ok = await self.cache.setnx(key, payload, ttl=ttl or self.ttl)
        if not ok:
            logger.info(
                "幂等命中，跳过重复处理",
                extra={"extra": {"idempotency_key": key}},
            )
        return ok

    async def is_duplicate(self, key: str) -> bool:
        """只查询不占位（用于回调"先查后判"场景）。"""
        return await self.cache.exists(key)

    async def mark_done(self, key: str, result: Any, *, ttl: int | None = None) -> None:
        """标记处理完成并缓存结果，供重复请求复用。

        Args:
            key: 幂等键。
            result: 可 JSON 序列化的处理结果。
            ttl: 覆盖默认 TTL。
        """
        await self.cache.set_json(
            key,
            {"status": STATUS_DONE, "result": result},
            ttl=ttl or self.ttl,
        )

    async def get_result(self, key: str) -> Any | None:
        """读取已完成的结果；处理中或不存在返回 None。

        Returns:
            首次处理的结果，或 None。
        """
        data = await self.cache.get_json(key)
        if not isinstance(data, dict):
            return None
        if data.get("status") == STATUS_DONE:
            return data.get("result")
        return None

    async def release(self, key: str) -> None:
        """释放占位（处理失败时调用，允许后续重试）。"""
        await self.cache.delete(key)


def idempotent(
    namespace: str,
    *,
    key_fields: tuple[str, ...] = ("msg_id",),
    ttl: int | None = None,
    return_cached: bool = True,
) -> Callable[..., Any]:
    """幂等装饰器（异步函数）。

    装饰后的函数在**首次调用时正常执行**，重复调用时：
        * `return_cached=True` 且已有结果 → 直接返回缓存结果；
        * 否则返回 None（由调用方决定如何响应，例如回调直接返回 200）。

    Args:
        namespace: 幂等命名空间。
        key_fields: 从 kwargs 中取哪些字段参与构造幂等键。
        ttl: 幂等 TTL（秒）。
        return_cached: 重复时是否返回首次结果。

    Example:
        >>> @idempotent("outbound", key_fields=("msg_id",))
        ... async def send_message(msg_id: str, content: str) -> dict:
        ...     ...
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T | None:
            store = IdempotencyStore(ttl=ttl or 86400)
            parts = [kwargs.get(f) for f in key_fields]
            if all(p is None for p in parts):
                # 未提供幂等字段时退化为直接执行，避免误吞请求
                logger.warning(
                    "幂等装饰器未取到 key 字段，已跳过幂等保护",
                    extra={"extra": {"namespace": namespace, "fields": list(key_fields)}},
                )
                return await fn(*args, **kwargs)

            key = build_key(namespace, *parts)
            if not await store.acquire(key, ttl=ttl):
                if return_cached:
                    cached = await store.get_result(key)
                    if cached is not None:
                        return cached  # type: ignore[return-value]
                return None

            try:
                result = await fn(*args, **kwargs)
            except Exception:
                # 处理失败 → 释放占位，允许下游重试
                await store.release(key)
                raise

            if result is not None:
                await store.mark_done(key, result, ttl=ttl)
            return result

        return wrapper

    return decorator


__all__ = [
    "STATUS_DONE",
    "STATUS_PROCESSING",
    "IdempotencyStore",
    "build_key",
    "idempotent",
]
