"""物流轨迹查询工具（BE-07）。

调用商家 2 号接口 `GET /api/v1/logistics/track`。

关键约束：

* `traces[].desc` **原文引用**（任务书 2.4.4 表），不得改写——物流描述
  是快递商的正式记录，改写会变成「客服编造」；
* 未发货（40401）不是错误，是**正常业务状态**，要给「还没发货」的话术；
* 停滞天数直接取 `LogisticsQueryResponse.stalled_days`（schemas/tools.py
  已实现四种时间格式解析），用于「物流停滞」判断。
"""

from __future__ import annotations

import re
from typing import Any

from app.adapters.merchant_tools import get_merchant_tool_port
from app.core.enums import CompanyCode, LogisticsStatus
from app.core.errors import MerchantApiError, MerchantErrorCode
from app.core.logging import get_logger, mask_text
from app.schemas.tools import (
    COMPANY_CN,
    LOGISTICS_STATUS_CN,
    LogisticsQueryResponse,
    LogisticsTrace,
)
from app.services.tools.base import (
    ToolResult,
    attempts_of,
    call_with_retry,
    elapsed_ms,
    result_from_error,
)
from app.services.tools.identity import BuyerContext

logger = get_logger(__name__)

_TRACKING_RE = re.compile(r"^[A-Z]{2}\d{10,14}$")
_ORDER_NO_RE = re.compile(r"^\d{12}$")
_COMPANY_CODES = {c.value for c in CompanyCode}


def build_logistics_response(data: dict[str, Any]) -> LogisticsQueryResponse:
    """商家响应 → `LogisticsQueryResponse`。"""
    traces = [
        LogisticsTrace(
            time=str(t.get("time", "")),
            desc=mask_text(str(t.get("desc", ""))),
            status=_safe_status(t.get("status")),
        )
        for t in (data.get("traces") or [])
    ]
    return LogisticsQueryResponse(
        company=str(data.get("company", "")) or "",
        company_code=_safe_company(data.get("company_code")),
        tracking_no=mask_text(str(data.get("tracking_no", ""))),
        status=_safe_status(data.get("status")) or LogisticsStatus.PENDING,
        latest={
            "time": str((data.get("latest") or {}).get("time", "")),
            "desc": mask_text(str((data.get("latest") or {}).get("desc", ""))),
        },
        estimated_arrival=str(data.get("estimated_arrival") or "") or None,
        traces=traces,
        signed=bool(data.get("signed", False)),
        signed_at=str(data.get("signed_at") or "") or None,
    )


def _safe_status(raw: Any) -> LogisticsStatus | None:
    """物流状态归一（未知值返回 None，由上游给默认值）。"""
    if not raw:
        return None
    try:
        return LogisticsStatus(str(raw).upper())
    except ValueError:
        return None


def _safe_company(raw: Any) -> CompanyCode | None:
    """快递公司编码归一。"""
    if not raw:
        return None
    try:
        return CompanyCode(str(raw).upper())
    except ValueError:
        return None


def summarize_logistics(track: LogisticsQueryResponse) -> str:
    """生成送模型摘要（`desc` 原文引用，仅脱敏不改写）。"""
    parts = [f"物流状态：{track.status_cn}"]
    if track.tracking_no:
        parts.append(f"运单号 {track.tracking_no}")
    if track.latest:
        parts.append(
            f"最新：{track.latest.get('time', '')} {track.latest.get('desc', '')}".strip()
        )
    if track.estimated_arrival:
        parts.append(f"预计送达 {track.estimated_arrival}")
    stalled = track.stalled_days
    if stalled is not None and stalled >= 3 and not track.signed:
        parts.append(f"轨迹已停滞 {stalled} 天")
    return "；".join(parts) + "。"


def query_logistics(
    *,
    tracking_no: str = "",
    order_no: str = "",
    company: str = "",
    buyer_id: str = "",
    session_id: str = "",
    ctx: BuyerContext | None = None,
    port: Any = None,
    sleep: Any = None,
) -> ToolResult:
    """查询物流轨迹（BE-07 主入口）。

    Args:
        tracking_no: 运单号。
        order_no: 订单号（与运单号至少一个）。
        company: 快递公司编码（ZTO/YTO/SF）。
        buyer_id: 买家标识。
        session_id: 会话 ID。
        ctx: 已归一的买家上下文。
        port: 读端口（测试注入）。
        sleep: 退避函数（测试注入）。

    Returns:
        `ToolResult`。
    """
    import time as _time

    started = _time.perf_counter()
    tracking_no = (tracking_no or "").strip()
    order_no = (order_no or "").strip()
    company = (company or "").strip().upper()

    if not tracking_no and not order_no:
        return result_from_error(
            "logistics_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_MISSING,
                message="tracking_no 与 order_no 至少提供一个",
            ),
            latency_ms=elapsed_ms(started),
        )
    if tracking_no and not _TRACKING_RE.match(tracking_no):
        return result_from_error(
            "logistics_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID, message="tracking_no 格式不正确"
            ),
            latency_ms=elapsed_ms(started),
        )
    if company and company not in _COMPANY_CODES:
        return result_from_error(
            "logistics_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message=f"company 仅支持 {'/'.join(sorted(_COMPANY_CODES))}",
            ),
            latency_ms=elapsed_ms(started),
        )

    params: dict[str, Any] = {}
    if tracking_no:
        params["tracking_no"] = tracking_no
    if order_no:
        params["order_no"] = order_no
    if company:
        params["company"] = company

    port = port or get_merchant_tool_port()
    attempts = 1
    try:
        result, attempts = call_with_retry(
            "logistics_query",
            lambda: port.track_logistics(params),
            sleep=sleep,
        )
    except MerchantApiError as exc:
        return result_from_error(
            "logistics_query",
            exc,
            latency_ms=elapsed_ms(started),
            attempts=attempts_of(exc, attempts),
        )
    track = build_logistics_response(result.data or {})
    degraded: list[str] = []
    if track.stalled_days is not None and track.stalled_days >= 3 and not track.signed:
        degraded.append("LOGISTICS_STALLED")
    return ToolResult(
        tool="logistics_query",
        ok=True,
        data=track.model_dump(mode="json"),
        text=summarize_logistics(track),
        degraded=degraded,
        latency_ms=elapsed_ms(started),
        attempts=attempts,
    )


__all__ = ["build_logistics_response", "query_logistics", "summarize_logistics"]
