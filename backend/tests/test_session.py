"""BE-03 会话状态 / 指代消解 / 槽位继承 单测。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.enums import Emotion
from app.services.session import (
    FORCE_HANDOFF_TURN_THRESHOLD,
    SESSION_KEY_PREFIX,
    SessionState,
    SessionStore,
    assert_no_pii,
    detect_pronoun,
    extract_entities,
    inherit_slots,
    is_comparative,
    is_negative,
    resolve_reference,
    should_force_handoff,
    update_state_after_turn,
)


class _Cache:
    """带 TTL 的内存缓存，模拟 Redis 的 async 接口。"""

    def __init__(self) -> None:
        self.data: dict[str, tuple[Any, float | None]] = {}

    def _expired(self, key: str) -> bool:
        item = self.data.get(key)
        if item is None:
            return True
        _, expire_at = item
        if expire_at is not None and time.time() >= expire_at:
            del self.data[key]
            return True
        return False

    async def get(self, key: str) -> Any:
        return None if self._expired(key) else self.data[key][0]

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        self.data[key] = (value, time.time() + ttl if ttl else None)
        return True

    async def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    async def ttl(self, key: str) -> int:
        item = self.data.get(key)
        if item is None or item[1] is None:
            return -1
        return max(0, int(item[1] - time.time()))


# ---------------------------------------------------------------------------
# 状态序列化
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_roundtrip(self) -> None:
        st = SessionState(
            session_id="s1",
            channel="PDD",
            intent_stack=["商品咨询", "订单服务"],
            slots={"sku_id": "WK-BEAR-20"},
            entities={"sku": ["WK-BEAR-20"]},
            turn_count=3,
        )
        back = SessionState.from_dict(st.to_dict())
        assert back.session_id == "s1"
        assert back.intent_stack == ["商品咨询", "订单服务"]
        assert back.slots == {"sku_id": "WK-BEAR-20"}

    def test_json_roundtrip_keeps_chinese(self) -> None:
        st = SessionState(session_id="s1", intent_stack=["售后服务"])
        raw = st.to_json()
        assert "售后服务" in raw  # 不能被转义成 \\uXXXX
        assert SessionState.from_json(raw).intent_stack == ["售后服务"]

    def test_from_dict_tolerates_missing(self) -> None:
        st = SessionState.from_dict({})
        assert st.turn_count == 0
        assert st.intent_stack == []

    def test_push_intent_dedupes_consecutive(self) -> None:
        st = SessionState(session_id="s")
        st.push_intent("商品咨询")
        st.push_intent("商品咨询")
        st.push_intent("商品咨询")
        assert st.intent_stack == ["商品咨询"], "连续同意图不该撑大栈"

    def test_push_intent_keeps_history(self) -> None:
        st = SessionState(session_id="s")
        st.push_intent("商品咨询")
        st.push_intent("售后服务")
        assert st.current_intent == "售后服务"
        assert st.pop_intent() == "售后服务"
        assert st.current_intent == "商品咨询", "回退后应落在上一个话题"

    def test_to_dict_masks_pii(self) -> None:
        st = SessionState(session_id="s", last_query="我的电话是 13812345678")
        dumped = st.to_dict()
        assert "13812345678" not in str(dumped), "序列化必须过脱敏"


# ---------------------------------------------------------------------------
# PII 兜底自检
# ---------------------------------------------------------------------------


class TestAssertNoPii:
    @pytest.mark.parametrize(
        "text",
        [
            "手机 13812345678",
            "身份证 330106199001011234",
            "邮箱 abc@example.com",
            "浙江省杭州市西湖区文三路100号",
        ],
    )
    def test_blocks_full_pii(self, text: str) -> None:
        with pytest.raises(ValueError):
            assert_no_pii(text, where="test")

    @pytest.mark.parametrize(
        "text",
        [
            "手机 138****5678",
            "订单 2026****0001",
            "下单后 48 小时内发货",
            "满 99 元包邮",
        ],
    )
    def test_allows_masked(self, text: str) -> None:
        assert_no_pii(text, where="test")

    def test_masked_district_allowed_but_door_number_blocked(self) -> None:
        # 省市区保留是合规的（坐席要靠它判断时效），门牌必须屏蔽
        assert_no_pii("浙江省杭州市西湖区", where="t")
        assert_no_pii("浙江省杭州市西湖区**** ", where="t")
        with pytest.raises(ValueError):
            assert_no_pii("浙江省杭州市西湖区**** 100 号 3 幢 502 室", where="t")


# ---------------------------------------------------------------------------
# 实体抽取与指代
# ---------------------------------------------------------------------------


class TestEntities:
    def test_extracts_sku(self) -> None:
        assert extract_entities("WK-BEAR-20 多少钱")["sku"] == ["WK-BEAR-20"]
        assert extract_entities("wk-bear-30 有货吗")["sku"] == ["WK-BEAR-30"]

    def test_extracts_size(self) -> None:
        assert extract_entities("20cm 的")["size"] == ["20cm"]
        assert extract_entities("30厘米的")["size"] == ["30cm"]

    def test_extracts_order_and_dedup(self) -> None:
        got = extract_entities("订单 2026****0001 和 2026****0001")
        assert got["order_no"] == ["2026****0001"]

    def test_empty(self) -> None:
        assert extract_entities("") == {}
        assert extract_entities("你好") == {}


class TestPronoun:
    @pytest.mark.parametrize("text", ["那款多少钱", "这个怎么洗", "它掉毛吗"])
    def test_detects(self, text: str) -> None:
        assert detect_pronoun(text) is not None

    @pytest.mark.parametrize("text", ["这么贵吗", "那样吧", "你们的政策"])
    def test_no_false_positive(self, text: str) -> None:
        # 「这/那」单字不算指代，否则误判率过高
        assert detect_pronoun(text) is None

    def test_comparative_direction(self) -> None:
        assert is_comparative("大的那个") == "max_size"
        assert is_comparative("便宜那款") == "min_price"
        assert is_comparative("那款多少钱") is None


class TestResolveReference:
    def _state(self, **kw: Any) -> SessionState:
        base = {
            "last_skus": ["WK-BEAR-20", "WK-BEAR-30"],
            "last_query": "奶油小熊 20cm 和 30cm 差多少",
        }
        base.update(kw)
        return SessionState(session_id="s", **base)

    def test_new_entity_no_resolve(self) -> None:
        res = resolve_reference("WK-BEAR-40 多少钱", self._state())
        assert res.method == "new_entity"
        assert not res.resolved
        assert "WK-BEAR-40" in res.query

    def test_pronoun_to_last_sku(self) -> None:
        res = resolve_reference("那款多少钱", self._state())
        assert res.resolved
        assert "WK-BEAR-30" in res.query
        assert res.confidence >= 0.8

    def test_comparative_picks_max_size(self) -> None:
        res = resolve_reference("大的那个多少钱", self._state())
        assert res.method == "comparative"
        assert "WK-BEAR-30" in res.query

    def test_comparative_picks_min_size(self) -> None:
        res = resolve_reference("小的那只呢", self._state())
        assert "WK-BEAR-20" in res.query

    def test_keeps_rest_of_sentence(self) -> None:
        res = resolve_reference("那个能机洗吗", self._state())
        assert "能机洗吗" in res.query, "替换指代不能丢掉句子其余部分"

    def test_no_history_low_confidence(self) -> None:
        res = resolve_reference("那款多少钱", self._state(last_skus=[], last_query=""))
        assert res.confidence <= 0.55, "无历史必须降置信让编排层反问"

    def test_plain_sentence_untouched(self) -> None:
        text = "你们的退换货政策是什么"
        res = resolve_reference(text, self._state())
        assert res.query == text
        assert not res.resolved

    def test_size_inherit(self) -> None:
        res = resolve_reference("30cm 的多少钱", self._state())
        assert res.method == "size_inherit"


class TestSlots:
    def test_inherit_keeps_previous(self) -> None:
        st = SessionState(session_id="s", slots={"size": "30cm", "sku_id": "A"})
        slots = inherit_slots(st, {"order_no": ["2026****0001"]})
        assert slots["size"] == "30cm", "本轮没给尺码应继承"
        assert slots["order_no"] == "2026****0001"
        assert slots["sku_id"] == "A"

    def test_inherit_overrides(self) -> None:
        st = SessionState(session_id="s", slots={"size": "30cm"})
        assert inherit_slots(st, {"size": ["20cm"]})["size"] == "20cm"

    def test_update_turn_increments_and_records(self) -> None:
        st = SessionState(session_id="s")
        update_state_after_turn(
            st,
            text="WK-BEAR-20 多少钱",
            resolved_query="WK-BEAR-20 多少钱",
            entities={"sku": ["WK-BEAR-20"]},
            intent="商品咨询",
        )
        assert st.turn_count == 1
        assert st.last_skus == ["WK-BEAR-20"]
        assert st.current_intent == "商品咨询"


class TestThresholds:
    def test_force_handoff_boundary(self) -> None:
        assert FORCE_HANDOFF_TURN_THRESHOLD == 10
        assert not should_force_handoff(SessionState(session_id="s", turn_count=10))
        assert should_force_handoff(SessionState(session_id="s", turn_count=11))

    def test_is_negative(self) -> None:
        assert is_negative(Emotion.ANGRY.value)
        assert is_negative(Emotion.DISSATISFIED.value)
        assert not is_negative(Emotion.NEUTRAL.value)


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


class TestSessionStore:
    def test_save_load_delete(self) -> None:
        store = SessionStore(cache=_Cache(), ttl_seconds=1800)
        st = store.load("x")
        st.turn_count = 2
        store.save(st)
        assert store.load("x").turn_count == 2
        assert store.delete("x") is True
        assert store.load("x").turn_count == 0

    def test_key_prefix(self) -> None:
        assert SESSION_KEY_PREFIX == "wk:session:"

    def test_ttl_expiry(self) -> None:
        store = SessionStore(cache=_Cache(), ttl_seconds=1)
        st = store.load("y")
        st.turn_count = 1
        store.save(st)
        assert store.load("y").turn_count == 1
        time.sleep(1.1)
        assert store.load("y").turn_count == 0, "TTL 到期必须清空"

    def test_save_rejects_pii(self) -> None:
        store = SessionStore(cache=_Cache())
        st = store.load("z")
        st.last_query = "我的手机号 13812345678"
        # to_dict 会脱敏，所以最终能存进去——验证的是脱敏生效
        store.save(st)
        cached = store.cache.data[f"{SESSION_KEY_PREFIX}z"][0]
        assert "13812345678" not in cached

    def test_default_ttl_is_30_minutes(self, monkeypatch) -> None:
        store = SessionStore(cache=_Cache())
        assert store.ttl_seconds == 1800
