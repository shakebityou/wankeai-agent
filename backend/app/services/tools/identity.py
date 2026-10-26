"""BE-08 身份绑定与越权防护。

任务书 BE-08 三条硬要求：

1. **三种绑定方式，按优先级**：① 渠道身份透传（回调带的 `buyer_id`）
   ② 会话内已提供（缓存到会话实体）③ 用户主动提供（订单号 / 截图 OCR）；
2. **每次查询强制校验 `buyer_id` 与订单归属一致**，不一致 100% 拒绝；
3. **单会话 20 次/小时**，超频给友好提示而不是报错。

## 为什么越权校验放在工具层而不是接口层

越权判定需要**订单归属数据**，而订单归属只有查过商家 1 号接口才知道。
放在 HTTP 层就得每个路由各查一遍；放在工具层则四个读接口共用一套
`ensure_access()`，且编排层（`orchestrator` 走 `registry.invoke`）与
内部接口（`/internal/v1/tools/order`）走的是同一条校验路径——
**这是「越权 100% 拒绝」DoD 的唯一可靠实现方式**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger, mask_order_no

logger = get_logger(__name__)

#: 单会话订单查询频次上限（次/小时），对应 `settings.order_query_hourly_limit`。
DEFAULT_HOURLY_LIMIT = 20
#: 频次计数窗口（秒）。
RATE_WINDOW_SECONDS = 3600


class AccessDeniedError(PermissionError):
    """越权访问（订单不属于当前买家）。

    Attributes:
        reason: 拒绝原因码，供上层映射话术。
        user_message: 用户侧话术。
    """

    def __init__(self, message: str = "", *, reason: str = "NOT_OWNER", user_message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.user_message = user_message or "这个订单不在当前账号下，请用下单账号咨询哦～"


class RateLimitedError(PermissionError):
    """单会话查询超频。

    Attributes:
        retry_after: 建议退避秒数（窗口重置时间）。
        user_message: 用户侧话术（**友好提示，不是报错**）。
    """

    def __init__(self, message: str = "", *, retry_after: int = 60, user_message: str = "") -> None:
        super().__init__(message or "rate_limited")
        self.retry_after = retry_after
        self.user_message = user_message or "查询有点频繁，休息一下再看哦～"


@dataclass
class BuyerContext:
    """一次查询所需的买家上下文（三种绑定方式的归一结果）。

    Attributes:
        buyer_id: 买家标识。**空表示未绑定**，此时任何订单查询都必须先绑定。
        source: 绑定来源，`channel` / `session` / `user` / `none`。
        session_id: 会话 ID（频次计数用）。
        channel: 渠道（`TAOBAO` 等）。
    """

    buyer_id: str = ""
    source: str = "none"
    session_id: str = ""
    channel: str = ""


class SessionRateCounter:
    """单会话频次计数器（滑动窗口，内存实现）。

    本机 / 单测用内存字典；生产多实例部署时由 `set_session_rate_counter`
    换成 Redis 实现（端口方法已按 Redis 的数据结构设计，见 `count()`）。
    """

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}

    def count(self, session_id: str, *, now: float | None = None) -> int:
        """返回窗口内已发生的查询次数（含本次之前的）。"""
        moment = time.time() if now is None else now
        events = [
            t for t in self._events.get(session_id, []) if moment - t < RATE_WINDOW_SECONDS
        ]
        self._events[session_id] = events
        return len(events)

    def record(self, session_id: str, *, now: float | None = None) -> int:
        """记录一次查询，返回窗口内累计次数。"""
        moment = time.time() if now is None else now
        events = [
            t for t in self._events.get(session_id, []) if moment - t < RATE_WINDOW_SECONDS
        ]
        events.append(moment)
        self._events[session_id] = events
        return len(events)

    def reset(self, session_id: str | None = None) -> None:
        """清空计数（测试用）。"""
        if session_id is None:
            self._events.clear()
        else:
            self._events.pop(session_id, None)


_counter: SessionRateCounter | None = None


def get_session_rate_counter() -> SessionRateCounter:
    """取全局频次计数器单例。"""
    global _counter  # noqa: PLW0603
    if _counter is None:
        _counter = SessionRateCounter()
    return _counter


def set_session_rate_counter(counter: SessionRateCounter | None) -> None:
    """替换全局计数器（**测试专用**）。"""
    global _counter  # noqa: PLW0603
    _counter = counter


def check_rate(
    ctx: BuyerContext,
    *,
    hourly_limit: int = DEFAULT_HOURLY_LIMIT,
    counter: SessionRateCounter | None = None,
    now: float | None = None,
) -> None:
    """校验单会话频次，超频抛 `RateLimitedError`。

    先计数后放行：本方法在**查询真正发起前**调用，因此「超频的请求」
    不会打到商家接口，也不会占用窗口额度。
    """
    if not ctx.session_id:
        return
    counter = counter or get_session_rate_counter()
    used = counter.count(ctx.session_id, now=now)
    if used >= hourly_limit:
        raise RateLimitedError(retry_after=RATE_WINDOW_SECONDS)


def ensure_order_access(
    order: dict[str, Any],
    ctx: BuyerContext,
    *,
    counter: SessionRateCounter | None = None,
    now: float | None = None,
) -> None:
    """校验「当前买家是否有权查这条订单」并记一次频次。

    这是 BE-08 的核心：**查到订单后必须比对 `buyer_id`**，只看订单号能查
    到就返回，等于把整个订单库开放给任意买家。

    Args:
        order: 商家 1 号接口返回的 `data`（含 `order_no` 与 `buyer_id`）。
        ctx: 买家上下文。
        counter: 频次计数器（测试注入）。
        now: 当前时间（测试注入假时钟）。

    Raises:
        AccessDeniedError: 未绑定身份，或订单归属与当前买家不一致。
        RateLimitedError: 单会话查询超频。
    """
    order_no = str(order.get("order_no") or "")
    owner = str(order.get("buyer_id") or "")

    if not ctx.buyer_id:
        logger.warning(
            "订单查询被拒绝：买家未绑定",
            extra={"extra": {"order_no": mask_order_no(order_no), "reason": "UNBOUND"}},
        )
        raise AccessDeniedError(reason="UNBOUND")

    if not owner or owner != ctx.buyer_id:
        logger.warning(
            "订单查询被拒绝：越权",
            extra={
                "extra": {
                    "order_no": mask_order_no(order_no),
                    "reason": "NOT_OWNER",
                    "source": ctx.source,
                }
            },
        )
        raise AccessDeniedError(reason="NOT_OWNER")

    if ctx.session_id:
        counter = counter or get_session_rate_counter()
        counter.record(ctx.session_id, now=now)


def resolve_buyer(
    *,
    channel_buyer_id: str = "",
    session_buyer_id: str = "",
    user_buyer_id: str = "",
    session_id: str = "",
    channel: str = "",
) -> BuyerContext:
    """三种绑定方式按优先级归一。

    优先级（任务书 BE-08）：① 渠道身份透传 > ② 会话内已提供 > ③ 用户主动提供。

    Args:
        channel_buyer_id: 渠道回调透传的买家标识（最可信，无需用户操作）。
        session_buyer_id: 会话缓存里已绑定的买家标识。
        user_buyer_id: 用户本轮主动提供的买家标识 / 订单号对应的归属。
        session_id: 会话 ID。
        channel: 渠道。

    Returns:
        `BuyerContext`，`source` 记录实际生效的绑定方式。
    """
    if channel_buyer_id:
        return BuyerContext(
            buyer_id=channel_buyer_id, source="channel", session_id=session_id, channel=channel
        )
    if session_buyer_id:
        return BuyerContext(
            buyer_id=session_buyer_id, source="session", session_id=session_id, channel=channel
        )
    if user_buyer_id:
        return BuyerContext(
            buyer_id=user_buyer_id, source="user", session_id=session_id, channel=channel
        )
    return BuyerContext(buyer_id="", source="none", session_id=session_id, channel=channel)


__all__ = [
    "AccessDeniedError",
    "BuyerContext",
    "DEFAULT_HOURLY_LIMIT",
    "RATE_WINDOW_SECONDS",
    "RateLimitedError",
    "SessionRateCounter",
    "check_rate",
    "ensure_order_access",
    "get_session_rate_counter",
    "resolve_buyer",
    "set_session_rate_counter",
]
