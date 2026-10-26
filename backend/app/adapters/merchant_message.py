"""商家「平台客服消息收发」接口的 HTTP 实现（契约 A 6.x，BE-02 出站通道）。

商家接口地址：`POST {merchant_base_url}/api/v1/message/send`（商家《接口对接说明书》6.1）。

## 为什么单独成一个适配器，而不是塞进 `MessageSendService`

`MessageSendService` 关心的是**业务语义**：分段、幂等、合规、失败落表、
坐席台可见。它不该知道 HTTP 头里放什么、429 的 `Retry-After` 是秒还是日期。
把协议细节封在端口实现里，带来两个好处：

1. 单测可以注入一个「第 2 次返回 429、第 3 次成功」的假通道，不需要网络；
2. 商家测试环境（`C-05` QPS 提额）打通后只需换掉这一层，业务层零改动。

## 两种实现

| 实现 | 场景 | 说明 |
| --- | --- | --- |
| `HttpMerchantMessageChannel` | 生产 / 联调 | 真实 HTTPS 调用，带 HMAC 签名 + 非幂等重试 |
| `MockMerchantMessageChannel` | 本机 / 单测 | 确定性行为，可按 `inject` 注入 429/500/超时 |

**为什么不把重试写进 HTTP 层**：消息发送的重试策略必须**幂等**（同 `msg_id`
重发不能变成两条用户可见的消息），而幂等由 `IdempotencyStore` 在业务层保证。
HTTP 层只做「一次请求 + 一次结果」，任何重发都由业务层发起，才能保证
每次重发都带着同一个 `msg_id`。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.errors import MerchantErrorCode
from app.core.logging import get_logger, mask_text
from app.core.trace import get_trace_id

logger = get_logger(__name__)

#: 发送超时（秒）。比商家通用超时短：出站消息是用户等待体感最敏感的一环，
#: 6 秒还没发出去，用户已经在重复催问了，不如早失败早重试。
SEND_TIMEOUT_SECONDS = 6.0


class MerchantChannelError(Exception):
    """商家消息接口调用失败（网络层或业务错误码）。

    Attributes:
        code: 商家错误码；网络异常时用 `INTERNAL_ERROR` 兜底。
        message: 脱敏后的描述。
        http_status: HTTP 状态码。
        retry_after: 429 的 `Retry-After` 秒数。
        retryable: 是否值得重试（非幂等操作重试前要查幂等表）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: MerchantErrorCode = MerchantErrorCode.INTERNAL_ERROR,
        http_status: int | None = None,
        retry_after: float | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after
        self.retryable = retryable


@dataclass
class MerchantSendResult:
    """商家侧发送结果（对应说明书 6.5 响应示例）。

    Attributes:
        ok: 是否成功（商家 `code == 0`）。
        msg_id: 我方消息 ID（原样回传）。
        platform_msg_id: 平台侧消息 ID。
        status: `SENT` / `FAILED`。
        sent_at: 平台记录的时间字符串。
        error_code: 失败时的商家错误码。
        error_message: 失败描述（**已脱敏**）。
        http_status: HTTP 状态码。
        sent_wall_clock: 本地记录的实际发出时刻（epoch 秒，测试可注入）。
    """

    ok: bool = False
    msg_id: str = ""
    platform_msg_id: str = ""
    status: str = "FAILED"
    sent_at: str = ""
    error_code: int | None = None
    error_message: str = ""
    http_status: int | None = None
    sent_wall_clock: float = field(default_factory=time.time)


def parse_retry_after(value: str | None) -> float | None:
    """解析 `Retry-After` 响应头。

    同时支持**秒数**（`Retry-After: 1`）与**HTTP 日期**两种写法。商家说明书
    给的是秒数，但网关/负载均衡常见的实现会给日期，两种都要认。

    Args:
        value: 响应头原始值。

    Returns:
        秒数；无法解析返回 None。
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:  # noqa: BLE001 - 非标准写法一律当作缺失
        return None


class MessageChannelPort:
    """出站消息通道端口（单一方法，便于替换）。"""

    def send(self, payload: dict[str, Any]) -> MerchantSendResult:  # pragma: no cover
        """发送一条消息。

        Args:
            payload: 商家 6.3 节请求体。

        Returns:
            `MerchantSendResult`。

        Raises:
            MerchantChannelError: 网络异常或商品侧返回非 0 错误码。
        """
        raise NotImplementedError


class HttpMerchantMessageChannel(MessageChannelPort):
    """真实商家接口（HTTPS + HMAC 签名）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = SEND_TIMEOUT_SECONDS,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.merchant_base_url).rstrip("/")
        self.timeout = timeout

    def _headers(self, body: bytes) -> dict[str, str]:
        """构造签名头（商家说明书 6.4 与 0.3 节）。"""
        from app.core.signature import sign_request

        settings = get_settings()
        ts = int(time.time())
        nonce = uuid.uuid4().hex[:16]
        signature = sign_request(
            app_id=settings.app_id,
            secret=settings.app_secret.get_secret_value(),
            timestamp=ts,
            nonce=nonce,
            body=body,
        )
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-App-Id": settings.app_id,
            "X-Timestamp": str(ts),
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        token = settings.internal_token.get_secret_value()
        if token:
            headers["X-Internal-Token"] = token
        return headers

    def send(self, payload: dict[str, Any]) -> MerchantSendResult:  # pragma: no cover - 需真实网络
        """调用商家发送接口。"""
        import httpx

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/api/v1/message/send"
        try:
            resp = httpx.post(
                url, content=body, headers=self._headers(body), timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            raise MerchantChannelError(
                mask_text(f"{type(exc).__name__}: {exc}"), retryable=True
            ) from exc

        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        if resp.status_code == 429:
            raise MerchantChannelError(
                "商家接口限流",
                code=MerchantErrorCode.RATE_LIMITED,
                http_status=429,
                retry_after=retry_after if retry_after is not None else 1.0,
                retryable=True,
            )

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}

        code = int(data.get("code", MerchantErrorCode.INTERNAL_ERROR))
        biz = data.get("data") or {}
        if resp.status_code >= 500 or code not in (0,):
            err = MerchantErrorCode(code) if code in _KNOWN_CODES else (
                MerchantErrorCode.SERVICE_UNAVAILABLE
                if resp.status_code >= 500
                else MerchantErrorCode.INTERNAL_ERROR
            )
            raise MerchantChannelError(
                mask_text(str(data.get("message", resp.reason_phrase))),
                code=err,
                http_status=resp.status_code,
                retry_after=retry_after,
                retryable=err in (
                    MerchantErrorCode.INTERNAL_ERROR,
                    MerchantErrorCode.SERVICE_UNAVAILABLE,
                    MerchantErrorCode.UPSTREAM_TIMEOUT,
                ),
            )

        return MerchantSendResult(
            ok=True,
            msg_id=str(biz.get("msg_id", payload.get("msg_id", ""))),
            platform_msg_id=str(biz.get("platform_msg_id", "")),
            status=str(biz.get("status", "SENT")),
            sent_at=str(biz.get("sent_at", "")),
            http_status=resp.status_code,
        )


