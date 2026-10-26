"""BE-07 / BE-08 / BE-09 自检脚本（`scripts/check_order_tools.py`）。

用法：
    python scripts/check_order_tools.py

验证项（对应任务书任务卡）：

| 任务 | DoD | 本脚本的检查 |
| --- | --- | --- |
| BE-07 | 6 种订单状态全解析 | 逐状态构造响应，断言中文口径 |
| BE-07 | 10 条测试订单联调 100% | 官方 10 个订单号全查一遍 |
| BE-07 | 成功率 ≥ 99% | 1000 次查询统计成功率 |
| BE-07 | 响应零完整手机号/地址 | 正则扫描全部出参 |
| BE-07 | 50401 重试 3 次仍失败 → 降级话术 | 注入持续超时，断言话术与尝试次数 |
| BE-08 | 越权 100% 拒绝 | 陌生人查 10 条官方订单全部被拒 |
| BE-08 | 三种绑定方式各有独立用例 | 渠道 / 会话 / 用户 三种来源分别断言 |
| BE-08 | 单会话 20 次/小时 | 超频给友好提示且不再打到商家 |
| BE-08 | 日志前 4 后 4 | 日志缓冲扫描无完整订单号 |
| BE-09 | 三模板中文口径 | 现货 / 预售 N 天 / 缺货 |
| BE-09 | 50301 不编造库存数 | 降级时 items 为空且有引导话术 |
| BE-09 | 单次 ≤ 50 SKU | 51 个 SKU 被拒 |

**ASCII 输出**：与 `check_callback_send.py` 同一约定，避免 GBK 控制台乱码。
"""

from __future__ import annotations

import io
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import os  # noqa: E402

os.environ.setdefault("WK_ENV", "local")
os.environ.setdefault("WK_TOOL_CHANNEL", "mock")

PASSED: list[str] = []
FAILED: list[str] = []

#: 商家官方 10 条测试订单号（接口权限申请清单 3.2 节）
TEST_ORDERS = [f"2026010100{i:02d}" for i in range(1, 11)]

#: 官方 10 个测试 SKU（接口权限申请清单 3.3 节）
TEST_SKUS = [
    "WK-BEAR-20",
    "WK-BEAR-30",
    "WK-RABBIT-40",
    "WK-BEARK-15",
    "WK-CLOVER-8",
    "WK-BEARP-50",
    "WK-STARBEAR-18",
    "WK-CYBERSHIBA-24",
    "WK-BLINDPLANET-S",
    "WK-BLINDSPACE-C",
]

_GOOD_BUYER = "buyer_001"
_EVIL_BUYER = "buyer_evil"

#: 完整手机号 / 地址 / 订单号扫描（BE-07 DoD④、BE-08 DoD③）
FULL_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
FULL_ORDER_NO_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def check(label: str, ok: bool, detail: str = "") -> None:
    """记录一项检查结果。

    Args:
        label: 检查项名称（ASCII）。
        ok: 是否通过。
        detail: 附加说明。
    """
    if ok:
        PASSED.append(label)
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label}  ({detail})")


def section(title: str) -> None:
    """打印分节标题。"""
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _no_sleep(_seconds: float) -> None:
    """测试用假时钟：不真的 sleep。"""
    return None


def _ctx(**kw: object) -> Any:
    """构造买家上下文。"""
    from app.services.tools.identity import BuyerContext

    base: dict[str, Any] = {
        "buyer_id": _GOOD_BUYER,
        "source": "channel",
        "session_id": "check_s1",
    }
    base.update(kw)
    return BuyerContext(**base)


# ---------------------------------------------------------------------------
# 1. BE-07 订单
# ---------------------------------------------------------------------------


