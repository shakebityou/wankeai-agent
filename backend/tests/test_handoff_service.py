"""BE-14 / BE-15 · 转人工建单服务、坐席路由与离线兜底单元测试。

覆盖 DoD：
* ① 工单落库，坐席卡片七要素齐全；
* ② 幂等：同一 `session_id` 只建一张单（缓存命中 + DB 唯一约束兜底）；
* ③ 路由：reason / 渠道 / 意图 → G1~G6；
* ④ 在线/离线话术：**零链接、无「请稍等」「马上」**；
* ⑤ 商家同步失败/限流降级为本地队列，不阻断用户；
* ⑥ 工单列表与详情（含事件时间线）。

**全部用替身**：缓存用 `InMemoryCache`、商家工单用 `MockMerchantTicketPort`、
DB 用临时 SQLite——不连 Redis / 不连商家。
"""

from __future__ import annotations

from datetime import time as dt_time
from typing import Any

import pytest

from app.adapters.merchant_ticket import MockMerchantTicketPort
from app.core.enums import Channel, HandoffReason, Priority, TicketStatus
from app.core.ratelimit import RateLimitExceeded
from app.services.handoff.online import (
    FORBIDDEN_WAIT_WORDS,
    is_online,
    offline_user_message,
    online_user_message,
    parse_online_hours,
)
from app.services.handoff.routing import DEFAULT_GROUPS, AgentRouter, GroupInfo
from app.services.handoff.service import HandoffService, get_handoff_service

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「在线判定」钉死为在线，让用例与真实时钟解耦。

    分组在线时段是 `09:00–22:00`，而 `is_online` 读的是真实时间——晚上跑
    测试会被判离线，于是「同步商家 / `SYNCED` 事件 / `queue_position`」这些
    断言会假失败（实测 22:37 跑全量挂了 5 例）。这里只替换**服务层引用的
    那个名字**（`app.services.handoff.service.is_online`），
    `app.services.handoff.online.is_online` 本身不动，所以
    `TestOnlineHours` 里对真实函数的时段断言照常有效。
    需要离线场景的用例显式传 `force_offline=True`，不受本夹具影响。
    """
    from app.services.handoff import service as service_mod

    monkeypatch.setattr(service_mod, "is_online", lambda *a, **k: True)


@pytest.fixture
def db() -> Any:
    """建好全部业务表的临时 SQLite（每个用例一套干净库）。"""
    from app.db.engine import create_all, reset_engine

    reset_engine()  # 丢掉上一个用例残留的 Engine 单例
    create_all()
    yield


@pytest.fixture
def cache() -> Any:
    """内存缓存。"""
    from app.adapters.cache import InMemoryCache

    return InMemoryCache()


@pytest.fixture
def service(cache: Any, db: Any) -> HandoffService:
    """建单服务（内存缓存 + 临时库 + Mock 商家工单）。"""
    return HandoffService(cache=cache, ticket_client=MockMerchantTicketPort())


def _card(**overrides: Any) -> dict[str, Any]:
    """七要素齐全的上下文卡片。"""
    card: dict[str, Any] = {
        "summary": "用户反馈收到的玩偶有破损，要求换货",
        "verified": [{"type": "entity", "key": "order_no", "value": "123456789012"}],
        "tried_actions": [{"action": "order_query", "result": "已查到订单"}],
        "unmet_demand": "要求立即换货并承担运费",
        "suggested_action": "核实破损照片后走换货流程",
        "emotion": {"level": "不满", "intensity": "medium"},
        "turn_count": 4,
    }
    card.update(overrides)
    return card


# ---------------------------------------------------------------------------
# 服务时段解析（BE-15 基础）
# ---------------------------------------------------------------------------


class TestOnlineHours:
    """`09:00–22:00` 这类时段串的解析与在线判定。"""

    @pytest.mark.parametrize(
        "spec",
        [
            "09:00–22:00",
            "09:00-22:00",
            "09:00~22:00",
            "09:00至22:00",
            "09:00 到 22:00",
            "9:00–22:00",
            "09：00–22：00",
        ],
    )
    def test_parse_variants(self, spec: str) -> None:
        """全角连接号、全角冒号、中文「至/到」都要能解析。"""
        parsed = parse_online_hours(spec)
        assert parsed is not None, spec
        assert parsed[0] == dt_time(9, 0), spec
        assert parsed[1] == dt_time(22, 0), spec

    @pytest.mark.parametrize("spec", ["", "全天", "9-22", "abc", "25:00–26:00"])
    def test_parse_invalid(self, spec: str) -> None:
        """无法解析的时段返回 None（不抛异常）。"""
        assert parse_online_hours(spec) is None

    def test_is_online_inside(self) -> None:
        """时段内为在线。"""
        assert is_online("09:00–22:00", now=dt_time(12, 0)) is True

    def test_is_online_outside(self) -> None:
        """时段外为离线。"""
        assert is_online("09:00–22:00", now=dt_time(23, 0)) is False

    def test_is_online_cross_midnight(self) -> None:
        """跨零点时段（22:00–02:00）判定正确。"""
        assert is_online("22:00–02:00", now=dt_time(23, 30)) is True
        assert is_online("22:00–02:00", now=dt_time(12, 0)) is False

    def test_is_online_unparsable_is_offline(self) -> None:
        """解析失败按离线处理（宁可给兜底话术，也不承诺有人接）。"""
        assert is_online("全天", now=dt_time(12, 0)) is False


# ---------------------------------------------------------------------------
# 话术（HC-1 零链接 / 禁等待词）
# ---------------------------------------------------------------------------


class TestUserMessages:
    """在线/离线话术约束。"""

    @pytest.mark.parametrize(
        "reason",
        [
            HandoffReason.USER_REQUEST,
            HandoffReason.EMOTION,
            HandoffReason.UNRESOLVED,
            HandoffReason.HIGH_RISK,
            HandoffReason.SYSTEM_ERROR,
        ],
    )
    def test_offline_no_link_no_wait_words(self, reason: HandoffReason) -> None:
        """离线话术：5 种 reason 全覆盖，零链接、无等待承诺词。"""
        text = offline_user_message(reason, "TS123456789012")
        assert "http" not in text.lower()
        assert "点击" not in text
        assert "跳转" not in text
        assert "小程序" not in text
        for word in FORBIDDEN_WAIT_WORDS:
            assert word not in text, (reason, word)

    def test_online_no_link(self) -> None:
        """在线话术同样零链接。"""
        text = online_user_message("TS123456789012", 3)
        assert "http" not in text.lower()
        assert "TS123456789012" in text
        assert "3" in text
        for word in FORBIDDEN_WAIT_WORDS:
            assert word not in text


# ---------------------------------------------------------------------------
# 坐席路由
# ---------------------------------------------------------------------------


class TestRouting:
    """reason / 渠道 / 意图 → 坐席分组。"""

    def test_groups_loaded(self) -> None:
        """默认 6 个分组，字段完整。"""
        router = AgentRouter(DEFAULT_GROUPS)
        assert len(router.groups) == 6
        for gid in ("G1", "G2", "G3", "G4", "G5", "G6"):
            assert router.get(gid) is not None

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (HandoffReason.HIGH_RISK, "G5"),
            (HandoffReason.EMOTION, "G5"),
            (HandoffReason.SYSTEM_ERROR, "G5"),
            (HandoffReason.UNRESOLVED, "G3"),
        ],
    )
    def test_route_by_reason(self, reason: HandoffReason, expected: str) -> None:
        """投诉/情绪/异常进升级组，未解决进售后组。"""
        group = AgentRouter(DEFAULT_GROUPS).route(reason=reason)
        assert group is not None
        assert group.group_id == expected

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            ("商品咨询", "G1"),
            ("订单服务", "G2"),
            ("售后服务", "G3"),
            ("营销活动", "G4"),
            ("品牌信任", "G6"),
            ("兜底", "G1"),
        ],
    )
    def test_route_by_intent(self, category: str, expected: str) -> None:
        """显式请求人工时按意图大类分流。"""
        group = AgentRouter(DEFAULT_GROUPS).route(
            reason=HandoffReason.USER_REQUEST, intent_category=category
        )
        assert group is not None
        assert group.group_id == expected

    def test_route_channel_filter(self) -> None:
        """渠道不匹配时按兜底顺序换组（G2 不服务微信客服）。"""
        router = AgentRouter(DEFAULT_GROUPS)
        group = router.route(reason=HandoffReason.USER_REQUEST, channel="WECHAT",
                             intent_category="订单服务")
        assert group is not None
        assert group.group_id != "G2"
        assert "微信客服" in group.channels or not group.channels

    def test_group_serves_all_channels(self) -> None:
        """channels 为空表示全渠道。"""
        group = GroupInfo(group_id="G9", group_name="全渠道组", channels=[])
        assert group.serves("微信客服") is True

    def test_estimate_wait(self) -> None:
        """预估等待 = ceil(排队/坐席数) * 180 秒。"""
        router = AgentRouter(DEFAULT_GROUPS)
        g1 = router.get("G1")
        assert g1 is not None
        assert router.estimate_wait_seconds(g1, 1) == 180
        assert router.estimate_wait_seconds(g1, 13) == 360


# ---------------------------------------------------------------------------
# 建单
# ---------------------------------------------------------------------------


class TestCreateTicket:
    """建单主流程。"""

    def test_create_basic(self, service: HandoffService) -> None:
        """建单返回工单号、分组、排队位次，卡片七要素齐全。"""
        out = service.handoff(
            session_id="s-1",
            reason=HandoffReason.USER_REQUEST,
            buyer_id="b-1",
            channel=Channel.TAOBAO,
            context_card=_card(),
        )
        assert out.ticket_no.startswith("TS")
        assert len(out.ticket_no) == 14  # TS + 12 位
        assert out.group_id is not None
        assert out.context_complete is True
        detail = service.get_ticket(out.ticket_no)
        assert detail is not None
        card = detail["context_card"]
        for key in (
            "summary",
            "verified",
            "tried_actions",
            "unmet_demand",
            "suggested_action",
            "emotion",
            "turn_count",
        ):
            assert card.get(key), key

    def test_priority_from_reason(self, service: HandoffService) -> None:
        """HIGH_RISK 默认 URGENT，UNRESOLVED 默认 NORMAL。"""
        high = service.handoff(session_id="s-h", reason=HandoffReason.HIGH_RISK,
                               context_card=_card())
        assert high.priority is Priority.URGENT
        low = service.handoff(session_id="s-u", reason=HandoffReason.UNRESOLVED,
                              context_card=_card())
        assert low.priority is Priority.NORMAL

    def test_priority_override(self, service: HandoffService) -> None:
        """显式传入的优先级覆盖 reason 推导。"""
        out = service.handoff(
            session_id="s-o",
            reason=HandoffReason.UNRESOLVED,
            priority=Priority.HIGH,
            context_card=_card(),
        )
        assert out.priority is Priority.HIGH

    def test_str_card_items_normalized(self, service: HandoffService) -> None:
        """编排层传 `list[str]` 也能归一成契约要求的 `list[dict]`。"""
        out = service.handoff(
            session_id="s-str",
            reason=HandoffReason.USER_REQUEST,
            context_card={
                "summary": "问题",
                "verified": ["订单已核对"],
                "tried_actions": ["order_query"],
                "unmet_demand": "要换货",
                "suggested_action": "人工跟进",
                "emotion": "不满",
                "turn_count": 2,
            },
        )
        detail = service.get_ticket(out.ticket_no)
        card = detail["context_card"]
        assert isinstance(card["verified"][0], dict)
        assert isinstance(card["tried_actions"][0], dict)
        assert isinstance(card["emotion"], dict)

    def test_incomplete_card_filled(self, service: HandoffService) -> None:
        """卡片缺要素时自动补齐（不因前端漏字段而拒单）。"""
        out = service.handoff(
            session_id="s-min",
            reason=HandoffReason.UNRESOLVED,
            summary="用户问题未解决",
            context_card={"summary": "用户问题未解决"},
        )
        detail = service.get_ticket(out.ticket_no)
        card = detail["context_card"]
        assert card["verified"]
        assert card["tried_actions"]
        assert card["emotion"]
        assert card["unmet_demand"]
        assert card["suggested_action"]
        assert card["turn_count"] >= 1

    def test_events_recorded(self, service: HandoffService) -> None:
        """落库后至少有 CREATED 与 SYNCED 两条事件。"""
        out = service.handoff(session_id="s-ev", reason=HandoffReason.USER_REQUEST,
                              context_card=_card())
        detail = service.get_ticket(out.ticket_no)
        events = [e["event"] for e in detail["events"]]
        assert "CREATED" in events
        assert "SYNCED" in events

    def test_merchant_ticket_no_saved(self, service: HandoffService) -> None:
        """商家同步成功后写入商家工单号。"""
        out = service.handoff(session_id="s-mt", reason=HandoffReason.USER_REQUEST,
                              context_card=_card())
        assert out.merchant_ticket_no is not None
        assert out.merchant_ticket_no.startswith("MT")


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


class TestIdempotency:
    """同一会话只建一张单。"""

    def test_cache_hit(self, service: HandoffService) -> None:
        """第二次调用命中幂等缓存，返回同一工单号。"""
        first = service.handoff(session_id="s-idem", reason=HandoffReason.USER_REQUEST,
                                context_card=_card())
        second = service.handoff(session_id="s-idem", reason=HandoffReason.USER_REQUEST,
                                 context_card=_card())
        assert second.idempotent_hit is True
        assert second.ticket_no == first.ticket_no

    def test_db_unique_constraint(self, service: HandoffService, cache: Any) -> None:
        """缓存被清空后，DB 唯一约束仍能挡住重复建单。"""
        first = service.handoff(session_id="s-db", reason=HandoffReason.USER_REQUEST,
                                context_card=_card())
        # 清空缓存，模拟缓存丢失 / 多实例
        import asyncio

        asyncio.run(cache.delete_prefix("ticket:"))
        second = service.handoff(session_id="s-db", reason=HandoffReason.USER_REQUEST,
                                 context_card=_card())
        assert second.idempotent_hit is True
        assert second.ticket_no == first.ticket_no
        items, total = service.list_tickets()
        assert total == 1

    def test_different_sessions_create_different_tickets(
        self, service: HandoffService
    ) -> None:
        """不同会话各自建单。"""
        a = service.handoff(session_id="s-a", reason=HandoffReason.UNRESOLVED,
                            context_card=_card())
        b = service.handoff(session_id="s-b", reason=HandoffReason.UNRESOLVED,
                            context_card=_card())
        assert a.ticket_no != b.ticket_no


# ---------------------------------------------------------------------------
# 在线 / 离线
# ---------------------------------------------------------------------------


class TestOnlineOffline:
    """在线同步、离线兜底。"""

    def test_offline_path(self, cache: Any, db: Any) -> None:
        """强制离线：不调商家接口，工单标记 offline。"""
        client = MockMerchantTicketPort()
        service = HandoffService(cache=cache, ticket_client=client)
        out = service.handoff(
            session_id="s-off",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
            force_offline=True,
        )
        assert out.offline is True
        assert out.merchant_ticket_no is None
        assert client.calls == []  # 离线不调商家
        assert "http" not in out.text.lower()

    def test_offline_ticket_still_saved(self, cache: Any, db: Any) -> None:
        """离线也要落本地工单（坐席上班后能看到）。"""
        service = HandoffService(cache=cache, ticket_client=MockMerchantTicketPort())
        out = service.handoff(
            session_id="s-off2",
            reason=HandoffReason.SYSTEM_ERROR,
            context_card=_card(),
            force_offline=True,
        )
        detail = service.get_ticket(out.ticket_no)
        assert detail is not None
        assert detail["offline"] is True

    def test_merchant_failure_degrades(self, cache: Any, db: Any) -> None:
        """商家接口失败：本地工单保留，degraded 标记，用户仍收到话术。"""
        client = MockMerchantTicketPort(always_fail=True)
        service = HandoffService(cache=cache, ticket_client=client)
        out = service.handoff(
            session_id="s-fail",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
        )
        assert "TICKET_SYNC_FAILED" in out.degraded
        assert out.merchant_ticket_no is None
        assert out.text  # 话术不为空
        detail = service.get_ticket(out.ticket_no)
        assert detail is not None
        events = [e["event"] for e in detail["events"]]
        assert "FAILED" in events

    def test_rate_limited_degrades(self, cache: Any, db: Any) -> None:
        """令牌桶超限：降级为本地队列，不报错给用户。"""

        class _AlwaysLimited:
            """永远超限的限流器替身（同步抛错即可）。"""

            def acquire(self, key: str, tokens: float = 1.0) -> None:
                raise RateLimitExceeded(retry_after=1)

        service = HandoffService(
            cache=cache, ticket_client=MockMerchantTicketPort(), limiter=_AlwaysLimited()
        )
        out = service.handoff(
            session_id="s-rl",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
        )
        assert "TICKET_RATE_LIMITED" in out.degraded
        detail = service.get_ticket(out.ticket_no)
        events = [e["event"] for e in detail["events"]]
        assert "RATE_LIMITED" in events


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


class TestQueries:
    """工单列表与详情。"""

    def test_list_and_filter(self, service: HandoffService) -> None:
        """列表可按状态/分组过滤。"""
        a = service.handoff(session_id="q1", reason=HandoffReason.HIGH_RISK,
                            context_card=_card())
        service.handoff(session_id="q2", reason=HandoffReason.UNRESOLVED,
                        context_card=_card())
        items, total = service.list_tickets()
        assert total == 2
        assert len(items) == 2
        g5_items, g5_total = service.list_tickets(group_id="G5")
        assert g5_total == 1
        assert g5_items[0]["ticket_no"] == a.ticket_no

    def test_get_missing(self, service: HandoffService) -> None:
        """不存在的工单返回 None（由 API 层转 404）。"""
        assert service.get_ticket("TS000000000000") is None

    def test_detail_has_transcript_and_events(self, service: HandoffService) -> None:
        """详情带对话摘录与事件时间线。"""
        out = service.handoff(
            session_id="q3",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
            transcript=[{"role": "user", "content": "我要换货"}],
        )
        detail = service.get_ticket(out.ticket_no)
        assert detail["transcript"]
        assert isinstance(detail["events"], list)

    def test_transcript_truncated(self, service: HandoffService) -> None:
        """对话摘录只留最近 20 轮。"""
        out = service.handoff(
            session_id="q4",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
            transcript=[{"role": "user", "content": f"第{i}轮"} for i in range(30)],
        )
        detail = service.get_ticket(out.ticket_no)
        assert len(detail["transcript"]) <= 20


# ---------------------------------------------------------------------------
# 补同步
# ---------------------------------------------------------------------------


class TestSyncPending:
    """离线/失败工单的补同步。"""

    def test_sync_pending_recovers(self, cache: Any, db: Any) -> None:
        """先前失败的工单在商家恢复后能被补同步。"""
        from app.core.ratelimit import TokenBucketRateLimiter

        failing = MockMerchantTicketPort(always_fail=True)
        # `ticket_create` 生产口径是 1 QPS：建单 + 补同步连跑必然被限流，
        # 所以这里显式给一个宽桶，专测「补同步」本身的恢复能力。
        service = HandoffService(
            cache=cache,
            ticket_client=failing,
            limiter=TokenBucketRateLimiter(rate=100, capacity=100),
        )
        out = service.handoff(
            session_id="s-sync",
            reason=HandoffReason.USER_REQUEST,
            context_card=_card(),
        )
        assert out.merchant_ticket_no is None

        # 换成正常客户端再补同步
        service._client = MockMerchantTicketPort()  # noqa: SLF001 - 受控替换
        stats = service.sync_pending()
        assert stats["scanned"] >= 1
        assert stats["synced"] >= 1
        detail = service.get_ticket(out.ticket_no)
        assert detail["merchant_ticket_no"]

    def test_sync_pending_empty(self, service: HandoffService) -> None:
        """没有待补同步工单时返回零统计。"""
        stats = service.sync_pending()
        assert stats["scanned"] == 0


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------


class TestSingleton:
    """服务单例。"""

    def test_get_handoff_service_singleton(self) -> None:
        """`get_handoff_service()` 返回同一实例。"""
        assert get_handoff_service() is get_handoff_service()
