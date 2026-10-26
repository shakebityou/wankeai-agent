"""库存查询工具（BE-09）。

调用商家 3 号接口 `GET /api/v1/inventory/query`。

三条硬约束：

1. **实时查询**：不用 `v_inventory_realtime`（15 分钟延迟的物化视图），
   每次问答都打实时接口（任务书 BE-09 原文）；
2. **中文口径**：对外答复只出现 `现货` / `预售 N 天` / `缺货`，
   **不得暴露英文枚举**（`stock_status_cn` 是唯一转换出口）；
3. **50301 降级**：不可用时回「库存实时同步中」+ 引导加购，
   **绝不编造库存数**（`degraded=True` 且 `available_qty=None`）。
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.merchant_tools import MAX_SKU_PER_INVENTORY, get_merchant_tool_port
from app.core.enums import StockStatus
from app.core.errors import MerchantApiError, MerchantErrorCode
from app.core.logging import get_logger
from app.schemas.tools import InventoryItem, InventoryQueryResponse, stock_status_cn
from app.services.tools.base import (
    ToolResult,
    attempts_of,
    call_with_retry,
    elapsed_ms,
    result_from_error,
)

logger = get_logger(__name__)

#: 50301 降级话术（任务书 BE-09 DoD）。
_DEGRADE_TEXT = "库存实时同步中"

#: 中文 → 枚举（商家 4 号接口与主数据表用中文，需反查）。
_CN_TO_STATUS = {
    "现货": StockStatus.IN_STOCK,
    "缺货": StockStatus.OUT_OF_STOCK,
}


def _safe_status(raw: Any) -> StockStatus:
    """库存状态归一：英文枚举直接认，中文按主数据表口径反查。"""
    if not raw:
        return StockStatus.OUT_OF_STOCK
    text = str(raw).strip().upper()
    try:
        return StockStatus(text)
    except ValueError:
        pass
    if text in _CN_TO_STATUS:
        return _CN_TO_STATUS[text]
    if text.startswith("预售"):
        return StockStatus.PRESALE
    return StockStatus.OUT_OF_STOCK


def _presale_days(raw: Any) -> int | None:
    """从 `预售(7天)` / `7` / `预售 7 天` 里提取天数。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return int(digits) if digits else None


def build_inventory_response(data: dict[str, Any], *, degraded: bool = False) -> InventoryQueryResponse:
    """商家响应 → `InventoryQueryResponse`。

    部分 SKU 不存在时商家**不返回该项**（不是报错），因此这里**不补占位**：
    补一个「缺货」会把「查不到」伪装成「卖完了」，是编造事实。
    """
    items = [
        InventoryItem(
            sku_id=str(it.get("sku_id", "")),
            title=str(it.get("title", "")),
            stock_status=_safe_status(it.get("stock_status")),
            available_qty=(
                int(it["available_qty"]) if it.get("available_qty") is not None else None
            ),
            presale_days=_presale_days(it.get("presale_days")),
            ship_within_hours=(
                int(it["ship_within_hours"])
                if it.get("ship_within_hours") is not None
                else None
            ),
            updated_at=_parse_dt(it.get("updated_at")),
        )
        for it in (data.get("items") or [])
    ]
    return InventoryQueryResponse(items=items, degraded=degraded)


def _parse_dt(raw: Any) -> Any:
    """解析 `yyyy-MM-dd HH:mm:ss`（容忍纯日期与 None）。"""
    if not raw:
        return None
    from datetime import datetime as _dt

    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.strptime(text, fmt)
        except ValueError:
            continue
    return None


def summarize_inventory(inv: InventoryQueryResponse) -> str:
    """生成送模型摘要（**只用中文口径**）。"""
    if inv.degraded or not inv.items:
        return f"{_DEGRADE_TEXT}，可以先加购留意补货哦～"
    parts = []
    for it in inv.items:
        qty = f"约 {it.available_qty} 件" if it.available_qty is not None else ""
        parts.append(f"{it.title}（{it.sku_id}）：{it.status_cn}{qty}".rstrip("，"))
    return "；".join(parts) + "。"


def query_inventory(
    sku_ids: list[str] | str,
    *,
    with_presale: bool = True,
    port: Any = None,
    sleep: Any = None,
) -> ToolResult:
    """查询库存（BE-09 主入口）。

    Args:
        sku_ids: SKU 列表或逗号分隔字符串（单次 ≤ 50）。
        with_presale: 是否包含预售商品（商家参数）。
        port: 读端口（测试注入）。
        sleep: 退避函数（测试注入）。

    Returns:
        `ToolResult`：`data` 为 `InventoryQueryResponse.model_dump()`。
    """
    started = time.perf_counter()
    if isinstance(sku_ids, str):
        sku_list = [s.strip() for s in sku_ids.split(",") if s.strip()]
    else:
        sku_list = [str(s).strip() for s in sku_ids if str(s).strip()]

    if not sku_list:
        return result_from_error(
            "inventory_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_MISSING, message="sku_ids 不能为空"
            ),
            latency_ms=elapsed_ms(started),
        )
    if len(sku_list) > MAX_SKU_PER_INVENTORY:
        return result_from_error(
            "inventory_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message=f"单次最多 {MAX_SKU_PER_INVENTORY} 个 SKU",
            ),
            latency_ms=elapsed_ms(started),
        )

    params = {"sku_ids": ",".join(sku_list), "with_presale": with_presale}
    port = port or get_merchant_tool_port()
    attempts = 1
    degraded = False
    try:
        result, attempts = call_with_retry(
            "inventory_query",
            lambda: port.query_inventory(params),
            sleep=sleep,
        )
    except MerchantApiError as exc:
        # 50301 / 50401 → 降级话术，**不编造库存数**
        if int(exc.code) in (
            int(MerchantErrorCode.SERVICE_UNAVAILABLE),
            int(MerchantErrorCode.UPSTREAM_TIMEOUT),
        ):
            degraded = True
            inv = InventoryQueryResponse(items=[], degraded=True)
            return ToolResult(
                tool="inventory_query",
                ok=True,
                data=inv.model_dump(mode="json"),
                text=summarize_inventory(inv),
                degraded=["INVENTORY_DEGRADED", "STALE_DATA"],
                latency_ms=elapsed_ms(started),
                attempts=attempts,
            )
        return result_from_error(
            "inventory_query",
            exc,
            latency_ms=elapsed_ms(started),
            attempts=attempts_of(exc, attempts),
        )
    inv = build_inventory_response(result.data or {}, degraded=degraded)
    return ToolResult(
        tool="inventory_query",
        ok=True,
        data=inv.model_dump(mode="json"),
        text=summarize_inventory(inv),
        degraded=list(inv.degraded and ["INVENTORY_DEGRADED"] or []),
        latency_ms=elapsed_ms(started),
        attempts=attempts,
    )


__all__ = ["build_inventory_response", "query_inventory", "summarize_inventory"]