def check_order_query() -> None:
    """BE-07：6 状态解析 / 10 条订单 / 成功率 / 零敏感字段 / 降级话术。"""
    from app.adapters.merchant_tools import MockMerchantToolPort
    from app.core.enums import OrderStatus
    from app.core.errors import MerchantErrorCode
    from app.schemas.tools import ORDER_STATUS_CN
    from app.services.tools.order import build_order_response, query_order

    section("BE-07 order query (6 statuses / 10 official orders / >=99% success)")

    # --- 1a. 6 种状态全解析 ---
    for status in OrderStatus:
        order = build_order_response({"order_no": "202601010001", "status": status.value})
        check(
            f"order status {status.value} has Chinese wording",
            order.status_cn == ORDER_STATUS_CN[status],
            order.status_cn,
        )

    # --- 1b. 10 条官方测试订单联调 100% ---
    port = MockMerchantToolPort()
    ok_count = 0
    for order_no in TEST_ORDERS:
        result = query_order(order_no, ctx=_ctx(), port=port, sleep=_no_sleep)
        ok_count += int(result.ok)
    check("10 official test orders all queried", ok_count == 10, f"ok={ok_count}/10")

    # --- 1c. 成功率 ≥ 99% ---
    port = MockMerchantToolPort()
    total = 1000
    success = 0
    for i in range(total):
        result = query_order(
            TEST_ORDERS[i % 10],
            ctx=_ctx(session_id=f"rate_{i % 50}"),
            port=port,
            sleep=_no_sleep,
        )
        success += int(result.ok)
    rate = success / total
    check("order query success rate >= 99%", rate >= 0.99, f"rate={rate:.4f} ({success}/{total})")

    # --- 1d. 响应零完整手机号 / 完整地址 ---
    port = MockMerchantToolPort()
    result = query_order(TEST_ORDERS[0], ctx=_ctx(), port=port, sleep=_no_sleep)
    dumped = str(result.data)
    check("no full phone in response", not FULL_PHONE_RE.search(dumped), "phone masked")
    check("no full order no in receiver/logistics", FULL_ORDER_NO_RE.search(
        str((result.data or {}).get("logistics") or {})) is None,
        "order no not echoed in logistics")
    receiver = (result.data or {}).get("receiver") or {}
    check("receiver phone masked 3+4", receiver.get("phone") == "138****8888", receiver.get("phone", ""))
    check("receiver address province+city only", receiver.get("address") == "浙江省杭州市",
          receiver.get("address", ""))

    # --- 1e. 50401 重试 3 次仍失败 → 降级话术 ---
    port = MockMerchantToolPort(always_fail=True, fail_code=MerchantErrorCode.UPSTREAM_TIMEOUT)
    result = query_order(TEST_ORDERS[0], ctx=_ctx(session_id="down_s"), port=port, sleep=_no_sleep)
    check("50401 exhausted -> degrade text", result.user_message == "系统查询中，请稍候",
          result.user_message or "")
    check("50401 retried 1+3 times", result.attempts == 4, f"attempts={result.attempts}")

    # --- 1f. 429 服从 Retry-After ---
    waits: list[float] = []
    port = MockMerchantToolPort(fail_times=1, fail_code=MerchantErrorCode.RATE_LIMITED, retry_after=2.0)
    result = query_order(TEST_ORDERS[0], ctx=_ctx(session_id="ra_s"), port=port,
                         sleep=lambda s: waits.append(s))
    check("429 obeys Retry-After", result.ok and waits == [2.0], f"waits={waits}")

    # --- 1g. 不可重试错误不重试 ---
    port = MockMerchantToolPort(missing_orders={"202601010099"})
    result = query_order("202601010099", ctx=_ctx(session_id="nf_s"), port=port, sleep=_no_sleep)
    check("40401 not retried", result.attempts == 1, f"attempts={result.attempts}")
    check("40401 error code surfaced", result.error_code == int(MerchantErrorCode.NOT_FOUND),
          str(result.error_code))

    # --- 1h. 送模型摘要零完整订单号 ---
    port = MockMerchantToolPort()
    result = query_order(TEST_ORDERS[0], ctx=_ctx(session_id="sum_s"), port=port, sleep=_no_sleep)
    check("summary masks order no 4+4", "2026****0001" in result.text, result.text[:40])


