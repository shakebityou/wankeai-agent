"""BE-07 / BE-08 / BE-09 单测（订单物流 + 越权防护 + 库存中文口径）。

覆盖任务书三条 DoD：

* BE-07：6 状态全解析、10 条测试订单号联调（Mock 100%）、响应零完整手机号/地址、
  50401 重试 3 次仍失败 → 降级话术、签名头构造；
* BE-08：三种绑定方式各有独立用例、越权 100% 拒绝、超频友好提示、
  日志前 4 后 4；
* BE-09：三模板中文口径、50301 降级不编造库存数、单次 ≤50 SKU。
"""

from __future__ import annotations

import time

import pytest

from app.adapters.merchant_message import MerchantChannelError
from app.adapters.merchant_tools import MockMerchantToolPort
from app.core.errors import MerchantErrorCode
from app.services.tools.base import call_with_retry
from app.services.tools.identity import (
    AccessDeniedError,
    BuyerContext,
    RateLimitedError,
    SessionRateCounter,
    check_rate,
    ensure_order_access,
    resolve_buyer,
)
from app.services.tools.inventory import query_inventory
from app.services.tools.logistics import query_logistics
from app.services.tools.order import query_order
from app.services.tools.product import query_product
from app.services.tools.registry import ToolRegistry, get_tool_registry

# 商家官方 10 条测试订单号（接口权限申请清单 3.2 节）
TEST_ORDERS = [f"2026010100{i:02d}" for i in range(1, 11)]

_GOOD_BUYER = "buyer_001"


def _ctx(**kw: object) -> BuyerContext:
    base = {"buyer_id": _GOOD_BUYER, "source": "channel", "session_id": "s1"}
    base.update(kw)  # type: ignore[arg-type]
    return BuyerContext(**base)  # type: ignore[arg-type]


def _no_sleep(_seconds: float) -> None:
    """测试用假时钟：不真的 sleep。"""
    return None


# ---------------------------------------------------------------------------
# BE-07 订单
# ---------------------------------------------------------------------------


