"""商家工单接口端口（商家 5 号接口：创建工单 / 同步工单）。

## 为什么单独一个端口

`app/adapters/merchant_tools.py` 是**读接口**（订单/物流/库存/商品，四个 GET，
幂等、可重试）。工单是**写接口**：

* 不是幂等的（重复提交会在商家侧产生多张工单）→ 必须靠 `session_id` 做
  我方侧幂等，端口本身不做重试；
* 有独立限流（`ticket_qps=5`，**全局瓶颈**）；
* 失败要落本地队列（`TicketEvent` 的 `RATE_LIMITED` / `RETRY` / `FAILED`），
  不能因为商家侧挂了就不给用户工单号。

因此单独立端口，与读接口解耦。

## 端口约定

`create_ticket(payload)` 返回 `MerchantTicketResult`，**不抛业务异常**：
调用方（`HandoffService`）需要「失败也要建本地工单」，用返回值比用异常
更好写。网络层异常会被端口自己包装成 `ok=False`。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.errors import MerchantErrorCode
from app.core.logging import mask_text

#: 工单写接口超时（秒）。写接口比读接口更保守：宁可失败落队列，也不长时间挂住请求
TICKET_TIMEOUT_SECONDS: float = 6.0


@dataclass
class MerchantTicketResult:
    """商家工单接口的调用结果。"""

    ok: bool = False
    ticket_no: str | None = None
    error_code: int | None = None
    message: str = ""
    http_status: int = 200
    attempts: int = 1
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MerchantTicketPort(Protocol):
    """商家工单端口协议。"""

    def create_ticket(self, payload: dict[str, Any]) -> MerchantTicketResult:
        """创建工单（商家 5 号接口）。"""
        ...


def _signed_headers(body: bytes) -> dict[str, str]:
    """构造带 HMAC 签名的请求头（商家说明书 0.3 节，POST 用真实 body）。"""
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
        "Accept": "application/json",
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


class HttpMerchantTicketPort:
    """真实商家工单接口（HTTPS + HMAC 签名，**不做重试**）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = TICKET_TIMEOUT_SECONDS,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.merchant_base_url).rstrip("/")
        self.timeout = timeout

    def create_ticket(self, payload: dict[str, Any]) -> MerchantTicketResult:
        """POST /api/v1/ticket/create。"""
        import json

        import httpx

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        url = f"{self.base_url}/api/v1/ticket/create"
        try:
            resp = httpx.post(
                url,
                content=body,
                headers=_signed_headers(body),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            return MerchantTicketResult(
                ok=False,
                error_code=int(MerchantErrorCode.SERVICE_UNAVAILABLE),
                message=mask_text(f"{type(exc).__name__}: {exc}"),
                http_status=0,
            )

        if resp.status_code != 200:
            return MerchantTicketResult(
                ok=False,
                error_code=int(MerchantErrorCode.SERVICE_UNAVAILABLE),
                message=mask_text(f"商家返回 HTTP {resp.status_code}"),
                http_status=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return MerchantTicketResult(
                ok=False,
                error_code=int(MerchantErrorCode.INTERNAL_ERROR),
                message=mask_text(f"商家响应不是合法 JSON: {exc}"),
                http_status=resp.status_code,
            )

        code = int(data.get("code", 0) or 0)
        if code != 0:
            return MerchantTicketResult(
                ok=False,
                error_code=code,
                message=mask_text(str(data.get("message", "") or f"code={code}")),
                http_status=resp.status_code,
            )

        inner = data.get("data") or {}
        return MerchantTicketResult(
            ok=True,
            ticket_no=str(inner.get("ticket_no") or "") or None,
            http_status=resp.status_code,
            data=inner if isinstance(inner, dict) else {},
        )


class MockMerchantTicketPort:
    """本地 Mock 工单接口（无商家环境时使用）。

    支持错误注入：`always_fail` / `fail_times` / `fail_code`，
    便于演练「商家 5 号接口挂了 → 本地队列兜底」。
    """

    def __init__(
        self,
        *,
        always_fail: bool = False,
        fail_times: int = 0,
        fail_code: int = int(MerchantErrorCode.SERVICE_UNAVAILABLE),
        rate_limited_times: int = 0,
    ) -> None:
        self.always_fail = always_fail
        self.fail_times = fail_times
        self.fail_code = fail_code
        self.rate_limited_times = rate_limited_times
        self.calls: list[dict[str, Any]] = []
        self._seq = 0

    def create_ticket(self, payload: dict[str, Any]) -> MerchantTicketResult:
        """模拟建单：默认成功并返回 `MT` 开头的商家工单号。"""
        self.calls.append(payload)

        if self.rate_limited_times > 0:
            self.rate_limited_times -= 1
            return MerchantTicketResult(
                ok=False,
                error_code=int(MerchantErrorCode.RATE_LIMITED),
                message="商家接口限流",
                http_status=429,
            )

        if self.always_fail or self.fail_times > 0:
            if not self.always_fail:
                self.fail_times -= 1
            return MerchantTicketResult(
                ok=False,
                error_code=self.fail_code,
                message="商家工单服务不可用",
                http_status=503,
            )

        self._seq += 1
        digest = hashlib.sha1(
            f"{payload.get('session_id', '')}:{self._seq}".encode()
        ).hexdigest()
        ticket_no = "MT" + str(int(digest[:12], 16)).zfill(12)[:12]
        return MerchantTicketResult(
            ok=True,
            ticket_no=ticket_no,
            http_status=200,
            data={"ticket_no": ticket_no, "status": "OPEN"},
        )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_CLIENT: MerchantTicketPort | None = None


def get_ticket_client() -> MerchantTicketPort:
    """取工单端口单例。

    生产环境用真实 HTTP 端口，本地/测试/联调环境用 Mock
    ——与 `app.adapters.merchant_tools` 的取舍口径一致（商家环境不可达）。
    """
    global _CLIENT
    if _CLIENT is None:
        settings = get_settings()
        if settings.is_prod:
            _CLIENT = HttpMerchantTicketPort()
        else:
            _CLIENT = MockMerchantTicketPort()
    return _CLIENT


def set_ticket_client(client: MerchantTicketPort | None) -> None:
    """替换工单端口单例（单测注入用；传 None 复位）。"""
    global _CLIENT
    _CLIENT = client


__all__ = [
    "TICKET_TIMEOUT_SECONDS",
    "HttpMerchantTicketPort",
    "MerchantTicketPort",
    "MerchantTicketResult",
    "MockMerchantTicketPort",
    "get_ticket_client",
    "set_ticket_client",
]