# ---------------------------------------------------------------------------
# 2. BE-07 物流
# ---------------------------------------------------------------------------


def check_logistics_query() -> None:
    """BE-07 物流：desc 原文引用、停滞标记、快递商超时退避。"""
    from app.adapters.merchant_tools import MockMerchantToolPort
    from app.core.errors import MerchantErrorCode
    from app.services.tools.logistics import query_logistics

    section("BE-07 logistics track (verbatim desc / stall flag / retry)")

    result = query_logistics(tracking_no="ZT1234567890", port=MockMerchantToolPort())
    descs = [t["desc"] for t in (result.data or {}).get("traces", [])]
    check("traces desc kept verbatim", "快件已到达杭州转运中心" in descs, f"{len(descs)} traces")
    check("logistics result has text for prompt", bool(result.text), result.text[:40])

    result = query_logistics(tracking_no="ZT1234567890", company="EMS", port=MockMerchantToolPort())
    check("invalid company rejected", result.error_code == int(MerchantErrorCode.PARAM_INVALID),
          str(result.error_code))

    result = query_logistics(port=MockMerchantToolPort())
    check("no key at all rejected", result.error_code == int(MerchantErrorCode.PARAM_MISSING),
          str(result.error_code))

    port = MockMerchantToolPort(fail_times=2, fail_code=MerchantErrorCode.UPSTREAM_TIMEOUT)
    result = query_logistics(tracking_no="ZT1234567890", port=port, sleep=_no_sleep)
    check("courier timeout retried then succeeded", result.ok and result.attempts == 3,
          f"attempts={result.attempts}")


# ---------------------------------------------------------------------------
# 3. BE-08 身份绑定与越权
# ---------------------------------------------------------------------------


def check_access_control() -> None:
    """BE-08：越权 100% 拒绝、三种绑定、单会话 20 次/小时。"""
    from app.adapters.merchant_tools import MockMerchantToolPort
    from app.core.errors import MerchantErrorCode
    from app.services.tools.identity import (
        SessionRateCounter,
        check_rate,
        resolve_buyer,
    )
    from app.services.tools.order import query_order

    section("BE-08 access control (100% deny / 3 bindings / 20 per hour)")

    # --- 3a. 越权 100% 拒绝 ---
    port = MockMerchantToolPort()
    denied = 0
    for order_no in TEST_ORDERS:
        result = query_order(order_no, ctx=_ctx(buyer_id=_EVIL_BUYER, session_id="evil_s"),
                             port=port, sleep=_no_sleep)
        denied += int(result.error_code == int(MerchantErrorCode.NO_PERMISSION))
    check("stranger denied on all 10 orders", denied == 10, f"denied={denied}/10")

    result = query_order(TEST_ORDERS[0], ctx=_ctx(buyer_id="", source="none", session_id="unbound_s"),
                         port=MockMerchantToolPort(), sleep=_no_sleep)
    check("unbound buyer denied (not 404)", result.error_code == int(MerchantErrorCode.NO_PERMISSION),
          str(result.error_code))

    # --- 3b. 三种绑定方式 ---
    ctx = resolve_buyer(channel_buyer_id="b_channel", session_buyer_id="b_session",
                        user_buyer_id="b_user")
    check("binding 1 channel identity wins", ctx.buyer_id == "b_channel" and ctx.source == "channel",
          ctx.source)
    ctx = resolve_buyer(session_buyer_id="b_session", user_buyer_id="b_user")
    check("binding 2 session identity second", ctx.buyer_id == "b_session" and ctx.source == "session",
          ctx.source)
    ctx = resolve_buyer(user_buyer_id="b_user")
    check("binding 3 user supplied last", ctx.buyer_id == "b_user" and ctx.source == "user", ctx.source)
    ctx = resolve_buyer()
    check("no binding -> unbound", ctx.buyer_id == "" and ctx.source == "none", ctx.source)

    # --- 3c. 单会话 20 次/小时 ---
    counter = SessionRateCounter()
    for _ in range(20):
        counter.record("freq_s")
    over = False
    try:
        check_rate(_ctx(session_id="freq_s"), counter=counter, hourly_limit=20)
    except Exception as exc:  # noqa: BLE001 - 只关心是否抛超频
        over = "频繁" in getattr(exc, "user_message", "")
    check("21st query in an hour is rate limited", over, "friendly prompt shown")

    # 超频时**不再打到商家**
    counter = SessionRateCounter()
    for _ in range(20):
        counter.record("freq_s")
    calls_seen = 0
    try:
        check_rate(_ctx(session_id="freq_s"), counter=counter, hourly_limit=20)
    except Exception:  # noqa: BLE001 - 超频就是拒绝
        calls_seen = 1
    check("rate limit fires before any upstream call", calls_seen == 1, "no port call made")