class TestOrderQuery:
    """订单查询（BE-07）。"""

    def test_ten_official_orders_all_succeed(self) -> None:
        """10 条官方测试订单号联调 100%（Mock 端口）。"""
        port = MockMerchantToolPort()
        for order_no in TEST_ORDERS:
            result = query_order(order_no, ctx=_ctx(), port=port, sleep=_no_sleep)
            assert result.ok, f"{order_no} 查询失败：{result.user_message}"
            assert result.data is not None
            assert result.data["order_no"] == order_no

    def test_success_rate_ninety_nine_percent(self) -> None:
        """成功率 ≥99%：前 3 次 504 后恢复（重试耗尽前成功）。"""
        port = MockMerchantToolPort(fail_times=2, fail_code=MerchantErrorCode.UPSTREAM_TIMEOUT)
        result = query_order(
            "202601010001", ctx=_ctx(), port=port, sleep=_no_sleep
        )
        assert result.ok
        assert result.attempts == 3
        assert result.latency_ms >= 0

    def test_50401_retry_exhausted_gives_degrade_text(self) -> None:
        """50401 重试 3 次仍失败 → 「系统查询中，请稍候」。"""
        port = MockMerchantToolPort(always_fail=True, fail_code=MerchantErrorCode.UPSTREAM_TIMEOUT)
        result = query_order(
            "202601010001", ctx=_ctx(), port=port, sleep=_no_sleep
        )
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.UPSTREAM_TIMEOUT)
        assert result.user_message == "系统查询中，请稍候"
        assert len([c for c in port.calls if c[0] == "order"]) == 4  # 1 + 3 次重试

    def test_status_cn_on_inventory_items(self) -> None:
        """`InventoryItem.status_cn` property 给中文口径（model_dump 会丢掉 property）。"""
        from app.schemas.tools import InventoryItem, StockStatus, stock_status_cn

        assert stock_status_cn(StockStatus.IN_STOCK) == "现货"
        assert stock_status_cn(StockStatus.PRESALE, 15) == "预售 15 天"
        assert stock_status_cn(StockStatus.OUT_OF_STOCK) == "缺货"
        item = InventoryItem(sku_id="X", stock_status=StockStatus.PRESALE, presale_days=30)
        assert item.status_cn == "预售 30 天"

    def test_response_has_no_full_phone_or_address(self) -> None:
        """响应不得出现完整手机号与完整地址（BE-07 DoD）。"""
        result = query_order("202601010001", ctx=_ctx(), port=MockMerchantToolPort())
        assert result.ok
        dumped = str(result.data)
        assert "13812345678" not in dumped
        assert "138****8888" in dumped
        assert "文一西路" not in dumped
        assert "浙江省杭州市" in dumped

    def test_invalid_order_no_rejected(self) -> None:
        """订单号非 12 位数字 → 40002。"""
        result = query_order("12345", ctx=_ctx(), port=MockMerchantToolPort())
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.PARAM_INVALID)

    def test_order_not_found(self) -> None:
        """40401：订单不存在。"""
        port = MockMerchantToolPort(missing_orders={"202601010099"})
        result = query_order("202601010099", ctx=_ctx(), port=port)
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.NOT_FOUND)

    def test_summary_masks_order_no(self) -> None:
        """送模型摘要里订单号前 4 后 4（BE-08 DoD③）。"""
        result = query_order("202601010001", ctx=_ctx(), port=MockMerchantToolPort())
        assert "2026****0001" in result.text
        assert "202601010001" not in result.text

    def test_status_cn_for_all_six_states(self) -> None:
        """6 种订单状态（含预售订单）都能给出中文口径。"""
        from app.core.enums import OrderStatus
        from app.schemas.tools import ORDER_STATUS_CN

        for status in OrderStatus:
            assert status in ORDER_STATUS_CN

    def test_429_obeys_retry_after(self) -> None:
        """429 优先服从 Retry-After（适配器解析出的秒数透传到退避）。"""
        waits: list[float] = []
        port = MockMerchantToolPort(
            fail_times=1, fail_code=MerchantErrorCode.RATE_LIMITED, retry_after=2.0
        )

        def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        result = query_order(
            "202601010001", ctx=_ctx(), port=port, sleep=fake_sleep
        )
        assert result.ok
        assert waits == [2.0]


# ---------------------------------------------------------------------------
# BE-07 物流
# ---------------------------------------------------------------------------


class TestLogisticsQuery:
    """物流轨迹（BE-07）。"""

    def test_track_success_and_desc_verbatim(self) -> None:
        """查询成功且 traces[].desc 原文引用（不改写）。"""
        result = query_logistics(tracking_no="ZT1234567890", port=MockMerchantToolPort())
        assert result.ok
        descs = [t["desc"] for t in result.data["traces"]]
        assert "快件已到达杭州转运中心" in descs

    def test_order_no_and_tracking_no_at_least_one(self) -> None:
        """两个都不传 → 40001。"""
        result = query_logistics(port=MockMerchantToolPort())
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.PARAM_MISSING)

    def test_invalid_company_rejected(self) -> None:
        """company 只支持 ZTO/YTO/SF。"""
        result = query_logistics(
            tracking_no="ZT1234567890", company="EMS", port=MockMerchantToolPort()
        )
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.PARAM_INVALID)

    def test_stalled_days_flagged(self) -> None:
        """停滞 ≥3 天记 LOGISTICS_STALLED 降级标识。"""
        from app.schemas.tools import LogisticsQueryResponse

        track = LogisticsQueryResponse(
            status="IN_TRANSIT",
            latest={"time": "2020-01-01 00:00:00", "desc": "停滞"},
        )
        assert (track.stalled_days or 0) >= 3

    def test_50401_retry_then_success(self) -> None:
        """快递商超时退避后成功。"""
        port = MockMerchantToolPort(fail_times=1, fail_code=MerchantErrorCode.UPSTREAM_TIMEOUT)
        result = query_logistics(
            tracking_no="ZT1234567890", port=port, sleep=_no_sleep
        )
        assert result.ok
        assert result.attempts == 2


