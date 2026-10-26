"""渠道回调接入网关（契约 2.3.4，BE-01）。

## 三条 DoD 对应的代码位置

| DoD | 实现位置 |
|---|---|
| 500 并发回调 3 秒内 100% 响应 | `callback()` 只做「验签 → 解析 → 幂等占位 → 入队」四件事，**编排不在请求线程里跑** |
| 伪造签名 100% 丢弃 | `CallbackVerifier.verify()`，失败**仍返 200**（契约要求）但 `discarded=True` |
| 同 msg_id 重推 3 次只处理 1 次 | `IdempotencyStore.acquire()`，key = `callback:msg:<channel>:<msg_id>`，TTL 24h |

## 为什么验签失败也返回 200

契约 2.3.4 明确要求：「签名校验失败返 200 但丢弃」。原因是渠道侧看到
非 200 会持续重推，一条伪造报文就能把我们的重推队列打爆；返 200 +
本地丢弃 + 安全告警，比「拒绝」更符合平台的补偿语义。

## 为什么幂等占位在入队之前

顺序反了（先入队再占位）的后果是：并发重推时两条消息都进了队列，
靠消费者再去重就晚了一步——消费者可能已经跑完一次编排，重复回复
已经在用户侧可见。先占位再入队，把去重成本压到一次 Redis SETNX。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import Channel
from app.core.idempotency import IdempotencyStore
from app.core.logging import get_logger, mask_text
from app.core.signature import SignatureError, get_callback_verifier
from app.core.trace import new_trace_id, trace_context
from app.services.channels import ChannelParseError, RawInbound, get_adapter

logger = get_logger(__name__)

#: 回调幂等键前缀。TTL 走 `idempotency_ttl_seconds`（默认 24h，契约 2.3.4）
CALLBACK_IDEMPOTENCY_PREFIX = "callback:msg"

#: 本地待处理队列的键（Celery 不可用时由进程内队列兜底）
INBOX_QUEUE_KEY = "wk:inbox"


@dataclass
class CallbackOutcome:
    """一次回调处理的结果。

    这个对象**就是回调路由的返回值**，也是 DoD 断言的对象，所以字段要
    能直接回答三个问题：收了几条 / 丢了几条 / 为什么丢。
    """

    #: HTTP 状态（**永远是 200**，除非是我们自己的 bug）
    http_status: int = 200
    #: 收到的消息条数（解析成功的）
    received: int = 0
    #: 新入队条数
    enqueued: int = 0
    #: 因幂等被丢弃的条数
    duplicated: int = 0
    #: 因验签失败被丢弃的条数
    rejected: int = 0
    #: 验签失败原因码
    reject_reason: str = ""
    #: 解析失败原因
    parse_error: str = ""
    #: 入队消息（调试与断言用；生产由队列消费）
    messages: list[RawInbound] = field(default_factory=list)
    #: 处理耗时（毫秒）—— DoD 要求 3 秒内，实测会断言这个值
    latency_ms: float = 0.0
    trace_id: str = ""

    @property
    def discarded(self) -> int:
        """被丢弃的总条数。"""
        return self.duplicated + self.rejected

    def to_dict(self) -> dict[str, Any]:
        """契约应答体（给渠道看的，极简）。"""
        return {"errcode": 0, "errmsg": "ok", "success": True}


class CallbackGateway:
    """回调接入网关。"""

    def __init__(
        self,
        *,
        verifier: Any = None,
        idempotency: IdempotencyStore | None = None,
        queue: Any = None,
        enqueue_mode: str = "inline",
    ) -> None:
        """初始化。

        Args:
            verifier: 验签器（默认全局）。
            idempotency: 幂等存储（默认全局缓存 + 24h TTL）。
            queue: 入队实现，需有 `put(msg)` 或 `enqueue(msg)`；
                None 表示进程内收集（本地开发与测试）。
            enqueue_mode: `inline` 表示在请求内收集消息交给后续处理；
                `queue` 表示投递给 `queue`。
        """
        self._verifier = verifier
        self._idem = idempotency
        self.queue = queue
        self.enqueue_mode = enqueue_mode

    @property
    def verifier(self) -> Any:
        """验签器（惰性取全局）。"""
        if self._verifier is None:
            self._verifier = get_callback_verifier()
        return self._verifier

    @property
    def idempotency(self) -> IdempotencyStore:
        """幂等存储（惰性建）。"""
        if self._idem is None:
            self._idem = IdempotencyStore(ttl=86400)
        return self._idem

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def handle_callback(
        self,
        *,
        channel: str,
        headers: Any,
        body: bytes | None,
        payload: Any = None,
        now: float | None = None,
        verify_signature: bool | None = None,
    ) -> CallbackOutcome:
        """处理一次渠道回调。

        Args:
            channel: 渠道值（TAOBAO / TMALL / PDD / DOUYIN / WECHAT）。
            headers: 请求头。
            body: **原始请求体字节**（验签用，必须原样）。
            payload: 已解析的报文（None 时从 body 解）。
            now: 当前 epoch 秒（测试注入）。
            verify_signature: 覆盖配置的验签开关（None 走配置）。

        Returns:
            `CallbackOutcome`。**永远返回成功对象**，失败信息在字段里。
        """
        started = time.perf_counter()
        trace_id = new_trace_id()
        outcome = CallbackOutcome(trace_id=trace_id)

        with trace_context(trace_id):
            # ---- 1. 验签 ----
            should_verify = (
                self._verify_enabled()
                if verify_signature is None
                else verify_signature
            )
            if should_verify:
                result = self.verifier.verify(headers=headers, body=body, now=now)
                if not result.ok:
                    outcome.rejected = 1
                    outcome.reject_reason = result.reason
                    outcome.latency_ms = (time.perf_counter() - started) * 1000
                    # 契约 2.3.4：验签失败返 200 但丢弃
                    self._alert_reject(channel, result.reason, result.detail, trace_id)
                    return outcome

            # ---- 2. 解析 ----
            try:
                adapter = get_adapter(channel)
                raw_payload = payload if payload is not None else body
                parsed = adapter.parse(raw_payload or {})
            except ChannelParseError as exc:
                outcome.parse_error = exc.detail
                outcome.rejected = 1
                outcome.reject_reason = "PARSE_ERROR"
                outcome.latency_ms = (time.perf_counter() - started) * 1000
                logger.warning(
                    "回调解析失败 channel=%s detail=%s trace=%s",
                    channel,
                    mask_text(exc.detail),
                    trace_id,
                )
                return outcome

            outcome.received = len(parsed)

            # ---- 3. 幂等占位 ----
            fresh: list[RawInbound] = []
            for msg in parsed:
                key = self._idem_key(msg)
                if self._acquire(key):
                    fresh.append(msg)
                else:
                    outcome.duplicated += 1
                    logger.info(
                        "回调重复推送已丢弃 channel=%s msg_id=%s trace=%s",
                        channel,
                        msg.msg_id,
                        trace_id,
                    )

            # ---- 4. 入队 ----
            enqueued: list[RawInbound] = []
            for msg in fresh:
                try:
                    self._enqueue(msg)
                    enqueued.append(msg)
                except Exception:  # noqa: BLE001
                    # 入队失败要**释放幂等占位**，否则这条消息永不重试
                    self._release(self._idem_key(msg))
                    logger.exception(
                        "回调入队失败，已释放幂等占位 msg_id=%s", msg.msg_id
                    )

            outcome.enqueued = len(enqueued)
            outcome.messages = enqueued
            outcome.latency_ms = (time.perf_counter() - started) * 1000

            from app.core.metrics import record_callback

            record_callback(channel, "enqueued", outcome.enqueued)
            record_callback(channel, "duplicated", outcome.duplicated)

            logger.info(
                "回调已受理 channel=%s received=%d enqueued=%d dup=%d ms=%.1f trace=%s",
                channel,
                outcome.received,
                outcome.enqueued,
                outcome.duplicated,
                outcome.latency_ms,
                trace_id,
            )
        return outcome

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _verify_enabled(self) -> bool:
        """是否验签（读配置；本地可用 `WK_VERIFY_CALLBACK_SIGNATURE=false` 关）。"""
        from app.core.config import get_settings

        return bool(get_settings().verify_callback_signature)

    def _idem_key(self, msg: RawInbound) -> str:
        """构造消息级幂等键。

        **带上 channel**：不同渠道的 msg_id 空间独立，淘宝的 `123` 与
        拼多多的 `123` 是两条不同消息，不加渠道会误判重复。

        Args:
            msg: 归一化消息。

        Returns:
            幂等键。
        """
        return f"{CALLBACK_IDEMPOTENCY_PREFIX}:{msg.channel}:{msg.msg_id}"

    def _acquire(self, key: str) -> bool:
        """占位（同步驱动异步幂等存储）。"""
        from app.core.asyncbridge import run_maybe_async

        return bool(run_maybe_async(self.idempotency.acquire(key)))

    def _release(self, key: str) -> None:
        """释放占位。"""
        from app.core.asyncbridge import run_maybe_async

        run_maybe_async(self.idempotency.release(key))

    def _enqueue(self, msg: RawInbound) -> None:
        """投递到队列。"""
        if self.queue is None:
            # 无外部队列：进程内列表收集，供同进程消费（本地跑通链路用）
            _INLINE_INBOX.append(msg)
            return
        put = getattr(self.queue, "put", None) or getattr(self.queue, "enqueue", None)
        if put is None:
            raise TypeError("queue 缺少 put/enqueue 方法")
        from app.core.asyncbridge import run_maybe_async

        payload = {
            "channel": msg.channel,
            "msg_id": msg.msg_id,
            "session_id": msg.session_id,
            "buyer_id": msg.buyer_id,
            "msg_type": msg.msg_type.value,
            "text": msg.text,
        }
        run_maybe_async(put(json.dumps(payload, ensure_ascii=False)))

    def _alert_reject(
        self, channel: str, reason: str, detail: str, trace_id: str
    ) -> None:
        """记录验签失败告警。

        **伪造签名是安全事件，必须可告警**：契约要求返 200 丢弃，
        如果只 return 不记日志，运维完全看不到有人在打我们的回调地址。
        """
        from app.core.metrics import record_callback_signature_rejected

        record_callback_signature_rejected(channel, reason)
        level = logger.warning if reason == SignatureError.SECRET_MISSING else logger.error
        level(
            "回调验签失败已丢弃 channel=%s reason=%s detail=%s trace=%s",
            channel,
            reason,
            mask_text(detail),
            trace_id,
        )

    def drain(self) -> list[RawInbound]:
        """取走进程内收集的消息（本地消费用）。"""
        items = list(_INLINE_INBOX)
        _INLINE_INBOX.clear()
        return items

    def reset(self) -> None:
        """清空进程内队列（测试用）。"""
        _INLINE_INBOX.clear()


#: 进程内收件箱
_INLINE_INBOX: list[RawInbound] = []

_GATEWAY: CallbackGateway | None = None


def get_callback_gateway() -> CallbackGateway:
    """全局网关单例。"""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = CallbackGateway()
    return _GATEWAY


def set_callback_gateway(gateway: CallbackGateway | None) -> None:
    """替换全局网关（测试用）。"""
    global _GATEWAY
    _GATEWAY = gateway
    if gateway is not None:
        gateway.reset()


def signed_headers(
    *,
    body: bytes | None,
    app_id: str,
    secret: str,
    nonce: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """构造一组合法的回调请求头（联调与测试用）。

    Args:
        body: 请求体原始字节。
        app_id: 应用标识。
        secret: app_secret。
        nonce: 一次性串（**每次调用都要换**，否则会被 nonce 去重拦掉）。
        timestamp: epoch 秒，默认当前。

    Returns:
        含四个签名头的字典。
    """
    from app.core.signature import sign_request

    ts = int(timestamp if timestamp is not None else time.time())
    signature = sign_request(
        app_id=app_id, secret=secret, timestamp=ts, nonce=nonce, body=body
    )
    return {
        "X-App-Id": app_id,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


def callback_path(channel: str | Channel) -> str:
    """渠道回调路由路径（契约 2.3.4 的固定域名 + 路径）。"""
    key = channel.value if isinstance(channel, Channel) else str(channel).upper()
    return f"/api/v1/callback/{key.lower()}"


__all__ = [
    "CALLBACK_IDEMPOTENCY_PREFIX",
    "CallbackGateway",
    "CallbackOutcome",
    "callback_path",
    "get_callback_gateway",
    "set_callback_gateway",
    "signed_headers",
]
