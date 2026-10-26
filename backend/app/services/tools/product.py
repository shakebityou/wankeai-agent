"""商品详情查询工具（BE-09 的读侧 + BE-05 的向量库禁入清单）。

调用商家 4 号接口 `GET /api/v1/product/detail`。

关键点：

* `sku_id` 与 `category` **二选一**（商家口径），二者都传或都不传都是 40002；
* `updated_since` 增量同步原样透传并**回显**（`ProductQueryResponse.updated_since`）；
* 该接口返回的 `stock_status` 是**中文口径**（`现货` / `预售(7天)` / `缺货`），
  与库存接口的英文枚举不同——这是商家接口本身的设计，不要强行统一；
* **本工具返回的数据一律不进向量库**（BE-05 DoD：价格是会变的数字）。
"""

from __future__ import annotations

import time
from typing import Any

from app.adapters.merchant_tools import get_merchant_tool_port
from app.core.errors import MerchantApiError, MerchantErrorCode
from app.core.logging import get_logger
from app.schemas.tools import ProductItem, ProductQueryResponse
from app.services.tools.base import (
    ToolResult,
    attempts_of,
    call_with_retry,
    elapsed_ms,
    result_from_error,
)

logger = get_logger(__name__)

#: 商家 4 号接口的类目白名单（毛绒玩偶/挂件/抱枕/搪胶潮玩/盲盒）。
CATEGORIES = ("毛绒玩偶", "挂件", "抱枕", "搪胶潮玩", "盲盒")

MAX_PAGE_SIZE = 200


def build_product_response(data: dict[str, Any], *, updated_since: str | None = None) -> ProductQueryResponse:
    """商家响应 → `ProductQueryResponse`。"""
    items = [
        ProductItem(
            sku_id=str(it.get("sku_id", "")),
            title=str(it.get("title", "")),
            category=str(it.get("category", "")),
            size=_opt_str(it.get("size")),
            material=_opt_str(it.get("material")),
            filling=_opt_str(it.get("filling")),
            age_range=_opt_str(it.get("age_range")),
            care_instruction=_opt_str(it.get("care_instruction")),
            packaging=_opt_str(it.get("packaging")),
            launch_date=_opt_str(it.get("launch_date")),
            is_ip_collab=bool(it.get("is_ip_collab", False)),
            list_price=_opt_float(it.get("list_price")),
            current_price=_opt_float(it.get("current_price")),
            stock_status=_opt_str(it.get("stock_status")),
            updated_at=_parse_dt(it.get("updated_at")),
        )
        for it in (data.get("items") or [])
    ]
    return ProductQueryResponse(items=items, updated_since=updated_since)


def _opt_str(raw: Any) -> str | None:
    """空串归一为 None（区分「没有这个字段」与「空值」）。"""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _opt_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_dt(raw: Any) -> Any:
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


def summarize_product(prod: ProductQueryResponse) -> str:
    """生成送模型摘要。"""
    if not prod.items:
        return "没有查到相关商品。"
    parts = []
    for it in prod.items:
        head = f"{it.title}（{it.sku_id}）"
        bits = []
        if it.category:
            bits.append(it.category)
        if it.size:
            bits.append(it.size)
        if it.current_price is not None:
            bits.append(f"现价 {it.current_price:.2f} 元")
        if it.stock_status:
            bits.append(it.stock_status)
        parts.append(head + ("：" + "，".join(bits) if bits else ""))
    return "；".join(parts) + "。"


def query_product(
    *,
    sku_id: str = "",
    category: str = "",
    updated_since: str = "",
    page: int = 1,
    page_size: int = 50,
    port: Any = None,
    sleep: Any = None,
) -> ToolResult:
    """查询商品详情（BE-09 读侧主入口）。

    Args:
        sku_id: SKU 编码（与 `category` 二选一）。
        category: 类目（与 `sku_id` 二选一）。
        updated_since: 增量同步起点（`yyyy-MM-dd HH:mm:ss`）。
        page: 页码。
        page_size: 每页条数（默认 50，最大 200）。
        port: 读端口（测试注入）。
        sleep: 退避函数（测试注入）。

    Returns:
        `ToolResult`。
    """
    started = time.perf_counter()
    sku_id = (sku_id or "").strip()
    category = (category or "").strip()
    updated_since = (updated_since or "").strip()

    if bool(sku_id) == bool(category):
        return result_from_error(
            "product_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message="sku_id 与 category 必须且只能提供一个",
            ),
            latency_ms=elapsed_ms(started),
        )
    if category and category not in CATEGORIES:
        return result_from_error(
            "product_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message=f"category 仅支持 {'/'.join(CATEGORIES)}",
            ),
            latency_ms=elapsed_ms(started),
        )
    if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
        return result_from_error(
            "product_query",
            MerchantApiError(
                code=MerchantErrorCode.PARAM_INVALID,
                message=f"page ≥ 1 且 1 ≤ page_size ≤ {MAX_PAGE_SIZE}",
            ),
            latency_ms=elapsed_ms(started),
        )

    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if sku_id:
        params["sku_id"] = sku_id
    if category:
        params["category"] = category
    if updated_since:
        params["updated_since"] = updated_since

    port = port or get_merchant_tool_port()
    attempts = 1
    try:
        result, attempts = call_with_retry(
            "product_query", lambda: port.query_product(params), sleep=sleep
        )
    except MerchantApiError as exc:
        return result_from_error(
            "product_query",
            exc,
            latency_ms=elapsed_ms(started),
            attempts=attempts_of(exc, attempts),
        )
    prod = build_product_response(result.data or {}, updated_since=updated_since or None)
    return ToolResult(
        tool="product_query",
        ok=True,
        data=prod.model_dump(mode="json"),
        text=summarize_product(prod),
        latency_ms=elapsed_ms(started),
        attempts=attempts,
    )


__all__ = ["build_product_response", "query_product", "summarize_product"]
