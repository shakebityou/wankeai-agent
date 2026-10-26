"""商家四个「读」接口的适配器（契约 A 1~4 号，BE-07 / BE-08 / BE-09）。

商家接口地址（《接口对接说明书》）：

| 端口方法 | 上游 | 路径 |
| --- | --- | --- |
| `query_order` | 1 号 | `GET {base}/api/v1/order/query` |
| `track_logistics` | 2 号 | `GET {base}/api/v1/logistics/track` |
| `query_inventory` | 3 号 | `GET {base}/api/v1/inventory/query` |
| `query_product` | 4 号 | `GET {base}/api/v1/product/detail` |

## 为什么与 `merchant_message.py` 分开

出站（写）与入站（读）的关注点不同：读接口要处理**分页、去重、
部分缺失**（库存 3 号接口允许部分 SKU 不存在），写接口要处理**幂等与
重试不重复**。共用的只有签名头构造与错误码映射，故各自独立成文件，
错误类型复用 `merchant_message.MerchantChannelError`。

## 重试放在哪一层

与出站一致：**适配器只做「一次请求 + 一次结果」**，重试由
`app.services.tools.*` 依据 `MERCHANT_ERROR_POLICIES` 发起。读操作天然
幂等（GET + 同参数），重试不会产生重复写，这一点是 BE-18 DoD③ 的依据。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.adapters.merchant_message import (
    MerchantChannelError,
    parse_retry_after,
)
from app.core.config import get_settings
from app.core.errors import MerchantErrorCode
from app.core.logging import get_logger, mask_text

logger = get_logger(__name__)

#: 读接口超时（秒）。物流轨迹是用户等待体感最敏感的一环，与出站一致取 6 秒。
READ_TIMEOUT_SECONDS = 6.0

#: 库存查询单次 SKU 上限（商家 3 号接口硬限制）。
MAX_SKU_PER_INVENTORY = 50

#: 商家已知错误码集合（用于区分「商家业务错误」与「我方/网关错误」）。
_KNOWN_CODES = {int(c) for c in MerchantErrorCode}


@dataclass
class MerchantQueryResult:
    """一次读接口调用的结果。

    Attributes:
        ok: 是否成功（HTTP 200 且商家 `code == 0`）。
        data: 商家响应体的 `data` 字段（可能是 dict 或 list）。
        request_id: 商家 `request_id`，排查链路用。
        error_code: 失败时的商家错误码。
        error_message: 失败描述（**已脱敏**）。
        http_status: HTTP 状态码。
        latency_ms: 本次耗时。
        attempts: 调用序号（由适配器内部计数，测试可注入）。
    """

    ok: bool = False
    data: Any = None
    request_id: str = ""
    error_code: int | None = None
    error_message: str = ""
    http_status: int | None = None
    latency_ms: int = 0
    attempts: int = 1


class MerchantToolPort:
    """商家读接口端口（四个方法，一一对应商家 1~4 号接口）。"""

    def query_order(self, params: dict[str, Any]) -> MerchantQueryResult:  # pragma: no cover
        """查询订单。"""
        raise NotImplementedError

    def track_logistics(self, params: dict[str, Any]) -> MerchantQueryResult:  # pragma: no cover
        """查询物流轨迹。"""
        raise NotImplementedError

    def query_inventory(self, params: dict[str, Any]) -> MerchantQueryResult:  # pragma: no cover
        """查询库存。"""
        raise NotImplementedError

    def query_product(self, params: dict[str, Any]) -> MerchantQueryResult:  # pragma: no cover
        """查询商品详情。"""
        raise NotImplementedError


def _signed_headers(body: bytes) -> dict[str, str]:
    """构造带 HMAC 签名的请求头（商家说明书 0.3 节）。

    GET 请求的 `body_sha256` 是**空字节串的 SHA-256**（不是省略、不是 null），
    这一点在 `app/core/signature.py` 的 `body_sha256` 里已处理，这里直接复用。
    """
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
        "X-App-Id": settings.app_id,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Signature": signature,
    }
    token = settings.internal_token.get_secret_value()
    if token:
        headers["X-Internal-Token"] = token
    return headers


def _classify_http_error(
    resp: Any, retry_after: float | None
) -> MerchantChannelError:
    """把商家 HTTP 响应翻译成 `MerchantChannelError`。

    Args:
        resp: `httpx.Response`。
        retry_after: 已解析的 `Retry-After` 秒数。

    Returns:
        带商家错误码的异常对象（调用方负责 raise）。
    """
    if resp.status_code == 429:
        return MerchantChannelError(
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
    code = int(data.get("code", 0) or 0)
    message = mask_text(str(data.get("message", "") or resp.reason_phrase or ""))

    if resp.status_code >= 500:
        return MerchantChannelError(
            message or "商家服务不可用",
            code=MerchantErrorCode.SERVICE_UNAVAILABLE,
            http_status=resp.status_code,
            retry_after=retry_after,
            retryable=True,
        )
    if code in _KNOWN_CODES:
        err = MerchantErrorCode(code)
        return MerchantChannelError(
            message or err.name,
            code=err,
            http_status=resp.status_code,
            retry_after=retry_after,
            retryable=err
            in (
                MerchantErrorCode.SERVICE_UNAVAILABLE,
                MerchantErrorCode.UPSTREAM_TIMEOUT,
                MerchantErrorCode.INTERNAL_ERROR,
            ),
        )
    return MerchantChannelError(
        message or f"商家返回未知错误码 {code}",
        code=MerchantErrorCode.INTERNAL_ERROR,
        http_status=resp.status_code,
        retry_after=retry_after,
        retryable=False,
    )


class HttpMerchantToolPort(MerchantToolPort):
    """真实商家读接口（HTTPS + HMAC 签名，无重试）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.merchant_base_url).rstrip("/")
        self.timeout = timeout

    # -- 内部 ---------------------------------------------------------------

    def _get(
        self, path: str, params: dict[str, Any]
    ) -> MerchantQueryResult:
        """发起一次 GET 请求。

        读接口是幂等的，所以**这里不做任何重试**：重试由上层按
        `MERCHANT_ERROR_POLICIES` 决定（429 服从 Retry-After，
        503/504 指数退避最多 3 次）。
        """
        import httpx

        started = time.perf_counter()
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.get(
                url,
                params=clean,
                headers=_signed_headers(b""),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            raise MerchantChannelError(
                mask_text(f"{type(exc).__name__}: {exc}"), retryable=True
            ) from exc

        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            raise _classify_http_error(resp, retry_after)

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MerchantChannelError(
                mask_text(f"商家响应不是合法 JSON: {exc}"), retryable=False
            ) from exc

        code = int(payload.get("code", 0) or 0)
        if code != 0:
            raise MerchantChannelError(
                mask_text(str(payload.get("message", "") or f"code={code}")),
                code=(
                    MerchantErrorCode(code)
                    if code in _KNOWN_CODES
                    else MerchantErrorCode.INTERNAL_ERROR
                ),
                http_status=resp.status_code,
                retry_after=retry_after,
                retryable=False,
            )

        return MerchantQueryResult(
            ok=True,
            data=payload.get("data"),
            request_id=str(payload.get("request_id", "")),
            http_status=resp.status_code,
            latency_ms=latency,
        )

    # -- 端口实现 -----------------------------------------------------------

    def query_order(self, params: dict[str, Any]) -> MerchantQueryResult:
        """商家 1 号接口：订单查询。"""
        return self._get("/api/v1/order/query", params)

    def track_logistics(self, params: dict[str, Any]) -> MerchantQueryResult:
        """商家 2 号接口：物流轨迹。"""
        return self._get("/api/v1/logistics/track", params)

    def query_inventory(self, params: dict[str, Any]) -> MerchantQueryResult:
        """商家 3 号接口：库存。"""
        return self._get("/api/v1/inventory/query", params)

    def query_product(self, params: dict[str, Any]) -> MerchantQueryResult:
        """商家 4 号接口：商品详情。"""
        return self._get("/api/v1/product/detail", params)


# ---------------------------------------------------------------------------
# Mock 实现（本机 / 单测）
# ---------------------------------------------------------------------------

_ORDER_NO = "202601010001"


def _mock_order(order_no: str = _ORDER_NO) -> dict[str, Any]:
    """构造一条 Mock 订单（对齐商家 1.5 节字段）。

    `receiver` 按商家口径**预脱敏**：手机号前 3 后 4、姓名留姓、地址仅省市级。
    项目组不得还原（BE-17 DoD）。
    """
    return {
        "order_no": order_no,
        "status": "PAID",
        "buyer_id": "buyer_001",
        "channel": "TAOBAO",
        "created_at": "2026-01-01 10:00:00",
        "paid_at": "2026-01-01 10:05:00",
        "amount": {"total": 129.0, "discount": 10.0, "freight": 0.0, "paid": 119.0},
        "items": [
            {"sku_id": "WK-BEAR-20", "title": "可爱小熊 20cm", "qty": 1, "price": 119.0}
        ],
        "logistics": {
            "company": "ZTO",
            "tracking_no": "780123456789",
            "latest": {"time": "2026-01-02 09:00:00", "desc": "快件已到达杭州转运中心"},
        },
        "estimated_arrival": "2026-01-03",
        "receiver": {"name": "张*", "phone": "138****8888", "address": "浙江省杭州市"},
    }


class MockMerchantToolPort(MerchantToolPort):
    """确定性 Mock 读端口（本机 / 单测，任务书 5.1 节「Mock 先行」）。

    行为完全可预测，可按 `inject` 注入各类失败：

        * `fail_times`：前 N 次抛指定错误码，之后成功（演练重试）；
        * `always_fail`：一直失败（演练「重试耗尽 → 降级话术」）；
        * `fail_code`：注入的错误码（默认 50301）；
        * `retry_after`：429 时的退避秒数；
        * `down`：整体不可用（演练熔断）；
        * `fail_keys`：只让指定 `order_no` / `tracking_no` / `sku_id` 失败；
        * `missing_orders` / `missing_skus`：模拟 40401（订单不存在 / SKU 不存在）；
        * `latency_ms`：模拟耗时。

    Attributes:
        calls: 全部请求参数列表（断言「重试次数」与「单次请求参数」的依据）。
    """

    def __init__(
        self,
        *,
        fail_times: int = 0,
        always_fail: bool = False,
        fail_code: MerchantErrorCode = MerchantErrorCode.SERVICE_UNAVAILABLE,
        retry_after: float = 0.0,
        down: bool = False,
        fail_keys: set[str] | None = None,
        missing_orders: set[str] | None = None,
        missing_skus: set[str] | None = None,
        latency_ms: int = 0,
    ) -> None:
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.fail_code = fail_code
        self.retry_after = retry_after
        self.down = down
        self.fail_keys = set(fail_keys or ())
        self.missing_orders = set(missing_orders or ())
        self.missing_skus = set(missing_skus or ())
        self.latency_ms = latency_ms
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._calls = 0
        self._lock = threading.Lock()

    # -- 内部 ---------------------------------------------------------------

    def _enter(self, kind: str, params: dict[str, Any]) -> int:
        """登记一次调用并返回序号（线程安全）。"""
        with self._lock:
            self._calls += 1
            call_no = self._calls
            self.calls.append((kind, dict(params)))
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        return call_no

    def _maybe_fail(self, call_no: int, keys: tuple[str, ...]) -> None:
        """按预设行为决定是否抛错。"""
        if self.down:
            raise MerchantChannelError(
                "mock tool port down",
                code=MerchantErrorCode.SERVICE_UNAVAILABLE,
                http_status=503,
                retryable=True,
            )
        if any(k in self.fail_keys for k in keys if k):
            raise MerchantChannelError(
                "mock targeted failure",
                code=self.fail_code,
                http_status=500,
                retry_after=self.retry_after,
                retryable=True,
            )
        if self.always_fail or call_no <= self.fail_times:
            status = 429 if self.fail_code == MerchantErrorCode.RATE_LIMITED else 503
            raise MerchantChannelError(
                f"mock failure #{call_no}",
                code=self.fail_code,
                http_status=status,
                retry_after=self.retry_after,
                retryable=True,
            )

    def _ok(self, data: Any) -> MerchantQueryResult:
        """包装成功结果。"""
        return MerchantQueryResult(
            ok=True,
            data=data,
            request_id=f"mock_{uuid.uuid4().hex[:8]}",
            http_status=200,
            latency_ms=self.latency_ms,
        )

    # -- 端口实现 -----------------------------------------------------------

    def query_order(self, params: dict[str, Any]) -> MerchantQueryResult:
        """订单查询：`order_no` 不存在时按 40401 抛错（无数据返回）。"""
        call_no = self._enter("order", params)
        order_no = str(params.get("order_no", ""))
        if order_no and order_no in self.missing_orders:
            raise MerchantChannelError(
                "订单不存在",
                code=MerchantErrorCode.NOT_FOUND,
                http_status=404,
                retryable=False,
            )
        self._maybe_fail(call_no, (order_no, str(params.get("buyer_id", ""))))
        return self._ok(_mock_order(order_no or _ORDER_NO))

    def track_logistics(self, params: dict[str, Any]) -> MerchantQueryResult:
        """物流轨迹：未发货按商家口径返回 40401。"""
        call_no = self._enter("logistics", params)
        tracking_no = str(params.get("tracking_no", ""))
        order_no = str(params.get("order_no", ""))
        if order_no and order_no in self.missing_orders:
            raise MerchantChannelError(
                "订单不存在",
                code=MerchantErrorCode.NOT_FOUND,
                http_status=404,
                retryable=False,
            )
        self._maybe_fail(call_no, (tracking_no, order_no))
        return self._ok(
            {
                "company": params.get("company") or "ZTO",
                "company_code": "ZTO",
                "tracking_no": tracking_no or "780123456789",
                "status": "IN_TRANSIT",
                "latest": {
                    "time": "2026-01-02 09:00:00",
                    "desc": "快件已到达杭州转运中心",
                },
                "estimated_arrival": "2026-01-03",
                "traces": [
                    {
                        "time": "2026-01-01 20:00:00",
                        "desc": "商家已发货",
                        "status": "SHIPPED",
                    },
                    {
                        "time": "2026-01-02 09:00:00",
                        "desc": "快件已到达杭州转运中心",
                        "status": "IN_TRANSIT",
                    },
                ],
                "signed": False,
                "signed_at": None,
            }
        )

    def query_inventory(self, params: dict[str, Any]) -> MerchantQueryResult:
        """库存：部分 SKU 不存在时**只返回存在的**（商家 3 号口径）。"""
        call_no = self._enter("inventory", params)
        raw = str(params.get("sku_ids", ""))
        sku_ids = [s.strip() for s in raw.split(",") if s.strip()]
        self._maybe_fail(call_no, tuple(sku_ids))
        known = {
            "WK-BEAR-20": {
                "sku_id": "WK-BEAR-20",
                "title": "可爱小熊 20cm",
                "stock_status": "IN_STOCK",
                "available_qty": 128,
                "presale_days": None,
                "ship_within_hours": 24,
                "updated_at": "2026-01-02 08:00:00",
            },
            "WK-BEAR-30": {
                "sku_id": "WK-BEAR-30",
                "title": "可爱小熊 30cm",
                "stock_status": "PRESALE",
                "available_qty": 0,
                "presale_days": 7,
                "ship_within_hours": None,
                "updated_at": "2026-01-02 08:00:00",
            },
            "WK-CYBERSHIBA-24": {
                "sku_id": "WK-CYBERSHIBA-24",
                "title": "赛博柴犬 24cm",
                "stock_status": "OUT_OF_STOCK",
                "available_qty": 0,
                "presale_days": None,
                "ship_within_hours": None,
                "updated_at": "2026-01-02 08:00:00",
            },
        }
        items = [
            dict(known[s])
            for s in sku_ids
            if s in known and s not in self.missing_skus
        ]
        return self._ok({"items": items})

    def query_product(self, params: dict[str, Any]) -> MerchantQueryResult:
        """商品详情：`sku_id` 不存在时按 40401 抛错。"""
        call_no = self._enter("product", params)
        sku_id = str(params.get("sku_id", ""))
        if sku_id and sku_id in self.missing_skus:
            raise MerchantChannelError(
                "商品不存在",
                code=MerchantErrorCode.NOT_FOUND,
                http_status=404,
                retryable=False,
            )
        self._maybe_fail(call_no, (sku_id,))
        item = {
            "sku_id": sku_id or "WK-BEAR-20",
            "title": "可爱小熊 20cm",
            "category": "毛绒玩偶",
            "size": "20cm",
            "material": "水晶超绒",
            "filling": "三维中空 PP 棉",
            "age_range": "3 岁以上",
            "care_instruction": "可机洗",
            "packaging": "密封袋",
            "launch_date": "2025-09-01",
            "is_ip_collab": False,
            "list_price": 129.0,
            "current_price": 119.0,
            "stock_status": "现货",
            "updated_at": "2026-01-02 08:00:00",
        }
        return self._ok(
            {
                "total": 1,
                "page": int(params.get("page", 1) or 1),
                "page_size": int(params.get("page_size", 50) or 50),
                "items": [item],
            }
        )


_port: MerchantToolPort | None = None


def get_merchant_tool_port() -> MerchantToolPort:
    """取全局读端口单例。

    默认 `MockMerchantToolPort`：本机不可达商家测试环境（见交接文档 9.2），
    默认走真实 HTTP 会让每个查询都超时。联调/生产通过
    ``WK_TOOL_CHANNEL=http`` 切换。
    """
    global _port  # noqa: PLW0603
    if _port is None:
        if get_settings().tool_channel == "http":
            _port = HttpMerchantToolPort()
        else:
            _port = MockMerchantToolPort()
    return _port


def set_merchant_tool_port(port: MerchantToolPort | None) -> None:
    """替换全局读端口（**测试专用**）。"""
    global _port  # noqa: PLW0603
    _port = port


__all__ = [
    "MAX_SKU_PER_INVENTORY",
    "HttpMerchantToolPort",
    "MerchantQueryResult",
    "MerchantToolPort",
    "MockMerchantToolPort",
    "READ_TIMEOUT_SECONDS",
    "get_merchant_tool_port",
    "set_merchant_tool_port",
]