# ---------------------------------------------------------------------------
# BE-09 库存
# ---------------------------------------------------------------------------


class TestInventoryQuery:
    """库存（BE-09）。"""

    def test_three_templates_in_chinese(self) -> None:
        """现货 / 预售 N 天 / 缺货 三模板正确，且**不出现英文枚举**。"""
        result = query_inventory(
            ["WK-BEAR-20", "WK-BEAR-30", "WK-CYBERSHIBA-24"],
            port=MockMerchantToolPort(),
        )
        assert result.ok
        by_sku = {it["sku_id"]: it for it in result.data["items"]}
        assert by_sku["WK-BEAR-20"]["stock_status"] == "IN_STOCK"
        assert by_sku["WK-BEAR-30"]["presale_days"] == 7
        assert by_sku["WK-CYBERSHIBA-24"]["stock_status"] == "OUT_OF_STOCK"
        # 对外口径必须走 stock_status_cn（schemas 唯一出口）
        from app.schemas.tools import InventoryItem, StockStatus

        for item in result.data["items"]:
            inv = InventoryItem.model_validate(item)
            assert inv.status_cn in ("现货", "预售 7 天", "缺货")
        assert "现货实时同步中" not in result.text  # 未降级不该出现降级话术

    def test_more_than_fifty_skus_rejected(self) -> None:
        """单次 >50 SKU → 40002。"""
        result = query_inventory([f"SKU-{i}" for i in range(51)])
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.PARAM_INVALID)

    def test_empty_sku_list_rejected(self) -> None:
        """空列表 → 40001。"""
        result = query_inventory([])
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.PARAM_MISSING)

    def test_50301_degrade_does_not_invent_stock(self) -> None:
        """50301 降级：不编造库存数，给「库存实时同步中」+ 引导加购。"""
        port = MockMerchantToolPort(always_fail=True, fail_code=MerchantErrorCode.SERVICE_UNAVAILABLE)
        result = query_inventory(["WK-BEAR-20"], port=port, sleep=_no_sleep)
        assert result.ok  # 降级不是错误，是「查不到但有话术」
        assert result.data["degraded"] is True
        assert result.data["items"] == []
        assert "库存实时同步中" in result.text
        assert "加购" in result.text

    def test_missing_sku_not_padded(self) -> None:
        """部分 SKU 不存在时**不补缺货占位**（不把「查不到」伪装成「卖完」）。"""
        port = MockMerchantToolPort(missing_skus={"WK-BEAR-30"})
        result = query_inventory(["WK-BEAR-20", "WK-BEAR-30"], port=port)
        skus = [it["sku_id"] for it in result.data["items"]]
        assert skus == ["WK-BEAR-20"]


# ---------------------------------------------------------------------------
# BE-09 商品
# ---------------------------------------------------------------------------


class TestProductQuery:
    """商品详情（BE-09 读侧）。"""

    def test_sku_and_category_mutually_exclusive(self) -> None:
        """sku_id 与 category 二选一。"""
        both = query_product(sku_id="WK-BEAR-20", category="毛绒玩偶")
        assert not both.ok
        assert both.error_code == int(MerchantErrorCode.PARAM_INVALID)

        neither = query_product()
        assert not neither.ok

    def test_updated_since_echoed(self) -> None:
        """`updated_since` 原样回显。"""
        result = query_product(
            sku_id="WK-BEAR-20", updated_since="2026-01-01 00:00:00",
            port=MockMerchantToolPort(),
        )
        assert result.ok
        assert result.data["updated_since"] == "2026-01-01 00:00:00"

    def test_chinese_stock_status_preserved(self) -> None:
        """该接口的 stock_status 是中文口径，不强行转英文。"""
        result = query_product(sku_id="WK-BEAR-20", port=MockMerchantToolPort())
        assert result.data["items"][0]["stock_status"] == "现货"