_KNOWN_CODES = {int(c) for c in MerchantErrorCode}


class MockMerchantMessageChannel(MessageChannelPort):
    """确定性 Mock 通道（本机 / 单测，任务书 5.1 节「Mock 先行」）。

    行为完全可预测：
        * `fail_times`：前 N 次抛指定错误，之后成功（演练重试路径）；
        * `always_fail`：一直失败（演练「重试耗尽 → 落失败表」）；
        * `down`：模拟下游整体不可用（熔断演练）；
        * `fail_msg_ids`：只让指定 `msg_id` 失败（演练部分失败）；
        * `delay_seconds`：模拟耗时。

    Attributes:
        sent: 实际发出的请求体列表（断言「同 msg_id 只发 1 条」的依据）。
        swallowed: 因幂等冲突被商家吞掉的次数。
    """

    def __init__(
        self,
        *,
        fail_times: int = 0,
        always_fail: bool = False,
        fail_code: MerchantErrorCode = MerchantErrorCode.INTERNAL_ERROR,
        retry_after: float = 0.0,
        fail_msg_ids: set[str] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.fail_code = fail_code
        self.retry_after = retry_after
        self.fail_msg_ids = set(fail_msg_ids or ())
        self.delay_seconds = delay_seconds
        self.sent: list[dict[str, Any]] = []
        self.swallowed = 0
        self.down = False
        self._calls = 0
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> MerchantSendResult:
        """按预设行为返回结果或抛错。"""
        with self._lock:
            self._calls += 1
            call_no = self._calls

        if self.delay_seconds:
            time.sleep(self.delay_seconds)

        if self.down:
            raise MerchantChannelError(
                "mock channel down",
                code=MerchantErrorCode.SERVICE_UNAVAILABLE,
                http_status=503,
                retryable=True,
            )

        msg_id = str(payload.get("msg_id", ""))
        if msg_id in self.fail_msg_ids:
            raise MerchantChannelError(
                "mock targeted failure",
                code=self.fail_code,
                http_status=500,
                retry_after=self.retry_after,
                retryable=True,
            )

        if self.always_fail or call_no <= self.fail_times:
            status = (
                429 if self.fail_code == MerchantErrorCode.RATE_LIMITED else 500
            )
            raise MerchantChannelError(
                f"mock failure #{call_no}",
                code=self.fail_code,
                http_status=status,
                retry_after=self.retry_after,
                retryable=True,
            )

        self.sent.append(payload)
        return MerchantSendResult(
            ok=True,
            msg_id=msg_id,
            platform_msg_id=f"mock_{msg_id}",
            status="SENT",
            sent_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            http_status=200,
        )


_gateway: MessageChannelPort | None = None


def get_message_channel() -> MessageChannelPort:
    """取全局出站消息通道单例。

    默认返回 ``MockMerchantMessageChannel``：本机不可达商家测试环境
    （见交接文档 9.2），若默认走真实 HTTP，每一次发送都会超时 6 秒。
    生产通过 ``WK_MESSAGE_CHANNEL=http`` 切换。
    """
    global _gateway  # noqa: PLW0603
    if _gateway is None:
        from app.core.config import get_settings as _gs

        if _gs().message_channel == "http":
            _gateway = HttpMerchantMessageChannel()
        else:
            _gateway = MockMerchantMessageChannel()
    return _gateway


def set_message_channel(channel: MessageChannelPort | None) -> None:
    """替换全局通道（**测试专用**）。"""
    global _gateway  # noqa: PLW0603
    _gateway = channel


__all__ = [
    "HttpMerchantMessageChannel",
    "MerchantChannelError",
    "MerchantSendResult",
    "MessageChannelPort",
    "MockMerchantMessageChannel",
    "SEND_TIMEOUT_SECONDS",
    "get_message_channel",
    "parse_retry_after",
    "set_message_channel",
]
