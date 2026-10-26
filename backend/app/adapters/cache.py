"""缓存适配器。

* `InMemoryCache`：进程内实现，**语义与 Redis 一致**（含 TTL、SETNX 原子性、
  INCRBY）。用于单测与本地无 Docker 环境。
* `RedisCache`：生产实现，对接任务书 3.1 节锁定的 Redis 7 主从。

工厂 `get_cache()` 按配置与可用性选择实现，**调用方无感知**。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.adapters.ports import CachePort
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class InMemoryCache:
    """进程内缓存，Redis 语义的等价实现。

    线程/协程安全：所有变更操作在事件循环内通过 `asyncio.Lock` 串行化，
    保证 `setnx` / `incrby` 的原子性。

    注意：**仅适用于单进程**。多 worker 部署必须用 `RedisCache`，
    否则幂等与限流会失效。
    """

    def __init__(self, *, name: str = "memory") -> None:
        self._name = name
        #: key -> (value, expire_at 或 None)
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    # -- 内部 ---------------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic()

    def _expire_if_needed(self, key: str) -> None:
        """惰性过期：读取时检查并删除。"""
        item = self._data.get(key)
        if item is None:
            return
        _, expire_at = item
        if expire_at is not None and expire_at <= self._now():
            del self._data[key]

    # -- CachePort ----------------------------------------------------------
    async def get(self, key: str) -> str | None:
        """读取字符串值。"""
        async with self._lock:
            self._expire_if_needed(key)
            item = self._data.get(key)
            return item[0] if item else None

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """写入字符串值。"""
        expire_at = self._now() + ttl if ttl else None
        async with self._lock:
            self._data[key] = (value, expire_at)

    async def delete(self, key: str) -> None:
        """删除键。"""
        async with self._lock:
            self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        """键是否存在。"""
        async with self._lock:
            self._expire_if_needed(key)
            return key in self._data

    async def setnx(self, key: str, value: str, *, ttl: int | None = None) -> bool:
        """仅当键不存在时写入。"""
        async with self._lock:
            self._expire_if_needed(key)
            if key in self._data:
                return False
            self._data[key] = (value, self._now() + ttl if ttl else None)
            return True

    async def incrby(
        self, key: str, amount: int = 1, *, ttl: int | None = None
    ) -> int:
        """原子自增。首次创建时应用 ttl。"""
        async with self._lock:
            self._expire_if_needed(key)
            item = self._data.get(key)
            if item is None:
                new_val = amount
                self._data[key] = (str(new_val), self._now() + ttl if ttl else None)
                return new_val
            new_val = int(item[0]) + amount
            self._data[key] = (str(new_val), item[1])
            return new_val

    async def expire(self, key: str, ttl: int) -> None:
        """设置过期时间。"""
        async with self._lock:
            item = self._data.get(key)
            if item is not None:
                self._data[key] = (item[0], self._now() + ttl)

    async def get_json(self, key: str) -> Any | None:
        """读取 JSON 值。"""
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("缓存值不是合法 JSON，已忽略", extra={"extra": {"key": key}})
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """写入 JSON 值。"""
        await self.set(key, json.dumps(value, ensure_ascii=False), ttl=ttl)

    async def health(self) -> bool:
        """内存实现恒为健康。"""
        return True

    async def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀的所有键，返回删除条数。

        Args:
            prefix: 键前缀。

        Returns:
            实际删除的键数量。
        """
        async with self._lock:
            doomed = [k for k in self._data if k.startswith(prefix)]
            for key in doomed:
                self._data.pop(key, None)
            return len(doomed)

    async def clear(self) -> None:
        """清空（测试用）。"""
        async with self._lock:
            self._data.clear()