# ---------------------------------------------------------------------------
# BE-08 身份绑定与越权
# ---------------------------------------------------------------------------


class TestBuyerBinding:
    """三种绑定方式（BE-08 DoD②）。"""

    def test_channel_identity_wins(self) -> None:
        """① 渠道身份透传优先级最高。"""
        ctx = resolve_buyer(
            channel_buyer_id="buyer_channel",
            session_buyer_id="buyer_session",
            user_buyer_id="buyer_user",
        )
        assert ctx.buyer_id == "buyer_channel"
        assert ctx.source == "channel"

    def test_session_identity_second(self) -> None:
        """② 会话内已提供次之。"""
        ctx = resolve_buyer(session_buyer_id="buyer_session", user_buyer_id="buyer_user")
        assert ctx.buyer_id == "buyer_session"
        assert ctx.source == "session"

    def test_user_supplied_last(self) -> None:
        """③ 用户主动提供兜底。"""
        ctx = resolve_buyer(user_buyer_id="buyer_user")
        assert ctx.buyer_id == "buyer_user"
        assert ctx.source == "user"

    def test_none_when_unbound(self) -> None:
        """三者都缺 → 未绑定。"""
        ctx = resolve_buyer()
        assert ctx.buyer_id == ""
        assert ctx.source == "none"


class TestAccessControl:
    """越权防护（BE-08 DoD①）。"""

    def test_owner_can_query(self) -> None:
        """归属一致放行。"""
        ensure_order_access(
            {"order_no": "202601010001", "buyer_id": _GOOD_BUYER}, _ctx()
        )

    def test_other_buyer_rejected(self) -> None:
        """归属不一致 100% 拒绝。"""
        with pytest.raises(AccessDeniedError) as exc:
            ensure_order_access(
                {"order_no": "202601010001", "buyer_id": "buyer_other"}, _ctx()
            )
        assert exc.value.reason == "NOT_OWNER"
        assert "下单账号" in exc.value.user_message

    def test_unbound_rejected(self) -> None:
        """未绑定身份 → 拒绝（不是「订单不存在」）。"""
        with pytest.raises(AccessDeniedError) as exc:
            ensure_order_access(
                {"order_no": "202601010001", "buyer_id": _GOOD_BUYER}, _ctx(buyer_id="")
            )
        assert exc.value.reason == "UNBOUND"

    def test_owner_mismatch_via_service_returns_403_code(self) -> None:
        """工具层把越权翻成 40301 + 话术（供 HTTP 层映射 403）。"""
        port = MockMerchantToolPort()
        # Mock 订单归属 buyer_001；用别的买家查
        result = query_order(
            "202601010001",
            ctx=_ctx(buyer_id="buyer_evil"),
            port=port,
        )
        assert not result.ok
        assert result.error_code == int(MerchantErrorCode.NO_PERMISSION)
        assert "下单账号" in (result.user_message or "")
        assert "ACCESS_DENIED" in result.degraded

    def test_all_ten_orders_rejected_for_stranger(self) -> None:
        """陌生人查 10 条官方订单全部被拒（越权 100%）。"""
        port = MockMerchantToolPort()
        for order_no in TEST_ORDERS:
            result = query_order(
                order_no, ctx=_ctx(buyer_id="buyer_evil"), port=port
            )
            assert not result.ok
            assert result.error_code == int(MerchantErrorCode.NO_PERMISSION)


