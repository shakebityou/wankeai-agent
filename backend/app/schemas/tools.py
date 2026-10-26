"""契约 B · 业务工具服务（任务书 2.4.4 节）。

工具层是**唯一**允许访问商家接口的地方（HC-3 硬约束：会变的数字永远走接口）。

| 方法 | 路径 | 上游 | 关键约束 |
| --- | --- | --- | --- |
| GET | `/internal/v1/tools/order` | 商家 1 号接口 | 强制校验 `buyer_id` 归属；日志脱敏前 4 后 4 |
| GET | `/internal/v1/tools/logistics` | 商家 2 号接口 | `traces[].desc` 原文引用时按业务口径转述 |
| GET | `/internal/v1/tools/inventory` | 商家 3 号接口 | 对外答复用中文口径，**不暴露英文枚举** |
| GET | `/internal/v1/tools/product` | 商家 4 号接口 | 支持 `updated_since` 增量同步 |
| POST | `/internal/v1/tools/ticket` | 商家 5 号接口 | **按 `session_id` 幂等**；5 QPS 退避 + 本地队列 |

**库存状态映射（必须做，来自商家 3.5 节）**：

| 接口枚举 | 中文口径（对外） | 对应商品主数据表 |
| --- | --- | --- |
| `IN_STOCK` | 现货 | 现货 |
| `PRESALE` | 预售 N 天 | 预售(N天) |
| `OUT_OF_STOCK` | 缺货 | 缺货 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import (
    Channel,
    CompanyCode,
    LogisticsStatus,
    OrderStatus,
    StockStatus,
    TicketStatus,
)

#: 库存枚举 → 中文口径（**对外答复唯一出口**，BE-09 DoD）
STOCK_STATUS_CN: dict[StockStatus, str] = {
    StockStatus.IN_STOCK: "现货",
    StockStatus.PRESALE: "预售",
    StockStatus.OUT_OF_STOCK: "缺货",
}

#: 订单状态 → 中文口径
ORDER_STATUS_CN: dict[OrderStatus, str] = {
    OrderStatus.CREATED: "已下单",
    OrderStatus.PAID: "已付款",
    OrderStatus.SHIPPED: "已发货",
    OrderStatus.DELIVERED: "已签收",
    OrderStatus.CLOSED: "已关闭",
}

#: 物流状态 → 中文口径
LOGISTICS_STATUS_CN: dict[LogisticsStatus, str] = {
    LogisticsStatus.PENDING: "待揽收",
    LogisticsStatus.COLLECTED: "已揽收",
    LogisticsStatus.IN_TRANSIT: "运输中",
    LogisticsStatus.OUT_FOR_DELIVERY: "派送中",
    LogisticsStatus.DELIVERED: "已签收",
    LogisticsStatus.EXCEPTION: "异常",
    LogisticsStatus.RETURNED: "已退回",
}

#: 快递公司 → 中文名
COMPANY_CN: dict[CompanyCode, str] = {
    CompanyCode.ZTO: "中通",
    CompanyCode.YTO: "圆通",
    CompanyCode.SF: "顺丰",
}


def stock_status_cn(status: StockStatus, presale_days: int | None = None) -> str:
    """库存状态转中文口径。

    Args:
        status: 库存枚举。
        presale_days: 预售天数，仅 `PRESALE` 时有意义（7 / 15 / 30）。

    Returns:
        `现货` / `预售 7 天` / `缺货`。

    Note:
        BE-09 DoD：**对外答复不得出现英文枚举**，本函数是唯一转换出口。
    """
    base = STOCK_STATUS_CN.get(status, "以实际为准")
    if status == StockStatus.PRESALE and presale_days:
        return f"预售 {presale_days} 天"
    return base


# ---------------------------------------------------------------------------
# 订单（商家 1 号接口）
# ---------------------------------------------------------------------------


class OrderAmount(BaseModel):
    """订单金额明细（单位：元）。"""

    total: float = Field(default=0.0, description="订单总额")
    discount: float = Field(default=0.0, description="优惠金额")
    freight: float = Field(default=0.0, description="运费")
    paid: float = Field(default=0.0, description="实付金额")


class OrderItem(BaseModel):
    """订单商品行。"""

    sku_id: str = Field(description="SKU 编码")
    title: str = Field(default="", description="商品名")
    qty: int = Field(default=1, ge=1, description="数量")
    price: float = Field(default=0.0, description="单价")


class ReceiverInfo(BaseModel):
    """收件人信息。

    ⚠️ 商家接口**已脱敏**：手机号前 3 后 4、姓名留姓、地址仅省市级。
    项目组**不得尝试还原**，也不得在日志中打印完整值（BE-17 DoD）。
    """

    name: str = Field(default="", description="收件人姓名（已脱敏，如「张**」）")
    phone: str = Field(default="", description="手机号（已脱敏，如「138****8888」）")
    address: str = Field(default="", description="地址（已脱敏，仅省市级）")


class OrderLogisticsBrief(BaseModel):
    """订单内嵌的物流摘要。"""

    company: str = Field(default="", description="快递公司")
    tracking_no: str = Field(default="", description="运单号")
    latest: dict[str, Any] = Field(
        default_factory=dict, description="最新轨迹 {time, desc}"
    )


class OrderQueryResponse(BaseModel):
    """订单查询响应（对齐商家 1.5 节）。

    ⚠️ `receiver` 已由商家脱敏；**响应中不得出现完整手机号 / 完整地址**
    （BE-07 DoD 有正则扫描用例）。
    """

    order_no: str = Field(description="订单号（12 位数字）")
    status: OrderStatus = Field(description="订单状态")
    buyer_id: str = Field(default="", description="买家标识")
    channel: Channel | None = Field(default=None, description="下单渠道")
    created_at: datetime | None = Field(default=None, description="下单时间")
    paid_at: datetime | None = Field(default=None, description="付款时间")
    amount: OrderAmount = Field(default_factory=OrderAmount, description="金额明细")
    items: list[OrderItem] = Field(default_factory=list, description="商品行")
    logistics: OrderLogisticsBrief | None = Field(
        default=None, description="物流摘要"
    )
    estimated_arrival: str | None = Field(default=None, description="预计送达")
    receiver: ReceiverInfo | None = Field(default=None, description="收件人（已脱敏）")

    @field_validator("order_no")
    @classmethod
    def _check_order_no(cls, v: str) -> str:
        """订单号必须是 12 位数字（商家接口口径）。"""
        v = v.strip()
        if not v.isdigit() or len(v) != 12:
            raise ValueError("order_no 必须为 12 位数字")
        return v

    @property
    def status_cn(self) -> str:
        """订单状态中文口径。"""
        return ORDER_STATUS_CN.get(self.status, "未知")


# ---------------------------------------------------------------------------
# 物流（商家 2 号接口）
# ---------------------------------------------------------------------------


class LogisticsTrace(BaseModel):
    """物流轨迹节点。"""

    time: str = Field(description="时间")
    desc: str = Field(description="轨迹描述")
    status: LogisticsStatus | None = Field(default=None, description="节点状态")


class LogisticsQueryResponse(BaseModel):
    """物流查询响应（对齐商家 2.5 节）。"""

    company: str = Field(default="", description="快递公司中文名")
    company_code: CompanyCode | None = Field(default=None, description="快递公司编码")
    tracking_no: str = Field(default="", description="运单号")
    status: LogisticsStatus = Field(description="物流状态")
    latest: dict[str, Any] = Field(default_factory=dict, description="最新轨迹")
    estimated_arrival: str | None = Field(default=None, description="预计送达")
    traces: list[LogisticsTrace] = Field(default_factory=list, description="轨迹列表")
    signed: bool = Field(default=False, description="是否已签收")
    signed_at: str | None = Field(default=None, description="签收时间")

    @property
    def status_cn(self) -> str:
        """物流状态中文口径。"""
        return LOGISTICS_STATUS_CN.get(self.status, "未知")

    @property
    def stalled_days(self) -> int | None:
        """轨迹停滞天数（用于「物流停滞」判断与上下文卡片）。"""
        if not self.latest or not self.latest.get("time"):
            return None
        raw = str(self.latest["time"])
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=datetime.now().year)
            return max(0, (datetime.now() - parsed).days)
        return None


# ---------------------------------------------------------------------------
# 库存（商家 3 号接口）
# ---------------------------------------------------------------------------


class InventoryItem(BaseModel):
    """库存条目。

    ⚠️ **本对象不得写入向量库**（BE-05 DoD）：库存是会变的数字，
    必须每次实时查询，进 RAG 会导致「过期库存被当作事实回答」。
    """

    sku_id: str = Field(description="SKU 编码")
    title: str = Field(default="", description="商品名")
    stock_status: StockStatus = Field(description="库存状态（英文枚举）")
    available_qty: int | None = Field(default=None, description="可售数量")
    presale_days: int | None = Field(default=None, description="预售天数 7/15/30")
    ship_within_hours: int | None = Field(default=None, description="N 小时内发货")
    updated_at: datetime | None = Field(default=None, description="数据更新时间")

    @property
    def status_cn(self) -> str:
        """中文口径（**对外唯一可用表述**）。"""
        return stock_status_cn(self.stock_status, self.presale_days)


class InventoryQueryResponse(BaseModel):
    """库存查询响应（对齐商家 3.5 节）。`sku_ids` 单次最多 50。"""

    items: list[InventoryItem] = Field(default_factory=list, description="库存条目")
    degraded: bool = Field(default=False, description="是否走了降级（50301）")

    @field_validator("items")
    @classmethod
    def _check_limit(cls, v: list[InventoryItem]) -> list[InventoryItem]:
        """单次最多 50 个 SKU（商家接口硬限制）。"""
        if len(v) > 50:
            raise ValueError("库存查询单次最多 50 个 SKU")
        return v


# ---------------------------------------------------------------------------
# 商品（商家 4 号接口）
# ---------------------------------------------------------------------------


class ProductItem(BaseModel):
    """商品详情条目（对齐商家 4.5 节）。

    `stock_status` 为**中文口径**（商家该接口返回中文），与库存接口的英文枚举不同，
    这是商家接口本身的设计，不要强行统一。
    """

    sku_id: str = Field(description="SKU 编码")
    title: str = Field(default="", description="商品名")
    category: str = Field(default="", description="类目")
    size: str | None = Field(default=None, description="尺寸")
    material: str | None = Field(default=None, description="材质")
    filling: str | None = Field(default=None, description="填充物")
    age_range: str | None = Field(default=None, description="适用年龄")
    care_instruction: str | None = Field(default=None, description="洗护方式")
    packaging: str | None = Field(default=None, description="包装形式")
    launch_date: str | None = Field(default=None, description="上市时间")
    is_ip_collab: bool = Field(default=False, description="是否 IP 联名")
    list_price: float | None = Field(default=None, description="吊牌价")
    current_price: float | None = Field(default=None, description="现价")
    stock_status: str | None = Field(default=None, description="库存状态（中文口径）")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class ProductQueryResponse(BaseModel):
    """商品查询响应。"""

    items: list[ProductItem] = Field(default_factory=list, description="商品条目")
    updated_since: str | None = Field(
        default=None, description="增量同步起点，回显便于确认"
    )


# ---------------------------------------------------------------------------
# 工具调用统一包装
# ---------------------------------------------------------------------------


class ToolCallResult(BaseModel):
    """工具调用结果包装（编排层用）。

    Attributes:
        tool: 工具名（`order_query` 等）。
        ok: 是否成功。
        data: 成功时的业务数据。
        error_code: 商家错误码（失败时）。
        user_message: 用户侧话术（失败时的降级文案）。
        degraded: 命中的降级标识。
        latency_ms: 耗时。
        attempts: 实际请求次数（含重试）。
    """

    tool: str = Field(description="工具名")
    ok: bool = Field(default=False, description="是否成功")
    data: dict[str, Any] | None = Field(default=None, description="业务数据")
    error_code: int | None = Field(default=None, description="商家错误码")
    user_message: str | None = Field(default=None, description="用户侧话术")
    degraded: list[str] = Field(default_factory=list, description="降级标识")
    latency_ms: int = Field(default=0, ge=0, description="耗时 ms")
    attempts: int = Field(default=1, ge=1, description="请求次数")


__all__ = [
    "COMPANY_CN",
    "InventoryItem",
    "InventoryQueryResponse",
    "LOGISTICS_STATUS_CN",
    "LogisticsQueryResponse",
    "LogisticsTrace",
    "ORDER_STATUS_CN",
    "OrderAmount",
    "OrderItem",
    "OrderLogisticsBrief",
    "OrderQueryResponse",
    "ProductItem",
    "ProductQueryResponse",
    "ReceiverInfo",
    "STOCK_STATUS_CN",
    "ToolCallResult",
    "stock_status_cn",
]
