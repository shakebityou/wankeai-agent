"""工具注册表：`ToolName` → 工具函数（orchestrator 的唯一入口）。

`app/services/orchestrator.py:407-418` 惰性 import 本模块的
`get_tool_registry()`，因此**本包一旦存在就会被自动拾取**，无需改编排层。

## 返回对象为什么必须同时有 `data` 和 `text`

`orchestrator.py:670-676` 用 `getattr(payload, "data")` 与
`getattr(payload, "text")`：`data` 给结构化展示，`text` 给拼 prompt。
任一缺失都会被静默降级，所以 `ToolResult` 两个字段都填满。

## 为什么 `faq_shipping_sla` 走 RAG 而不是本注册表

它是**知识问答**（发货时效政策），答案在知识库里，不该打商家接口。
编排层已用 `ActionType.RAG_ANSWER` 分流，这里只登记会打到商家接口的工具；
`ticket_create` 留待 BE-14（任务书明确本期后置）。
"""

from __future__ import annotations

from typing import Any

from app.core.enums import ToolName
from app.core.logging import get_logger
from app.services.tools.base import ToolResult
from app.services.tools.identity import BuyerContext, resolve_buyer
from app.services.tools.inventory import query_inventory
from app.services.tools.logistics import query_logistics
from app.services.tools.order import query_order
from app.services.tools.product import query_product

logger = get_logger(__name__)


class ToolRegistry:
    """按 `ToolName` 分发到具体工具函数。

    Attributes:
        _handlers: 工具名 → 无参可调用（返回 `ToolResult`）。
        _port: 读端口（测试注入）。
    """

    def __init__(self, *, port: Any = None) -> None:
        self._port = port
        self._handlers: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, name: str, handler: Any) -> None:
        """登记一个工具处理器（测试/扩展用）。"""
        self._handlers[name] = handler

    def known_tools(self) -> list[str]:
        """已登记的工具名列表。"""
        return sorted(self._handlers)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    def invoke(
        self,
        tool: ToolName | str,
        *,
        session_id: str = "",
        buyer_id: str = "",
        order_no: str = "",
        sku_ids: Any = None,
        tracking_no: str = "",
        company: str = "",
        channel: str = "",
        channel_buyer_id: str = "",
        session_buyer_id: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """调用一个工具。

        Args:
            tool: 工具名。
            session_id: 会话 ID（频次计数与身份绑定）。
            buyer_id: 买家标识（兜底，`channel_buyer_id` 优先）。
            order_no: 订单号 / 物流订单号。
            sku_ids: 库存查询的 SKU 列表。
            tracking_no: 运单号。
            company: 快递公司编码。
            channel: 渠道。
            channel_buyer_id: 渠道透传买家（BE-08 绑定方式①）。
            session_buyer_id: 会话缓存买家（BE-08 绑定方式②）。

        Returns:
            `ToolResult`；工具未登记时返回带 `TOOL_UNKNOWN` 的失败结果
            （**不抛异常**：编排层的 `except Exception` 会把它变成一个没有
            话术的空降级，用户看到的就是兜底文案）。
        """
        name = tool.value if hasattr(tool, "value") else str(tool)
        ctx = resolve_buyer(
            channel_buyer_id=channel_buyer_id or buyer_id,
            session_buyer_id=session_buyer_id,
            session_id=session_id,
            channel=channel,
        )
        port = self._port
        try:
            if name == ToolName.ORDER_QUERY.value:
                return query_order(
                    order_no,
                    ctx=ctx,
                    port=port,
                    **kwargs,
                )
            if name == ToolName.LOGISTICS_QUERY.value:
                return query_logistics(
                    tracking_no=tracking_no,
                    order_no=order_no,
                    company=company,
                    ctx=ctx,
                    port=port,
                    **kwargs,
                )
            if name == ToolName.INVENTORY_QUERY.value:
                return query_inventory(sku_ids or [], port=port, **kwargs)
            if name == ToolName.PRODUCT_QUERY.value:
                return query_product(port=port, **kwargs)
            if name in self._handlers:
                return self._handlers[name](
                    session_id=session_id, buyer_id=buyer_id, order_no=order_no
                )
        except Exception as exc:  # noqa: BLE001 - 工具异常不得炸掉编排
            logger.exception(
                "工具调用异常", extra={"extra": {"tool": name, "error": str(exc)[:120]}}
            )
            return ToolResult(
                tool=name,
                ok=False,
                error_code=50001,
                user_message="系统查询中，请稍候",
                degraded=[f"{name.upper()}_FAILED", "TOOL_EXCEPTION"],
            )
        return ToolResult(
            tool=name,
            ok=False,
            error_code=None,
            user_message=None,
            degraded=["TOOL_UNKNOWN"],
        )


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """取全局工具注册表单例。"""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def set_tool_registry(registry: ToolRegistry | None) -> None:
    """替换全局注册表（**测试专用**）。"""
    global _registry  # noqa: PLW0603
    _registry = registry


__all__ = [
    "BuyerContext",
    "ToolRegistry",
    "get_tool_registry",
    "set_tool_registry",
]