class RedisCache:
    """Redis 7 生产实现。

    使用 `redis.asyncio`。所有 TTL 由 Redis 侧管理，多 worker 间共享。
    """

    def __init__(self, url: str, *, socket_timeout: float = 2.0) -> None:
        self._url = url
        self._socket_timeout = socket_timeout
        #: **按事件循环缓存客户端**。`redis.asyncio` 的连接池把 socket 绑在
        #: 「创建它那个 loop」上，跨 loop 复用会在 `transport.close()` 里抛
        #: `RuntimeError: Event loop is closed`。同步业务代码经 `run_maybe_async`
        #: 驱动时可能落在不同线程/不同 loop 上，所以一个进程里存在多个 loop 是常态。
        self._clients: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _get_client(self) -> Any:
        """惰性建立连接（每个事件循环一个客户端）。"""
        loop = asyncio.get_running_loop()
        key = id(loop)
        client = self._clients.get(key)
        if client is not None:
            return client

        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            client = self._clients.get(key)
            if client is None:
                import redis.asyncio as aioredis  # 延迟导入，未装 redis 时不影响内存实现

                client = aioredis.from_url(
                    self._url,
                    socket_timeout=self._socket_timeout,
                    decode_responses=True,
                )
                self._clients[key] = client
        return client

    async def get(self, key: str) -> str | None:
        """读取字符串值。"""
        client = await self._get_client()
        return await client.get(key)

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """写入字符串值。"""
        client = await self._get_client()
        await client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        """删除键。"""
        client = await self._get_client()
        await client.delete(key)

    async def exists(self, key: str) -> bool:
        """键是否存在。"""
        client = await self._get_client()
        return bool(await client.exists(key))

    async def setnx(self, key: str, value: str, *, ttl: int | None = None) -> bool:
        """SET NX EX 原子写入。"""
        client = await self._get_client()
        result = await client.set(key, value, nx=True, ex=ttl)
        return bool(result)

    async def incrby(
        self, key: str, amount: int = 1, *, ttl: int | None = None
    ) -> int:
        """INCRBY；首次创建时补设 TTL。"""
        client = await self._get_client()
        new_val = await client.incrby(key, amount)
        if ttl and new_val == amount:
            await client.expire(key, ttl)
        return int(new_val)

    async def expire(self, key: str, ttl: int) -> None:
        """设置过期时间。"""
        client = await self._get_client()
        await client.expire(key, ttl)

    async def get_json(self, key: str) -> Any | None:
        """读取 JSON 值。"""
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("缓存值不是合法 JSON，已忽略", extra={"extra": {"key": key}})
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """写入 JSON 值。"""
        await self.set(key, json.dumps(value, ensure_ascii=False), ttl=ttl)

    async def health(self) -> bool:
        """PING 探测。"""
        try:
            client = await self._get_client()
            return bool(await client.ping())
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("Redis 健康检查失败：%s", type(exc).__name__)
            return False

    async def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀的所有键，返回删除条数。

        用 `SCAN` 而非 `KEYS`：`KEYS` 是 O(N) 阻塞命令，
        在百万级 key 的生产 Redis 上会卡住整个实例。

        Args:
            prefix: 键前缀。

        Returns:
            实际删除的键数量。
        """
        client = await self._get_client()
        deleted = 0
        async for key in client.scan_iter(match=f"{prefix}*", count=200):
            deleted += int(await client.delete(key))
        return deleted


#: 进程级单例
_CACHE: CachePort | None = None


def get_cache() -> CachePort:
    """获取缓存实例（单例）。

    策略：`WK_ENV=local` 或 Redis 不可达时用 `InMemoryCache`；
    其余环境用 `RedisCache`。可通过 `set_cache()` 在测试中替换。
    """
    global _CACHE  # noqa: PLW0603
    if _CACHE is None:
        settings = get_settings()
        if settings.env == "local":
            logger.info("缓存实现：InMemoryCache（本地环境）")
            _CACHE = InMemoryCache()
        else:
            logger.info("缓存实现：RedisCache")
            _CACHE = RedisCache(
                settings.redis_url, socket_timeout=settings.redis_socket_timeout
            )
    return _CACHE


def set_cache(cache: CachePort | None) -> None:
    """替换缓存实例（测试用）。传 None 重置。"""
    global _CACHE  # noqa: PLW0603
    _CACHE = cache


__all__ = ["InMemoryCache", "RedisCache", "get_cache", "set_cache"]
