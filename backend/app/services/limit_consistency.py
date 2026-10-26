"""BE-18 DoD①「6 个接口的限流配置与商家说明书完全一致（自动化比对配置文件与文档）」。

任务书 3.9 节 BE-18 DoD① 的原文是**自动化比对**，所以不能靠人眼看文档填配置。
本模块从商家《接口对接说明书.md》的「0.6 限流阈值」表里解析出六个接口的
生产 QPS，与 `Settings.qps_*` 逐项比对，任何不一致都算失败。

## 为什么解析 Markdown 表而不是另存一份 JSON 配置

* 商家文档是**唯一权威源**（任务书 2.3.6 节「接口契约以商家文档为准」），
  另存一份配置就多一个会漂移的副本；
* 文档更新时，这个脚本会在 CI 里立刻红，逼着我们改代码而不是让两边静静不一致；
* 解析逻辑本身很简单（一张固定格式的表），引入模板引擎反而更脆。

## 解析口径（改文档格式时必须同步改这里）

表中行形如 `| 订单查询 | 20 | 200,000 | 允许 2 倍突发 10 秒 |`，
本模块只取前两列，并把中文接口名映射到配置键：

| 中文接口名 | 配置键 | 生产 QPS |
| --- | --- | --- |
| 订单查询 | `qps_order_query` | 20 |
| 物流轨迹查询 | `qps_logistics_track` | 20 |
| 库存查询 | `qps_inventory_query` | 50 |
| 商品信息读取 | `qps_product_detail` | 30 |
| 工单创建 | `qps_ticket_create` | 5 |
| 平台客服消息收发 | `qps_message_send` | 30 |

另外还校验两条**派生约束**：

1. 测试环境 QPS = 生产 / 5（商家 0.6 节「测试环境 QPS 上限为生产环境的 1/5」）；
2. `ticket_qps`（BE-14 用的单独配置项）与 `qps_ticket_create` 必须一致，
   否则会出现「限流器放行、工单创建被自己另一套配置挡住」的诡异现象。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import REPO_ROOT, get_settings

#: 商家《接口对接说明书.md》路径。
#: 解析失败**必须让整条链路失败**，因为 BE-18 DoD① 的验收方式就是「比对」；
#: 文档找不到时静默跳过，等于把这条 DoD 悄悄关掉。所以这里也写成必需项：
#: 文档缺失时 `parse_merchant_limits` 抛 `FileNotFoundError`。
MERCHANT_DOC = REPO_ROOT / "商家材料包" / "docs" / "接口对接说明书.md"

#: 「0.6 限流阈值」表的行正则。四列：接口名 / QPS / 日上限 / 突发额度。
#: 注意日上限列允许带千分位逗号（`200,000`），且行**可以不以 `|` 收尾**
#: （Markdown 允许省略尾竖线），所以 burst 段用 `[^|]*?` + `\|?` 兜底。
_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<name>[^|]+?)\s*\|"
    r"\s*(?P<qps>\d[\d,]*)\s*\|"
    r"\s*(?P<daily>\d[\d,]*)\s*\|"
    r"(?P<burst>[^|]*?)\s*\|?\s*$"
)

#: 表头行与分隔行的特征（这两行不是数据）
_HEADER_NAMES: frozenset[str] = frozenset({"接口"})

#: 中文接口名 → Settings 字段名。**新增接口时两处都要加**
ENDPOINT_FIELD_MAP: dict[str, str] = {
    "订单查询": "qps_order_query",
    "物流轨迹查询": "qps_logistics_track",
    "库存查询": "qps_inventory_query",
    "商品信息读取": "qps_product_detail",
    "工单创建": "qps_ticket_create",
    "平台客服消息收发": "qps_message_send",
}

#: 商家文档承诺的「测试环境 = 生产 1/5」（0.6 节原文）
TEST_ENV_DIVISOR = 5


@dataclass
class EndpointLimit:
    """一个接口的限流口径（文档侧 + 配置侧）。"""

    name: str
    field: str
    doc_qps: int
    config_qps: int
    daily_cap: int = 0
    burst: str = ""

    @property
    def matches(self) -> bool:
        """配置与文档是否一致。"""
        return self.doc_qps == self.config_qps


@dataclass
class ConsistencyReport:
    """比对结果。

    Attributes:
        endpoints: 逐接口比对。
        extra_checks: 派生约束的检查（测试环境折算、ticket_qps 一致性等）。
        missing: 文档里没解析到的接口名（文档格式变了会体现为这里非空）。
    """

    endpoints: list[EndpointLimit] = field(default_factory=list)
    extra_checks: list[tuple[str, bool, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全部一致才为 True。"""
        return (
            all(e.matches for e in self.endpoints)
            and all(ok for _, ok, _ in self.extra_checks)
            and not self.missing
        )

    @property
    def mismatches(self) -> list[str]:
        """不一致项的可读描述（告警与 CI 输出用）。"""
        out: list[str] = []
        for e in self.endpoints:
            if not e.matches:
                out.append(
                    f"{e.name}（{e.field}）：文档 {e.doc_qps} QPS ≠ 配置 {e.config_qps} QPS"
                )
        for name, ok, detail in self.extra_checks:
            if not ok:
                out.append(f"{name}：{detail}")
        for name in self.missing:
            out.append(f"{name}：商家文档中未解析到（文档格式可能已变更）")
        return out


