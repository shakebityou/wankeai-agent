"""跨 async 边界的统一驱动器。

**这是全项目最容易被忽视的一个坑，所以单独成模块。**

项目的适配器层（缓存、向量库、稀疏索引、Embedding、LLM Provider）
早期都是 `async def`，而业务层是同步的。同步代码直接调用 async 函数
时**不会报错**——它只是返回一个协程对象，然后被扔掉，GC 时打一条
`RuntimeWarning: coroutine ... was never awaited`。异常被 `except
Exception` 吞掉后，表现就是「索引没写进去」「缓存没清掉」，而日志里
什么都没发生。

更麻烦的是第二种情形：业务代码里已经有事件循环（FastAPI 的 async 端点），
此时 `asyncio.run()` 会直接抛
`RuntimeError: asyncio.run() cannot be called from a running event loop`，
同样被 `except Exception` 吞掉——**静默失效**。

所以规定：**任何跨 async 边界的调用都必须走 `run_maybe_async`**。

为什么放在 `app/core` 而不是 `app.services.knowledge.service`
-----------------------------------------------------------------
它原本住在知识库服务里，结果每个模块第一次调用都要
`from app.services.knowledge.service import _run_maybe_async`，
等于把整个知识库服务（jieba 词典、向量库适配器）加载一遍。
实测冷启动时这一步要 800ms，直接压垮了会话读状态的延迟预算。
基础设施不该寄生在业务模块里。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any, TypeVar

T = TypeVar("T")


#: 每线程一个常驻事件循环。
#:
#: **为什么不能用 `asyncio.run()`**：`asyncio.run()` 每次调用都新建一个 loop，
#: 跑完就 `close()`。而适配器层（`RedisCache`、`ElasticsearchIndex`）持有的
#: 异步客户端把连接池绑在「第一次用它那个 loop」上，loop 一关，连接对象里的
#: `_loop` 就成了死循环；下一次调用再 `asyncio.run()` 复用池里的旧连接，就会在
#: `transport.close()` 里炸出 `RuntimeError: Event loop is closed`。
#:
#: 这个 bug 只在**真实部署**下暴露：单测全部用 `InMemoryCache` 等本地实现，
#: 根本没有异步客户端，所以 1111 个用例全绿也测不到。
#:
#: 改成线程内常驻 loop 后，同一线程的所有同步→异步调用共用同一个 loop，
#: 连接池里的连接始终有效；线程退出时 loop 随线程一起回收。
_LOCAL = threading.local()


def _thread_loop() -> asyncio.AbstractEventLoop:
    """取当前线程的常驻事件循环（没有就建一个）。"""
    loop = getattr(_LOCAL, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _LOCAL.loop = loop
    return loop


def run_maybe_async(result: Any) -> Any:
    """驱动可能是协程的返回值。

    Args:
        result: 可能是 awaitable，也可能就是普通值。

    Returns:
        协程的返回值，或普通值本身。

    Raises:
        协程内部抛出的任何异常（**原样抛出，不吞**）。

    Notes:
        三个分支：
        1. 不是 awaitable → 原样返回（绝大多数适配器的本地实现是同步的）；
        2. 没有运行中的事件循环 → 用**当前线程的常驻 loop** `run_until_complete`；
        3. **有运行中的事件循环**（FastAPI async 端点里）→
           在独立线程里新开一个 loop 执行，结果经闭包变量回传。
           不能在这个 loop 上再 `run_until_complete`（会报
           `This event loop is already running`），也不能 `asyncio.run`。
    """
    if not inspect.isawaitable(result):
        return result

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 分支 2：没有运行中的 loop —— 用线程常驻 loop，**不能**用 asyncio.run
        loop = _thread_loop()
        if loop.is_running():  # pragma: no cover - 理论上到不了
            return asyncio.run(result)
        return loop.run_until_complete(result)

    # 分支 3：已有运行中的 loop，换线程执行
    box: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(result)
        except BaseException as exc:  # noqa: BLE001 - 原样转交给调用线程
            error["exc"] = exc

    thread = threading.Thread(target=_runner, name="run-maybe-async", daemon=True)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]
    return box.get("value")


#: 兼容旧名（曾有 `_run_maybe_async` 这个私有名散布在各模块）
_run_maybe_async = run_maybe_async

__all__ = ["run_maybe_async", "_run_maybe_async"]
