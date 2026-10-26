"""L7 基础层 · 合规闸（任务书 2.2.4 / 3.9 `BE-17`）。

本包是**所有出站内容的唯一出口**。任何文本在发送给用户之前必须经过
`OutboundGate.enforce()`；任何对象在写入向量库之前必须经过
`app.adapters.vector_store.assert_no_forbidden_fields()`。

三条硬约束在本包的落点：

| 约束 | 落点 |
| --- | --- |
| HC-1 不生成外链 / 跳转卡片 | `outbound_gate.OutboundGate` 正则扫描 + 兜底替换 |
| HC-2 最小必要、日志零敏感 | `pii` 复用 `app.core.logging` 的 7 类脱敏 |
| HC-3 会变的数字不走 RAG | `app.adapters.vector_store.FORBIDDEN_FIELDS` |
"""

from __future__ import annotations

from app.compliance.outbound_gate import (
    FALLBACK_REPLY,
    ComplianceScanResult,
    OutboundGate,
    assert_no_outbound_link,
    enforce_outbound,
    get_outbound_gate,
    scan_outbound,
    set_outbound_gate,
)

__all__ = [
    "FALLBACK_REPLY",
    "ComplianceScanResult",
    "OutboundGate",
    "assert_no_outbound_link",
    "enforce_outbound",
    "get_outbound_gate",
    "scan_outbound",
    "set_outbound_gate",
]
