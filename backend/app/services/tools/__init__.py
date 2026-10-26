"""业务查询工具层（BE-07 订单物流 / BE-08 越权防护 / BE-09 库存口径）。

本包是**唯一**调用商家 1~4 号读接口的地方（HC-3：会变的数字永远走接口）。

```
app/services/tools/
├── __init__.py      # 导出注册表
├── registry.py      # ToolRegistry：按 ToolName 分发，返回带 .text 的结果
├── identity.py      # BE-08 身份绑定 + 越权校验 + 单会话频次
├── order.py         # 订单查询
├── logistics.py     # 物流轨迹
├── inventory.py     # 库存（中文口径）
└── product.py       # 商品详情
```

## 为什么 `registry.invoke` 的返回值必须同时有 `data` 和 `text`

`app/services/orchestrator.py:650-689` 的 `_do_tool_and_rag` 直接用
`getattr(payload, "data")` 与 `getattr(payload, "text")`：`data` 供结构化
展示（上下文卡片、前端渲染），`text` 供拼进送模型的 prompt。二者缺失
都会被静默降级，因此 `ToolResult` 两个字段都必填。
"""

from __future__ import annotations

from app.services.tools.registry import ToolRegistry, get_tool_registry

__all__ = ["ToolRegistry", "get_tool_registry"]
