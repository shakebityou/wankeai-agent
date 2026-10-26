"""契约 B · 业务查询内部接口（BE-07 / BE-08 / BE-09）。

| 路由 | 上游 | 关键约束 |
| --- | --- | --- |
| `GET /internal/v1/tools/order` | 商家 1 号 | 强制校验 `buyer_id` 归属；日志脱敏前 4 后 4 |
| `GET /internal/v1/tools/logistics` | 商家 2 号 | `traces[].desc` 原文引用 |
| `GET /internal/v1/tools/inventory` | 商家 3 号 | 对外中文口径，不暴露英文枚举 |
| `GET /internal/v1/tools/product` | 商家 4 号 | 支持 `updated_since` 增量同步 |

**为什么不直接透传商家路径**：与出站消息同一个理由——`/api/v1/*` 是
「我方作为调用方」访问商家的路径，`/internal/v1/*` 才是「项目组内部服务」
调用我方的路径。两者混在一起，联调时无法从路径判断请求方向。

## 越权拒绝怎么返回

BE-08 DoD① 要求「越权 100% 拒绝」。这里返回 **403 + 明确话术**，
而不是 404「订单不存在」：用 404 伪装越权会让真实「订单不存在」与
「不属于你」无法区分，坐席与监控都看不出攻击痕迹。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from app.api.deps import InternalAuthGuard
from app.core.enums import Channel
from app.core.errors import ForbiddenError, MerchantErrorCode, ValidationError
from app.core.logging import mask_order_no
from app.core.response import ok
from app.services.tools.inventory import query_inventory
from app.services.tools.logistics import query_logistics
from app.services.tools.order import query_order
from app.services.tools.product import CATEGORIES, query_product

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[InternalAuthGuard])


class OrderQueryParams(BaseModel):
    """订单查询参数（对齐商家 1.3 节）。"""

    order_no: str = Field(min_length=12, max_length=12, description="12 位订单号")
    buyer_id: str = Field(min_length=1, max_length=128, description="买家标识")

    @field_validator("order_no")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("order_no 必须为 12 位数字")
        return v


class LogisticsQueryParams(BaseModel):
    """物流查询参数（对齐商家 2.3 节，两个条件至少一个）。"""

    tracking_no: str | None = Field(default=None, description="运单号")
    order_no: str | None = Field(default=None, description="订单号")
    company: str | None = Field(default=None, description="快递公司 ZTO/YTO/SF")

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str | None) -> str | None:
        if v is not None and v.upper() not in ("ZTO", "YTO", "SF"):
            raise ValueError("company 仅支持 ZTO/YTO/SF")
        return v.upper() if v else v

    def model_post_init(self, __context: Any) -> None:
        """tracking_no 与 order_no 至少一个。"""
        if not self.tracking_no and not self.order_no:
            raise ValueError("tracking_no 与 order_no 至少提供一个")


class InventoryQueryParams(BaseModel):
    """库存查询参数（对齐商家 3.3 节）。"""

    sku_ids: str = Field(min_length=1, description="逗号分隔，单次 ≤ 50")
    with_presale: bool = True


class ProductQueryParams(BaseModel):
    """商品查询参数（对齐商家 4.3 节，sku_id 与 category 二选一）。"""

    sku_id: str | None = None
    category: str | None = None
    updated_since: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class TicketCreateParams(BaseModel):
    """商家 5 号接口：创建/同步工单（BE-14）。

    ⚠️ 本接口**不做幂等**：幂等由 `HandoffService` 用 `session_id` 保证
    （`tickets.session_id` 唯一索引 + 7 天幂等缓存）。直接调本接口的调用方
    必须自己先确认该 `session_id` 没有工单，否则会在商家侧产生重复工单。
    """

    session_id: str = Field(min_length=1, max_length=64, description="会话 ID")
    ticket_no: str | None = Field(default=None, description="我方工单号")
    reason: str = Field(default="UNRESOLVED", description="转人工原因（5 值）")
    priority: str = Field(default="NORMAL", description="优先级")
    channel: str | None = Field(default=None, description="渠道")
    summary: str = Field(default="", max_length=500, description="问题摘要")
    buyer_id: str = Field(default="", description="买家标识")
    entities: dict[str, Any] = Field(default_factory=dict, description="结构化实体")
    tried_actions: list[Any] = Field(default_factory=list, description="已尝试动作")
    transcript: list[dict[str, Any]] = Field(
        default_factory=list, description="对话摘录（≤20 轮）"
    )


def _guard_tool(result: Any) -> dict[str, Any]:
    """把 `ToolResult` 翻成 HTTP 响应，失败按错误码映射状态码。

    越权（40301）→ 403；参数错（40001/40002）→ 400；
    订单不存在（40401）→ 404；其余 → 400（上游问题，话术由策略表给出）。
    """
    if result.ok:
        return ok(
            {
                "tool": result.tool,
                "data": result.data,
                "text": result.text,
                "attempts": result.attempts,
                "latency_ms": result.latency_ms,
                "degraded": result.degraded,
            }
        )
    code = result.error_code
    if code == 40301:
        raise ForbiddenError(result.user_message or "这个订单不在当前账号下")
    if code in (40001, 40002):
        raise ValidationError(result.user_message or "请求参数不正确")
    if code == 40401:
        raise ValidationError(result.user_message or "没有查到相关数据")
    if code == 42901:
        raise ValidationError("商家接口限流，请稍后再试")
    raise ValidationError(result.user_message or "系统查询中，请稍候")


@router.get("/order", summary="订单查询（强制校验归属）")
def tool_order(
    order_no: Annotated[str, Query(min_length=12, max_length=12)],
    buyer_id: Annotated[str, Query(min_length=1)],
    channel: Annotated[Channel | None, Query(description="下单渠道")] = None,
    session_id: Annotated[str | None, Query(description="会话 ID（频次计数）")] = None,
    channel_buyer_id: Annotated[
        str | None, Query(description="渠道透传买家（优先于 buyer_id）")
    ] = None,
) -> dict[str, Any]:
    """查询订单详情。

    ⚠️ 响应里的 `receiver` 已脱敏（手机号前 3 后 4、姓名留姓、地址仅省市级），
    项目组**不得还原**（BE-17 DoD）。
    """
    if not order_no.isdigit():
        raise ValidationError("order_no 必须为 12 位数字")
    result = query_order(
        order_no,
        buyer_id=channel_buyer_id or buyer_id,
        channel=channel.value if channel else "",
        session_id=session_id or "",
    )
    return _guard_tool(result)


@router.get("/logistics", summary="物流轨迹查询")
def tool_logistics(
    tracking_no: Annotated[str | None, Query(description="运单号")] = None,
    order_no: Annotated[str | None, Query(description="订单号")] = None,
    company: Annotated[str | None, Query(description="ZTO/YTO/SF")] = None,
    buyer_id: Annotated[str | None, Query(description="买家标识")] = None,
    session_id: Annotated[str | None, Query(description="会话 ID")] = None,
) -> dict[str, Any]:
    """查询物流轨迹（`traces[].desc` 原文引用）。"""
    result = query_logistics(
        tracking_no=tracking_no or "",
        order_no=order_no or "",
        company=company or "",
        buyer_id=buyer_id or "",
        session_id=session_id or "",
    )
    return _guard_tool(result)


@router.get("/inventory", summary="库存查询（中文口径）")
def tool_inventory(
    sku_ids: Annotated[str, Query(description="逗号分隔，单次 ≤ 50")],
    with_presale: Annotated[bool, Query(description="是否含预售")] = True,
) -> dict[str, Any]:
    """查询库存。

    对外答复只出现 `现货` / `预售 N 天` / `缺货`，**不暴露英文枚举**（BE-09）。
    """
    result = query_inventory(sku_ids, with_presale=with_presale)
    return _guard_tool(result)


@router.get("/product", summary="商品详情查询")
def tool_product(
    sku_id: Annotated[str | None, Query(description="SKU 编码")] = None,
    category: Annotated[str | None, Query(description="类目")] = None,
    updated_since: Annotated[str | None, Query(description="增量同步起点")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """查询商品详情（`sku_id` 与 `category` 二选一）。

    ⚠️ 本接口返回的数据**不得写入向量库**（BE-05 DoD：价格是会变的数字）。
    """
    result = query_product(
        sku_id=sku_id or "",
        category=category or "",
        updated_since=updated_since or "",
        page=page,
        page_size=page_size,
    )
    return _guard_tool(result)


@router.post("/ticket", summary="创建/同步工单（商家 5 号接口）")
def tool_ticket(payload: TicketCreateParams) -> dict[str, Any]:
    """把工单推送到商家客服系统，返回商家侧工单号。

    ⚠️ **本接口不做幂等**。幂等由 `HandoffService` 用 `session_id` 保证
    （`tickets.session_id` 唯一索引 + 7 天幂等缓存）。直接调用方必须自己
    先确认该 `session_id` 没有工单，否则商家侧会产生重复工单。

    限流：`ticket_create` 走独立令牌桶（`WK_TICKET_QPS`，默认 5 QPS），
    超限返回 42901，调用方按 `retry_after` 退避。
    """
    from app.adapters.merchant_ticket import get_ticket_client

    client = get_ticket_client()
    result = client.create_ticket(
        {
            "session_id": payload.session_id,
            "ticket_no": payload.ticket_no or "",
            "reason": payload.reason,
            "priority": payload.priority,
            "channel": payload.channel or "",
            "summary": payload.summary,
            "buyer_id": payload.buyer_id,
            "entities": payload.entities,
            "tried_actions": payload.tried_actions,
            "transcript": payload.transcript,
        }
    )
    if result.ok:
        return ok(
            {
                "tool": "ticket_create",
                "data": {
                    "merchant_ticket_no": result.ticket_no,
                    "session_id": payload.session_id,
                },
                "text": f"工单已创建：{result.ticket_no}",
                "attempts": result.attempts,
            }
        )
    code = result.error_code or MerchantErrorCode.SERVICE_UNAVAILABLE
    message = result.message or "工单创建失败，请稍后重试"
    if code == MerchantErrorCode.RATE_LIMITED:
        raise ValidationError("商家接口限流，请稍后再试")
    if code in (MerchantErrorCode.PARAM_MISSING, MerchantErrorCode.PARAM_INVALID):
        raise ValidationError(message)
    if code == MerchantErrorCode.NO_PERMISSION:
        raise ForbiddenError(message)
    raise ValidationError("工单创建失败，请稍后重试")


__all__ = [
    "InventoryQueryParams",
    "LogisticsQueryParams",
    "OrderQueryParams",
    "ProductQueryParams",
    "TicketCreateParams",
    "router",
    "tool_inventory",
    "tool_logistics",
    "tool_order",
    "tool_product",
    "tool_ticket",
]