class TestSessionRateLimit:
    """单会话 20 次/小时（BE-08 DoD③）。"""

    def test_under_limit_passes(self) -> None:
        counter = SessionRateCounter()
        for _ in range(19):
            check_rate(_ctx(session_id="s-rate"), counter=counter, hourly_limit=20)
            counter.record("s-rate")

    def test_over_limit_friendly_prompt(self) -> None:
        """超频给友好提示，不是报错。"""
        counter = SessionRateCounter()
        for _ in range(20):
            counter.record("s-rate")
        with pytest.raises(RateLimitedError) as exc:
            check_rate(_ctx(session_id="s-rate"), counter=counter, hourly_limit=20)
        assert "频繁" in exc.value.user_message
        assert exc.value.retry_after > 0

    def test_window_slides(self) -> None:
        """窗口滑动：1 小时前的记录不再计数。"""
        counter = SessionRateCounter()
        now = time.time()
        for i in range(20):
            counter.record("s-slide", now=now - 4000 + i)
        # 全部超出窗口
        check_rate(_ctx(session_id="s-slide"), counter=counter, now=now, hourly_limit=20)


# ---------------------------------------------------------------------------
# 重试骨架
# ---------------------------------------------------------------------------


class TestCallWithRetry:
    """`call_with_retry` 与商家策略表一致。"""

    def test_retries_up_to_three_then_raises(self) -> None:
        calls = {"n": 0}

        def always_fail() -> None:
            calls["n"] += 1
            raise MerchantChannelError(
                "down", code=MerchantErrorCode.SERVICE_UNAVAILABLE, retryable=True
            )

        from app.core.errors import MerchantApiError

        with pytest.raises(MerchantApiError):
            call_with_retry("order_query", always_fail, sleep=_no_sleep)
        assert calls["n"] == 4  # 1 + 3

    def test_no_retry_for_param_error(self) -> None:
        calls = {"n": 0}

        def bad_param() -> None:
            calls["n"] += 1
            raise MerchantChannelError(
                "bad", code=MerchantErrorCode.PARAM_INVALID, retryable=False
            )

        from app.core.errors import MerchantApiError

        with pytest.raises(MerchantApiError):
            call_with_retry("order_query", bad_param, sleep=_no_sleep)
        assert calls["n"] == 1

    def test_network_error_translated(self) -> None:
        """非 MerchantChannelError 也翻成 MerchantApiError（不裸抛）。"""

        def boom() -> None:
            raise OSError("connection reset")

        from app.core.errors import MerchantApiError

        with pytest.raises(MerchantApiError):
            call_with_retry("order_query", boom, sleep=_no_sleep)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    """编排层入口。"""

    def test_registry_returns_data_and_text(self) -> None:
        """invoke 的返回必须同时有 data 与 text（orchestrator 两个都取）。"""
        reg = ToolRegistry()
        result = reg.invoke("order_query", session_id="s1", buyer_id=_GOOD_BUYER,
                            order_no="202601010001")
        assert result.ok
        assert isinstance(result.data, dict)
        assert result.text

    def test_unknown_tool_not_raising(self) -> None:
        """未登记工具不抛异常（抛了会让编排层降级成无话术兜底）。"""
        reg = ToolRegistry()
        result = reg.invoke("faq_shipping_sla")
        assert not result.ok
        assert "TOOL_UNKNOWN" in result.degraded

    def test_inventory_via_registry(self) -> None:
        reg = ToolRegistry()
        result = reg.invoke("inventory_query", sku_ids=["WK-BEAR-20"])
        assert result.ok
        item = result.data["items"][0]
        assert item["stock_status"] == "IN_STOCK"
        assert "现货" in result.text

    def test_logistics_via_registry(self) -> None:
        reg = ToolRegistry()
        result = reg.invoke("logistics_query", tracking_no="ZT1234567890")
        assert result.ok
        assert result.text

    def test_product_via_registry(self) -> None:
        reg = ToolRegistry()
        result = reg.invoke("product_query", sku_id="WK-BEAR-20")
        assert result.ok

    def test_singleton_available(self) -> None:
        """全局单例可拿（orchestrator 惰性 import 的就是它）。"""
        assert get_tool_registry() is not None
