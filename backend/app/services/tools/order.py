"""订单查询工具（BE-07）。

调用商家 1 号接口 `GET /api/v1/order/query`，经过：

1. 参数校验（订单号 12 位数字）；
2. 带退避的调用（429 服从 `Retry-After`，503/504 指数退避最多 3 次）；
3. **越权校验**（BE-08）：比对返回的 `buyer_id` 与当前会话绑定身份；
4. 二次脱敏兜底（商家理论上已脱敏，我方不得信任上游的脱敏质量）；
5. 组装 `OrderQueryResponse` + 生成送模型的自然语言摘要。

## 为什么要做「二次脱敏兜底」

商家说明书承诺 `receiver` 已脱敏，但 BE-17 DoD④ 要求「送模型的 prompt
零完整手机号/地址」。上游脱敏是**别的人写的正则**，我方不能把合规责任
外包出去——因此这里再过一遍 `mask_text`，双保险。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.adapters.merchant_message import MerchantChannelError
from app.adapters.merchant_tools import (
    MerchantQueryResult,
    get_merchant_tool_port,
)
from app.core.enums import Channel, OrderStatus
from app.core.errors import MerchantApiError, MerchantErrorCode
from app.core.logging import get_logger, mask_order_no, mask_text
from app.schemas.tools import (
    ORDER_STATUS_CN,
    OrderAmount,
    OrderItem,
    OrderLogisticsBrief,
    OrderQueryResponse,
    ReceiverInfo,
)
from app.services.tools.base import (
    ToolResult,
    attempts_of,
    call_with_retry,
    elapsed_ms,
    result_from_error,
)
from app.services.tools.identity import (
    BuyerContext,
    ensure_order_access,
    resolve_buyer,
)

logger = get_logger(__name__)

_ORDER_NO_RE = re.compile(r"^\d{12}$")

#: 订单查询的降级话术（任务书 BE-07 DoD：50401 重试 3 次仍失败 → 转人工）。
_ORDER_DEGRADE_TEXT = "系统查询中，请稍候"


def _parse_dt(raw: Any) -> datetime | None:
    """解析 `yyyy-MM-dd HH:mm:ss`（容忍纯日期）。"""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _safe_channel(raw: Any) -> Channel | None:
    """渠道枚举归一（未知渠道返回 None，不抛错）。"""
    if not raw:
        return None
    try:
        return Channel(str(raw).upper())
    except ValueError:
        return None


def build_order_response(data: dict[str, Any]) -> OrderQueryResponse:
    """商家响应 → `OrderQueryResponse`（**出口统一脱敏**）。

    Args:
        data: 商家 1 号接口 `data` 字段。

    Returns:
        校验通过的响应对象。

    Raises:
        ValueError: 订单号不是 12 位数字。
    """
    amount_raw = data.get("amount") or {}
    items = [
        OrderItem(
            sku_id=str(it.get("sku_id", "")),
            title=mask_text(str(it.get("title", ""))),
            qty=max(1, int(it.get("qty", 1) or 1)),
            price=float(it.get("price", 0.0) or 0.0),
        )
        for it in (data.get("items") or [])
    ]
    logistics_raw = data.get("logistics")
    logistics = None
    if logistics_raw:
        logistics = OrderLogisticsBrief(
            company=mask_text(str(logistics_raw.get("company", ""))),
            tracking_no=mask_text(str(logistics_raw.get("tracking_no", ""))),
            latest={
                "time": str((logistics_raw.get("latest") or {}).get("time", "")),
                "desc": mask_text(str((logistics_raw.get("latest") or {}).get("desc", ""))),
            },
        )
    receiver_raw = data.get("receiver")
    receiver = None
    if receiver_raw:
        # 商家已脱敏；这里再过一遍是**兜底**，不是替代（BE-17 DoD④）。
        receiver = ReceiverInfo(
            name=mask_text(str(receiver_raw.get("name", ""))),
            phone=mask_text(str(receiver_raw.get("phone", ""))),
            address=mask_text(str(receiver_raw.get("address", ""))),
        )
    return OrderQueryResponse(
        order_no=str(data.get("order_no", "")),
        status=OrderStatus(str(data.get("status", "CREATED")).upper()),
        buyer_id=str(data.get("buyer_id", "")),
        channel=_safe_channel(data.get("channel")),
        created_at=_parse_dt(data.get("created_at")),
        paid_at=_parse_dt(data.get("paid_at")),
        amount=OrderAmount(
            total=float(amount_raw.get("total", 0.0) or 0.0),
            discount=float(amount_raw.get("discount", 0.0) or 0.0),
            freight=float(amount_raw.get("freight", 0.0) or 0.0),
            paid=float(amount_raw.get("paid", 0.0) or 0.0),
        ),
        items=items,
        logistics=logistics,
        estimated_arrival=str(data.get("estimated_arrival") or "") or None,
        receiver=receiver,
    )


def summarize_order(order: OrderQueryResponse) -> str:
    """生成送模型的自然语言摘要。

    ⚠️ 摘要里**不得出现完整订单号**（前 4 后 4，BE-08 DoD③），
    也不得出现完整手机号/地址（BE-07 DoD）。
    """
    parts = [f"订单 {mask_order_no(order.order_no)}，状态：{order.status_cn}"]
    if order.paid_at:
        parts.append(f"付款时间 {order.paid_at:%Y-%m-%d %H:%M}")
    parts.append(f"实付 {order.amount.paid:.2f} 元")
    if order.items:
        names = "、".join(f"{it.title}x{it.qty}" for it in order.items)
        parts.append(f"商品：{names}")
    if order.logistics and order.logistics.tracking_no:
        parts.append(
            f"物流：{order.logistics.company} {mask_text(order.logistics.tracking_no)}"
        )
    if order.estimated_arrival:
        parts.append(f"预计送达 {order.estimated_arrival}")
    return "；".join(parts) + "。"


def query_order(
    order_no: str,
    *,
    ctx: BuyerContext | None = None,
    buyer_id: str = "",
    channel: str = "",
    session_id: str = "",
    port: Any = None,
    sleep: Any = None,
) -> ToolResult:
    """查询订单（BE-07 主入口）。

    Args:
        order_no: 12 位订单号。
        ctx: 已归一的买家上下文；给了就不再从散参数重建。
        buyer_id: 渠道透传的买家标识（`ctx` 未提供时使用）。
        channel: 渠道。
        session_id: 会话 ID。
        port: 读端口（测试注入）。
        sleep: 退避函数（测试注入假时钟）。

    Returns:
        `ToolResult`：成功时 `data` 为 `OrderQueryResponse.model_dump()`，
        `text` 为自然语言摘要。

    Note:
        越权时不返回「订单不存在」，而是返回**明确的无权限话术**——
        这是 BE-08 DoD①「越权 100% 拒绝」的可观测要求（日志与用户话术
        都要能区分「不存在」与「不属于你」）。
    """
    started = __import__("time").perf_counter()
    order_no = (order_no or "").strip()
    ctx = ctx or resolve_buyer(
        channel_buyer_id=buyer_id,
        session_buyer_id="",
        session_id=session_id,
        channel=channel,
    )

    if not _ORDER_NO_RE.match(order_no):
        return result_from_error(
            "order_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message="order_no 必须为 12 位数字",
            ),
            latency_ms=elapsed_ms(started),
        )

    port = port or get_merchant_tool_port()
    attempts = 1
    try:
        result, attempts = call_with_retry(
            "order_query", lambda: port.query_order({"order_no": order_no}), sleep=sleep
        )
    except MerchantApiError as exc:
        logger.warning(
            "订单查询失败",
            extra={
                "extra": {
                    "order_no": mask_order_no(order_no),
                    "error_code": int(exc.code),
                }
            },
        )
        return result_from_error(
            "order_query",
            exc,
            latency_ms=elapsed_ms(started),
            attempts=attempts_of(exc, attempts),
        )

    # -- BE-08 越权校验 ----------------------------------------------------
    try:
        ensure_order_access(result.data or {}, ctx)
    except PermissionError as exc:
        return ToolResult(
            tool="order_query",
            ok=False,
            error_code=int(MerchantErrorCode.NO_PERMISSION),
            user_message=getattr(exc, "user_message", None),
            degraded=["ORDER_QUERY_FAILED", "ACCESS_DENIED"],
            latency_ms=elapsed_ms(started),
            attempts=attempts,
        )

    order = build_order_response(result.data or {})
    return ToolResult(
        tool="order_query",
        ok=True,
        data=order.model_dump(mode="json"),
        text=summarize_order(order),
        latency_ms=elapsed_ms(started),
        attempts=attempts,
    )


__all__ = ["build_order_response", "query_order", "summarize_order"]