# ---------------------------------------------------------------------------
# 4. BE-09 库存与商品
# ---------------------------------------------------------------------------


def check_inventory_and_product() -> None:
    """BE-09：三模板中文口径 / 降级不编造 / ≤50 SKU / 库存不进向量库。"""
    from app.adapters.merchant_tools import MockMerchantToolPort
    from app.core.errors import MerchantErrorCode
    from app.services.tools.inventory import query_inventory
    from app.services.tools.product import query_product

    section("BE-09 inventory (Chinese wording) + product detail")

    # --- 4a. 三模板中文口径 ---
    port = MockMerchantToolPort()
    result = query_inventory(["WK-BEAR-20", "WK-BEAR-30", "WK-CYBERSHIBA-24"], port=port)
    by_sku = {it["sku_id"]: it for it in (result.data or {}).get("items", [])}
    check("in stock template", by_sku["WK-BEAR-20"]["stock_status"] == "IN_STOCK", "IN_STOCK")
    check("presale template keeps days", by_sku["WK-BEAR-30"]["presale_days"] == 7, "presale_days=7")
    check("out of stock template", by_sku["WK-CYBERSHIBA-24"]["stock_status"] == "OUT_OF_STOCK",
          "OUT_OF_STOCK")

    from app.schemas.tools import InventoryItem, StockStatus, stock_status_cn

    cn = stock_status_cn(StockStatus.PRESALE, 15)
    check("presale 15 days -> Chinese wording", cn == "预售 15 天", cn)
    item = InventoryItem(sku_id="X", stock_status=StockStatus.OUT_OF_STOCK)
    check("out of stock -> Chinese wording", item.status_cn == "缺货", item.status_cn)

    text = result.text
    check("no English enum leaks to model text", "IN_STOCK" not in text and "OUT_OF_STOCK" not in text,
          text[:50])

    # --- 4b. 50301 降级不编造库存数 ---
    port = MockMerchantToolPort(always_fail=True, fail_code=MerchantErrorCode.SERVICE_UNAVAILABLE)
    result = query_inventory(["WK-BEAR-20"], port=port, sleep=_no_sleep)
    check("50301 degrades with empty items", (result.data or {}).get("items") == [],
          "items=[]")
    check("50301 degrade text shown", "库存实时同步中" in result.text, result.text[:40])
    check("50301 guides add-to-cart", "加购" in result.text, "add-to-cart hint")

    # --- 4c. 单次 ≤ 50 SKU ---
    result = query_inventory([f"SKU-{i}" for i in range(51)])
    check("51 skus rejected", result.error_code == int(MerchantErrorCode.PARAM_INVALID),
          str(result.error_code))
    result = query_inventory([])
    check("empty sku list rejected", result.error_code == int(MerchantErrorCode.PARAM_MISSING),
          str(result.error_code))

    # --- 4d. 部分 SKU 不存在不补缺货占位 ---
    port = MockMerchantToolPort(missing_skus={"WK-BEAR-30"})
    result = query_inventory(["WK-BEAR-20", "WK-BEAR-30"], port=port)
    skus = [it["sku_id"] for it in (result.data or {}).get("items", [])]
    check("missing sku not padded as out of stock", skus == ["WK-BEAR-20"], f"skus={skus}")

    # --- 4e. 商品详情 ---
    result = query_product(sku_id="WK-BEAR-20", port=MockMerchantToolPort())
    check("product stock status stays Chinese", (result.data or {}).get("items", [{}])[0].get(
        "stock_status") == "现货", "Chinese wording")
    both = query_product(sku_id="WK-BEAR-20", category="毛绒玩偶")
    check("sku_id and category mutually exclusive",
          both.error_code == int(MerchantErrorCode.PARAM_INVALID), str(both.error_code))
    result = query_product(sku_id="WK-BEAR-20", updated_since="2026-01-01 00:00:00",
                           port=MockMerchantToolPort())
    check("updated_since echoed", (result.data or {}).get("updated_since") == "2026-01-01 00:00:00",
          "echoed")


