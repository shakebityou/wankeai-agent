"""商家渠道签名与回调校验（契约 2.3.2 / 2.3.4，BE-01）。

## 签名串的确切形态

契约规定：

```text
sign_str   = app_id + timestamp + nonce + body_sha256
X-Signature = HMAC-SHA256(sign_str, app_secret)   # 小写十六进制
```

三条容易踩的细节，都在本模块里固化：

1. **GET 请求的 body_sha256 是空字符串的 SHA-256**，不是省略该段。
   `sha256(b"")` = `e3b0c442...b855`。漏掉这一段签名就会对不上。
2. **签名前不得对 body 二次序列化**。商家发来什么字节就签什么字节；
   我们重排一遍 JSON key 顺序算出来的 sha256 与商家的不同。
   所以本模块所有校验函数都接收**原始 bytes**，不接收 dict。
3. **app_secret 只从环境变量/KMS 读**，本模块不接受调用方传入明文以外
   的来源，也不写进任何日志（日志里只出现 app_id 与 timestamp）。

## 时间窗口与重放

* `X-Timestamp` 与本地时间偏差 > `timestamp_skew_seconds`（默认 300s）→ 拒。
  用 `abs()` 判断，**未来时间同样要拒**，否则攻击者可以预签一条明年才生效的请求。
* `X-Nonce` 在 `nonce_ttl_seconds`（默认 300s）内只能用一次，用缓存 SETNX 实现。
* 另外契约 2.3.4 还要求 `msg_id` 在 24h 内幂等——那是消息级幂等，
  由 `app/api/v1/callback.py` 用 `idempotency_ttl_seconds` 处理，不在这里。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 空 body 的 SHA-256（GET 请求的 body_sha256 必须是这个值，不是省略）
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

#: Nonce 合法形态：只允许可见 ASCII，长度 8–64
_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

#: 十六进制摘要形态
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class SignatureError(Exception):
    """签名校验失败。`reason` 是**结构化原因码**，用于告警归类与用例断言。"""

    #: 原因码常量（不要在别处另起字符串）
    MISSING_HEADER = "MISSING_HEADER"
    BAD_NONCE = "BAD_NONCE"
    BAD_TIMESTAMP = "BAD_TIMESTAMP"
    TIMESTAMP_SKEW = "TIMESTAMP_SKEW"
    NONCE_REPLAY = "NONCE_REPLAY"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    SECRET_MISSING = "SECRET_MISSING"

    def __init__(self, reason: str, detail: str = "") -> None:
        """初始化。

        Args:
            reason: 原因码（用本类常量）。
            detail: 补充说明（**不得含密钥**）。
        """
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def body_sha256(body: bytes | None) -> str:
    """计算请求体摘要。

    Args:
        body: **原始字节**。`None` 与 `b""` 等价（GET 请求）。

    Returns:
        小写十六进制 SHA-256。
    """
    if not body:
        return EMPTY_BODY_SHA256
    return hashlib.sha256(body).hexdigest()


def build_sign_string(*, app_id: str, timestamp: str, nonce: str, body_sha: str) -> str:
    """拼签名串。**顺序是契约的一部分，不能调整。**

    Args:
        app_id: 应用标识。
        timestamp: X-Timestamp 原样字符串（不要重新格式化数字）。
        nonce: X-Nonce。
        body_sha: `body_sha256()` 的结果。

    Returns:
        签名串。
    """
    return f"{app_id}{timestamp}{nonce}{body_sha}"


def compute_signature(sign_str: str, secret: str) -> str:
    """HMAC-SHA256 签名，小写十六进制。

    Args:
        sign_str: `build_sign_string()` 的结果。
        secret: app_secret 明文。

    Returns:
        小写十六进制签名串。
    """
    return hmac.new(
        secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_request(
    *,
    app_id: str,
    secret: str,
    timestamp: str | int | float,
    nonce: str,
    body: bytes | None = None,
) -> str:
    """对一次出站请求签名，返回 `X-Signature` 的值。

    Args:
        app_id: 应用标识。
        secret: app_secret。
        timestamp: 秒级时间戳（int/float/字符串均可，统一转成整数字符串）。
        nonce: 一次性随机串。
        body: 请求体原始字节（GET 传 `None`）。

    Returns:
        小写十六进制签名。
    """
    ts = _normalize_timestamp(timestamp)
    sign_str = build_sign_string(
        app_id=app_id, timestamp=ts, nonce=nonce, body_sha=body_sha256(body)
    )
    return compute_signature(sign_str, secret)


def _normalize_timestamp(timestamp: str | int | float) -> str:
    """把时间戳统一成整数字符串。

    **必须整数**：商家侧大概率用 `int(time.time())`，我们如果传 `1234.5`
    或者带前导零的字符串，两边签名串就不一致。

    Args:
        timestamp: 任意形态的时间戳。

    Returns:
        整数字符串。
    """
    if isinstance(timestamp, str):
        return str(int(float(timestamp.strip())))
    return str(int(timestamp))


def secure_compare(a: str, b: str) -> bool:
    """常量时间比较，防时序侧信道。

    Args:
        a: 期望值。
        b: 实际值。

    Returns:
        是否相等。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class CallbackAuthResult:
    """回调验签结果。"""

    ok: bool
    reason: str = ""
    detail: str = ""
    app_id: str = ""
    timestamp: str = ""
    nonce: str = ""
    #: 是否命中过本进程内的 nonce 重放（仅当 ok=False 且有意义）
    replay: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class CallbackVerifier:
    """回调验签 + 时间窗 + nonce 去重。

    设计决定：**验签失败也要返回 200**（契约 2.3.4），由调用方决定
    HTTP 状态；本类只负责说「过还是不过、为什么不过」。
    """

    def __init__(
        self,
        *,
        cache: Any = None,
        settings: Settings | None = None,
        clock: Any = None,
    ) -> None:
        """初始化。

        Args:
            cache: `CachePort` 实现（本地为 `InMemoryCache`）。
            settings: 配置。
            clock: 可注入时钟（测试用），返回 epoch 秒。
        """
        self._cache = cache
        self._settings = settings or get_settings()
        self._clock = clock or time.time

    # -- 惰性依赖 ------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """配置。"""
        return self._settings

    @property
    def cache(self) -> Any:
        """缓存（惰性取全局实现）。"""
        if self._cache is None:
            from app.adapters.cache import get_cache

            self._cache = get_cache()
        return self._cache

    # -- 主流程 --------------------------------------------------------

    def verify(
        self,
        *,
        headers: dict[str, str] | Any,
        body: bytes | None,
        now: float | None = None,
    ) -> CallbackAuthResult:
        """校验一次回调。

        Args:
            headers: 请求头（大小写不敏感，dict 或 Starlette Headers 均可）。
            body: **原始请求体字节**。
            now: 当前 epoch 秒（测试注入）。

        Returns:
            `CallbackAuthResult`。`ok=False` 时 `reason` 为原因码。
        """
        app_id = _header(headers, "x-app-id")
        timestamp = _header(headers, "x-timestamp")
        nonce = _header(headers, "x-nonce")
        signature = _header(headers, "x-signature")

        if not (app_id and timestamp and nonce and signature):
            missing = [
                name
                for name, val in (
                    ("X-App-Id", app_id),
                    ("X-Timestamp", timestamp),
                    ("X-Nonce", nonce),
                    ("X-Signature", signature),
                )
                if not val
            ]
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.MISSING_HEADER,
                detail=f"缺少请求头 {missing}",
            )

        if app_id != self._settings.app_id:
            # 不用 BAD_SIGNATURE 报，便于告警区分「配置错」与「伪造」
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.BAD_SIGNATURE,
                detail=f"app_id 不匹配（收到 {app_id}）",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        if not _NONCE_RE.match(nonce):
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.BAD_NONCE,
                detail="X-Nonce 形态非法",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        # 时间窗
        try:
            ts_int = int(float(timestamp))
        except (TypeError, ValueError):
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.BAD_TIMESTAMP,
                detail="X-Timestamp 不是数字",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )
        current = self._clock() if now is None else now
        skew = abs(current - ts_int)
        if skew > self._settings.timestamp_skew_seconds:
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.TIMESTAMP_SKEW,
                detail=f"偏差 {skew:.0f}s 超过 {self._settings.timestamp_skew_seconds}s",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        # 验签（先验签再登记 nonce：否则伪造请求能把真 nonce 占掉）
        secret = self._secret()
        if not secret:
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.SECRET_MISSING,
                detail="未配置 app_secret，无法验签",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        if not _HEX_RE.match(signature.lower()):
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.BAD_SIGNATURE,
                detail="X-Signature 不是 64 位十六进制",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        expected = sign_request(
            app_id=app_id,
            secret=secret,
            timestamp=ts_int,
            nonce=nonce,
            body=body,
        )
        if not secure_compare(expected, signature.lower()):
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.BAD_SIGNATURE,
                detail="签名不匹配",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
            )

        # nonce 去重（验签通过后才登记，避免伪造请求污染 nonce 池）
        replayed = self._remember_nonce(nonce)
        if replayed:
            return CallbackAuthResult(
                ok=False,
                reason=SignatureError.NONCE_REPLAY,
                detail="X-Nonce 在 TTL 内已被使用",
                app_id=app_id,
                timestamp=timestamp,
                nonce=nonce,
                replay=True,
            )

        return CallbackAuthResult(
            ok=True, app_id=app_id, timestamp=timestamp, nonce=nonce
        )

    # -- 内部 ----------------------------------------------------------

    def _secret(self) -> str:
        """取 app_secret 明文（只在内存里用，绝不写日志）。"""
        secret = self._settings.app_secret
        return secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)

    def _remember_nonce(self, nonce: str) -> bool:
        """登记 nonce，返回**是否已存在**（True 即重放）。

        用 cache 的 `setnx` 语义：本地 `InMemoryCache` 与 `RedisCache` 都提供。
        取不到 `setnx` 时退化为 get/set 两步——**退化路径不是原子的**，
        并发下极小概率放过重放，但比「没有去重」好；日志会记一次告警。

        Args:
            nonce: 已经过验签的 nonce。

        Returns:
            True 表示 TTL 内已用过。
        """
        from app.core.asyncbridge import run_maybe_async

        key = f"wk:nonce:{nonce}"
        ttl = self._settings.nonce_ttl_seconds
        cache = self.cache
        setnx = getattr(cache, "setnx", None)
        if setnx is not None:
            acquired = run_maybe_async(setnx(key, "1", ttl=ttl))
            return not acquired
        logger.warning("缓存实现缺少 setnx，nonce 去重退化为非原子路径")
        existed = run_maybe_async(cache.get(key)) is not None
        if not existed:
            run_maybe_async(cache.set(key, "1", ttl=ttl))
        return existed

    def reset(self) -> None:
        """清空本进程内的 nonce 记录（测试用）。"""
        from app.core.asyncbridge import run_maybe_async

        delete_prefix = getattr(self.cache, "delete_prefix", None)
        if delete_prefix is not None:
            run_maybe_async(delete_prefix("wk:nonce:"))


def _header(headers: dict[str, str] | Any, name: str) -> str:
    """大小写不敏感地取请求头。

    Args:
        headers: dict 或 Starlette `Headers`。
        name: 小写头名。

    Returns:
        头值，缺失返回空串。
    """
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except AttributeError:
        return ""
    if value is None:
        # Starlette Headers 大小写不敏感；普通 dict 再试一次规范化 key
        for key, val in dict(headers).items():
            if key.lower() == name:
                return str(val)
        return ""
    return str(value)


_VERIFIER: CallbackVerifier | None = None


def get_callback_verifier() -> CallbackVerifier:
    """全局验签器（单例）。"""
    global _VERIFIER
    if _VERIFIER is None:
        _VERIFIER = CallbackVerifier()
    return _VERIFIER


def set_callback_verifier(verifier: CallbackVerifier | None) -> None:
    """替换全局验签器（测试用）。"""
    global _VERIFIER
    _VERIFIER = verifier