def _to_int(raw: str) -> int:
    """把 `200,000` 这类带千分位的数字串转 int。"""
    return int(raw.replace(",", ""))


def parse_merchant_limits(doc_path: Path | None = None) -> dict[str, int]:
    """从商家说明书解析「0.6 限流阈值」表。

    Args:
        doc_path: 文档路径，默认 `MERCHANT_DOC`。

    Returns:
        `{中文接口名: 生产 QPS}`。

    Raises:
        FileNotFoundError: 文档不存在。
    """
    path = doc_path or MERCHANT_DOC
    if not path.exists():
        raise FileNotFoundError(f"商家接口说明书不存在：{path}")

    limits: dict[str, int] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### 0.6"):
            in_section = True
            continue
        if in_section and line.startswith("### "):
            break  # 下一节开始
        if not in_section or not line.startswith("|"):
            continue
        match = _TABLE_ROW_RE.match(line)
        if match is None:
            continue
        name = match.group("name").strip()
        if name in _HEADER_NAMES or set(name) <= {"-", " ", ":"}:
            continue  # 表头行 / 分隔行
        limits[name] = _to_int(match.group("qps"))
    if not limits:
        raise ValueError(
            f"未能从 {path} 的「0.6 限流阈值」表解析出任何接口，"
            "文档格式可能已变更——BE-18 DoD① 的自动比对依赖这张表"
        )
    return limits


def check_consistency(settings: object | None = None) -> ConsistencyReport:
    """比对商家文档与本地配置（BE-18 DoD①）。

    Args:
        settings: 配置对象，默认 `get_settings()`。

    Returns:
        `ConsistencyReport`。
    """
    settings = settings or get_settings()
    doc_limits = parse_merchant_limits()
    report = ConsistencyReport()

    for name, field_name in ENDPOINT_FIELD_MAP.items():
        if name not in doc_limits:
            report.missing.append(name)
            continue
        report.endpoints.append(
            EndpointLimit(
                name=name,
                field=field_name,
                doc_qps=doc_limits[name],
                config_qps=int(getattr(settings, field_name)),
            )
        )

    # --- 派生约束 1：测试环境折算为生产 1/5 ---
    effective = settings.effective_qps
    bad: list[str] = []
    for name, field_name in ENDPOINT_FIELD_MAP.items():
        endpoint_key = {
            "qps_order_query": "order_query",
            "qps_logistics_track": "logistics_track",
            "qps_inventory_query": "inventory_query",
            "qps_product_detail": "product_detail",
            "qps_ticket_create": "ticket_create",
            "qps_message_send": "message_send",
        }[field_name]
        expected = max(1, doc_limits[name] // TEST_ENV_DIVISOR)
        actual = effective.get(endpoint_key)
        if actual != expected:
            bad.append(f"{endpoint_key}: 期望 {expected}（文档 {doc_limits[name]}/5），实际 {actual}")
    report.extra_checks.append(
        ("测试环境 QPS = 生产 1/5（商家 0.6 节）", not bad, "；".join(bad) or "全部一致")
    )

    # --- 派生约束 2：ticket_qps 与 qps_ticket_create 必须同源 ---
    report.extra_checks.append(
        (
            "ticket_qps 与 qps_ticket_create 一致",
            settings.ticket_qps == settings.qps_ticket_create,
            f"ticket_qps={settings.ticket_qps} vs qps_ticket_create={settings.qps_ticket_create}",
        )
    )

    # --- 派生约束 3：effective_qps 的键集合与六个接口一一对应 ---
    # `effective_qps` 用的是 `order_query` 这类短键，`ENDPOINT_FIELD_MAP` 用的是
    # `qps_order_query` 这类配置字段名。两套命名之间必须**双射**：少一个键
    # 意味着某个接口没有限流闸（`PerEndpointRateLimiter` 会回落到默认 10 QPS，
    # 而那可能高于商家上限）；多一个键说明配置里躺着一个没人用的死项。
    expected_keys = {
        "订单查询": "order_query",
        "物流轨迹查询": "logistics_track",
        "库存查询": "inventory_query",
        "商品信息读取": "product_detail",
        "工单创建": "ticket_create",
        "平台客服消息收发": "message_send",
    }
    actual_keys = set(effective)
    missing_keys = sorted(set(expected_keys.values()) - actual_keys)
    extra_keys = sorted(actual_keys - set(expected_keys.values()))
    report.extra_checks.append(
        (
            "effective_qps 键与六个接口一一对应",
            not missing_keys and not extra_keys,
            f"缺失键：{missing_keys}；多余键：{extra_keys}",
        )
    )

    return report


def assert_consistency() -> ConsistencyReport:
    """校验并在不一致时抛 `AssertionError`（CI / 自检脚本用）。

    Returns:
        一致的 `ConsistencyReport`。

    Raises:
        AssertionError: 任一不一致。
    """
    report = check_consistency()
    if not report.ok:
        raise AssertionError(
            "限流配置与商家说明书不一致：\n  - " + "\n  - ".join(report.mismatches)
        )
    return report


__all__ = [
    "ENDPOINT_FIELD_MAP",
    "MERCHANT_DOC",
    "TEST_ENV_DIVISOR",
    "ConsistencyReport",
    "EndpointLimit",
    "assert_consistency",
    "check_consistency",
    "parse_merchant_limits",
]