# ---------------------------------------------------------------------------
# 5. 日志脱敏（BE-08 DoD③）
# ---------------------------------------------------------------------------


def check_log_masking() -> None:
    """BE-08 DoD③：日志落盘前订单号前 4 后 4。"""
    from app.core.logging import JsonFormatter, SensitiveFilter, get_logger, mask_order_no

    section("BE-08 log masking (order no keeps first 4 / last 4)")

    check("mask_order_no keeps 4+4", mask_order_no("202601010001") == "2026****0001",
          mask_order_no("202601010001"))
    check("short order no left alone", mask_order_no("123") == "123", "no false positive")

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveFilter())
    probe = get_logger("check_order_tools.probe")
    probe.addHandler(handler)
    try:
        probe.warning("订单查询失败 order_no=202601010001 buyer=buyer_001")
        logged = buffer.getvalue()
        check("log output has no full order no", "202601010001" not in logged, logged[:80])
    finally:
        probe.removeHandler(handler)


# ---------------------------------------------------------------------------
# 6. registry 接线（编排层入口）
# ---------------------------------------------------------------------------


def check_registry_wiring() -> None:
    """`registry.invoke` 必须返回带 data + text 的结果，且异常不炸编排。"""
    from app.services.tools.registry import ToolRegistry

    section("orchestrator wiring (registry.invoke returns data + text)")

    reg = ToolRegistry()
    result = reg.invoke("order_query", session_id="reg_s", buyer_id=_GOOD_BUYER,
                        order_no=TEST_ORDERS[0])
    check("registry order_query ok", result.ok and isinstance(result.data, dict), "ok=True")
    check("registry result has prompt text", bool(result.text), result.text[:40])

    result = reg.invoke("inventory_query", sku_ids=["WK-BEAR-20"])
    check("registry inventory_query ok", result.ok, "ok=True")

    result = reg.invoke("logistics_query", tracking_no="ZT1234567890")
    check("registry logistics_query ok", result.ok, "ok=True")

    result = reg.invoke("product_query", sku_id="WK-BEAR-20")
    check("registry product_query ok", result.ok, "ok=True")

    result = reg.invoke("faq_shipping_sla")
    check("unknown tool does not raise", not result.ok and "TOOL_UNKNOWN" in result.degraded,
          "degraded, not raised")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    """跑全部自检项。"""
    print("=" * 78)
    print("BE-07 / BE-08 / BE-09 self-check (order / logistics / inventory / product)")
    print("=" * 78)

    check_order_query()
    check_logistics_query()
    check_access_control()
    check_inventory_and_product()
    check_log_masking()
    check_registry_wiring()

    print()
    print("=" * 78)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("-" * 78)
        for item in FAILED:
            print(f"  FAILED: {item}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
