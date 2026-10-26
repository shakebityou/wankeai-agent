"""BE-04 对话编排服务单元测试。

测试策略：**全部用替身，不连 Redis / 不连 LLM / 不连商家接口**。
编排层的价值在于「按正确顺序、在正确条件下、用正确预算把几件事串起来」，
所以测的是决策矩阵与降级隔离，不是下游实现细节。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.enums import (
    ActionType,
    Channel,
    HandoffReason,
    IntentCategory,
    RiskLevel,
    ToolName,
)
from app.services.intent import IntentResult
from app.services.orchestrator import (
    ActionDecision,
    Orchestrator,
    ToolOutcome,
    TurnResult,
    _build_filters,
    _fallback_text,
    _has_any,
    _looks_like_query,
    decide_action,
    get_orchestrator,
    set_orchestrator,
)
from app.services.session import SessionState, SessionStore


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _MemCache:
    """极简缓存替身（接口要 async，与真实 Redis 适配器一致）。"""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        self.data[key] = value
        return True

    async def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in list(self.data) if k.startswith(prefix)]
        for k in keys:
            del self.data[k]
        return len(keys)


class _FakeAnswer:
    """假答案服务。"""

    def __init__(
        self,
        *,
        decision: str = "DIRECT_ANSWER",
        text: str = "这是知识库里的答案～",
        exc: Exception | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.decision = decision
        self.text = text
        self.exc = exc
        self.sleep_s = sleep_s
        self.calls: list[str] = []

    def answer(self, question: str, **kw: Any) -> Any:
        self.calls.append(question)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.exc:
            raise self.exc

        class _R:
            pass

        r = _R()
        r.text = self.text
        r.decision = self.decision
        r.top_score = 0.83 if self.decision != "NO_EVIDENCE" else 0.21
        r.hits = ["chunk_a"] if self.decision != "NO_EVIDENCE" else []
        r.degraded = self.decision == "NO_EVIDENCE"
        r.degrade_reason = "NO_EVIDENCE" if self.decision == "NO_EVIDENCE" else ""
        return r


class _FakeTools:
    """假工具注册表。"""

    def __init__(self, *, boom: bool = False, text: str = "您的订单已发货。") -> None:
        self.boom = boom
        self.text = text
        self.calls: list[str] = []

    def invoke(self, tool: Any, **kw: Any) -> Any:
        self.calls.append(getattr(tool, "value", str(tool)))
        if self.boom:
            raise TimeoutError("模拟商家接口超时")

        class _P:
            pass

        p = _P()
        p.data = {"status": "SHIPPED"}
        p.text = self.text
        return p


class _FakeHandoff:
    """假转人工服务。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    def handoff(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("工单服务不可用")

        class _O:
            pass

        o = _O()
        o.text = "已为您转接人工客服，前面还有 2 位～"
        o.ticket_no = "TS000000001234"
        o.queue_position = 2
        o.offline = False
        return o


def _make_orch(
    *,
    answer: Any = None,
    tools: Any = None,
    handoff: Any = None,
) -> Orchestrator:
    """构造一个依赖全部注入的编排器。"""
    return Orchestrator(
        store=SessionStore(cache=_MemCache()),
        answer_service=answer or _FakeAnswer(),
        tool_registry=tools if tools is not None else _FakeTools(),
        handoff_service=handoff if handoff is not None else _FakeHandoff(),
    )


def _intent(cat: IntentCategory, label: str = "通用") -> IntentResult:
    """构造意图结果。"""
    return IntentResult(category=cat, label=label, confidence=0.9)


# ---------------------------------------------------------------------------
# 决策：转人工优先级
# ---------------------------------------------------------------------------


class TestDecisionHandoff:
    """转人工的 5 条触发路径。"""

    def test_explicit_request_by_label(self) -> None:
        """意图标签命中即转人工，reason=USER_REQUEST。"""
        got = decide_action(
            text="帮我转一下",
            intent=_intent(IntentCategory.FALLBACK, "转人工请求"),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.USER_REQUEST

    @pytest.mark.parametrize(
        "text",
        [
            "转人工",
            "我要真人客服",
            "叫你们经理来",
            "找个真人",
            "人工客服在吗",
            "找个活人",
            "给我转个人",
            "要人工",
            "找个人来",
            "换人工",
            "真人呢",
        ],
    )
    def test_explicit_request_by_text(self, text: str) -> None:
        """**即使分类器给出无关标签，文本信号也必须触发转人工**。

        这是 P0 级要求：用户明确要人工被漏掉，会直接引发投诉。

        语料来源：这些是「动宾无穷组合」的典型形态。第一版实现只列了
        「找/要/转人工」几个固定短语，「找个真人」直接漏掉——
        所以最终改用正则按词法结构匹配，这组用例就是那次漏触发的回归。
        """
        got = decide_action(
            text=text,
            intent=_intent(IntentCategory.FALLBACK, "通用"),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.USER_REQUEST

    @pytest.mark.parametrize(
        "text",
        [
            "这个人偶多少钱",
            "人工费怎么算",
            "真人版玩偶有吗",
            "我要买这个",
            "这个材质是什么",
        ],
    )
    def test_non_request_not_misclassified(self, text: str) -> None:
        """**反例同样重要**：含「人」「人工」但不转人工的说法不能误触发。

        正则若写成 `[人]` 这种宽匹配，「人工费」「真人版玩偶」
        都会被判成转人工，等于把正常咨询的用户推给人工，白烧坐席。
        """
        got = decide_action(
            text=text,
            intent=_intent(IntentCategory.PRODUCT_CONSULT),
            state=SessionState(session_id="s"),
        )
        assert got.action is not ActionType.HANDOFF

    @pytest.mark.parametrize(
        "text",
        ["我要投诉", "打 12315 举报", "我要曝光你们", "去法院起诉", "你们这是欺诈"],
    )
    def test_high_risk_by_text(self, text: str) -> None:
        """高风险意图 → HIGH_RISK，且风险等级为 HIGH。"""
        got = decide_action(
            text=text,
            intent=_intent(IntentCategory.FALLBACK),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.HIGH_RISK
        assert got.risk is RiskLevel.HIGH

    def test_high_risk_by_label(self) -> None:
        """标签命中投诉维权也走 HIGH_RISK。"""
        got = decide_action(
            text="这个怎么回事",
            intent=_intent(IntentCategory.AFTER_SALE, "投诉维权"),
            state=SessionState(session_id="s"),
        )
        assert got.handoff_reason is HandoffReason.HIGH_RISK

    def test_emotion_negative(self) -> None:
        """情绪负面 → EMOTION。"""
        got = decide_action(
            text="等了好几天了",
            intent=_intent(IntentCategory.ORDER_SERVICE),
            state=SessionState(session_id="s"),
            emotion_negative=True,
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.EMOTION

    def test_turn_limit_forced(self) -> None:
        """轮次超限强制转人工（BE-03 的 turn_count>10）。"""
        got = decide_action(
            text="我还是没懂",
            intent=_intent(IntentCategory.PRODUCT_CONSULT),
            state=SessionState(session_id="s", turn_count=11),
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.UNRESOLVED

    def test_no_evidence_business_intent(self) -> None:
        """检索无依据且属业务类 → UNRESOLVED。"""
        got = decide_action(
            text="这个支持以旧换新吗",
            intent=_intent(IntentCategory.AFTER_SALE),
            state=SessionState(session_id="s"),
            retrieval_decision="NO_EVIDENCE",
        )
        assert got.action is ActionType.HANDOFF
        assert got.handoff_reason is HandoffReason.UNRESOLVED

    def test_no_evidence_non_business_intent_still_answers(self) -> None:
        """检索无依据但意图是闲聊 → 仍走 RAG（由兜底话术处理，不转人工）。"""
        got = decide_action(
            text="今天天气不错",
            intent=_intent(IntentCategory.MARKETING),
            state=SessionState(session_id="s"),
            retrieval_decision="NO_EVIDENCE",
        )
        assert got.action is ActionType.RAG_ANSWER

    def test_priority_order_explicit_beats_high_risk(self) -> None:
        """同时命中显式请求与高风险时，**显式请求优先**。

        两者都要求不挽留、立即转人工，但工单原因要按用户真实诉求记：
        他说「转人工」是想要人，说「投诉」是想要处理结果。先判显式请求
        能保证 reason 稳定为 USER_REQUEST，不被同一句话里的「投诉」抢走。
        """
        got = decide_action(
            text="转人工，我要投诉",
            intent=_intent(IntentCategory.FALLBACK),
            state=SessionState(session_id="s"),
        )
        assert got.handoff_reason is HandoffReason.USER_REQUEST

    def test_handoff_never_returns_link(self) -> None:
        """转人工决策本身不携带任何链接（HC-1）。"""
        got = decide_action(
            text="转人工",
            intent=_intent(IntentCategory.FALLBACK),
            state=SessionState(session_id="s"),
        )
        assert "http" not in str(got.to_dict())


# ---------------------------------------------------------------------------
# 决策：工具 vs RAG
# ---------------------------------------------------------------------------


class TestDecisionToolVsRag:
    """该查 vs 该答。"""

    @pytest.mark.parametrize(
        "text",
        [
            "帮我查一下订单 2026****0001",
            "我的快递到哪了",
            "运单号是多少",
            "发货了吗",
            "退款到账了吗",
        ],
    )
    def test_query_signals_go_to_tools(self, text: str) -> None:
        """命中查询信号 → TOOL_QUERY。"""
        got = decide_action(
            text=text,
            intent=_intent(IntentCategory.ORDER_SERVICE),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.TOOL_QUERY
        assert ToolName.ORDER_QUERY in got.tools

    @pytest.mark.parametrize(
        "text",
        ["你们退货怎么算", "能机洗吗", "材质是什么", "有优惠券吗", "适合几岁"],
    )
    def test_policy_questions_go_to_rag(self, text: str) -> None:
        """政策类问题 → RAG，**不能去拉用户订单**（多一次越权调用）。"""
        got = decide_action(
            text=text,
            intent=_intent(IntentCategory.PRODUCT_CONSULT),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.RAG_ANSWER

    def test_short_followup_with_slot_order_no(self) -> None:
        """槽位里已有订单号 + 追问短句 → 走工具。"""
        state = SessionState(session_id="s")
        state.slots["order_no"] = "2026****0001"
        got = decide_action(
            text="那还要多久",
            intent=_intent(IntentCategory.ORDER_SERVICE),
            state=state,
        )
        assert got.action is ActionType.TOOL_QUERY

    def test_long_sentence_with_slot_does_not_force_tool(self) -> None:
        """长句不因为槽位有单号就走工具（避免误触发多一次越权调用）。"""
        state = SessionState(session_id="s")
        state.slots["order_no"] = "2026****0001"
        got = decide_action(
            text="我想问一下你们家退换货的具体政策是怎么规定的呢",
            intent=_intent(IntentCategory.AFTER_SALE),
            state=state,
        )
        assert got.action is ActionType.RAG_ANSWER

    def test_no_tool_intents_for_pure_consult(self) -> None:
        """纯咨询意图没有配工具，只能是 RAG。"""
        got = decide_action(
            text="这个玩偶是哪部动画的",
            intent=_intent(IntentCategory.BRAND_TRUST),
            state=SessionState(session_id="s"),
        )
        assert got.action is ActionType.RAG_ANSWER
        assert got.tools == []


# ---------------------------------------------------------------------------
# 决策辅助函数
# ---------------------------------------------------------------------------


class TestDecisionHelpers:
    """辅助函数。"""

    def test_has_any(self) -> None:
        """命中任一即真。"""
        assert _has_any("我要转人工", ("转人工",)) is True
        assert _has_any("我要买", ("转人工",)) is False

    def test_looks_like_query_with_order_no(self) -> None:
        """带订单号即算查询。"""
        assert _looks_like_query("2026****0001 怎么样了", SessionState(session_id="s"))

    def test_looks_like_query_empty(self) -> None:
        """无信号词无槽位 → 不是查询。"""
        assert not _looks_like_query("能机洗吗", SessionState(session_id="s"))

    def test_build_filters_never_filters_by_sku(self) -> None:
        """**按 SKU 过滤会漏掉通用政策文档**，所以这里刻意返回空过滤。

        政策类文档的 `sku_ids` 是空的，若按 SKU 过滤，
        「你们家退换货怎么算」就命中不到政策文档。
        """
        state = SessionState(session_id="s")
        state.slots["sku_id"] = "WK-BEAR-20"
        assert _build_filters(state) == {}

    def test_fallback_texts_never_infinite_wait(self) -> None:
        """兜底话术**禁止「请稍等」类无限等待表述**（BE-15 DoD）。"""
        for key in ("ANSWER_FAILED", "TOOL_LAYER_ABSENT", "TOOL_FAILED", "HANDOFF"):
            text = _fallback_text(key)
            assert "请稍等" not in text
            assert "马上" not in text
            assert text

    def test_action_decision_to_dict(self) -> None:
        """导出字典结构。"""
        d = ActionDecision(
            action=ActionType.TOOL_QUERY,
            reason="r",
            tools=[ToolName.ORDER_QUERY],
            handoff_reason=HandoffReason.UNRESOLVED,
            risk=RiskLevel.MEDIUM,
        ).to_dict()
        assert d["action"] == "tool_query"
        assert d["tools"] == ["order_query"]
        assert d["handoff_reason"] == "UNRESOLVED"
        assert d["risk"] == "MEDIUM"

    def test_action_decision_to_dict_no_handoff(self) -> None:
        """无转人工原因时该字段为 None。"""
        d = ActionDecision(action=ActionType.RAG_ANSWER, reason="r").to_dict()
        assert d["handoff_reason"] is None


# ---------------------------------------------------------------------------
# handle_turn 端到端
# ---------------------------------------------------------------------------


class TestHandleTurn:
    """一轮完整处理。"""

    def test_rag_path(self) -> None:
        """该答：产出回复、actions 含 rag_answer、rag_hits 有内容。"""
        orch = _make_orch()
        res = orch.handle_turn(session_id="t1", text="这个能机洗吗")
        assert res.reply_content
        assert "rag_answer" in res.actions
        assert res.rag_hits
        assert res.handoff is None
        assert res.intent
        assert res.emotion
        assert res.decision

    def test_tool_path_calls_registry(self) -> None:
        """该查：真的调用了工具注册表。"""
        tools = _FakeTools()
        orch = _make_orch(tools=tools)
        res = orch.handle_turn(session_id="t2", text="帮我查订单 2026****0001 到哪了")
        assert tools.calls
        assert ToolName.ORDER_QUERY.value in res.actions
        assert res.reply_content

    def test_tool_and_rag_both_run(self) -> None:
        """工具与 RAG 在同一轮里都跑了（并行复合分支）。"""
        tools = _FakeTools()
        answer = _FakeAnswer()
        orch = _make_orch(tools=tools, answer=answer)
        res = orch.handle_turn(
            session_id="t3", text="我的订单 2026****0001 发货了吗，你们发货规则是什么"
        )
        assert tools.calls
        assert answer.calls
        assert "rag_answer" in res.actions

    def test_handoff_path(self) -> None:
        """该转：建单、返回 handoff 信息、actions 含 handoff。"""
        handoff = _FakeHandoff()
        orch = _make_orch(handoff=handoff)
        res = orch.handle_turn(session_id="t4", text="转人工")
        assert handoff.calls
        assert res.handoff is not None
        assert res.handoff["reason"] == "USER_REQUEST"
        assert res.handoff["ticket_no"] == "TS000000001234"
        assert "handoff" in res.actions

    def test_handoff_context_card_has_seven_fields(self) -> None:
        """坐席上下文卡片七要素齐全（缺一即缺陷）。"""
        handoff = _FakeHandoff()
        orch = _make_orch(handoff=handoff)
        orch.handle_turn(session_id="t5", text="转人工")
        card = handoff.calls[0]["context_card"]
        for key in (
            "summary",
            "verified",
            "tried_actions",
            "unmet_demand",
            "suggested_action",
            "emotion",
            "turn_count",
        ):
            assert key in card, f"上下文卡片缺 {key}"

    def test_handoff_passes_reason_enum(self) -> None:
        """传给转人工服务的是枚举而非字符串（避免下游拼错）。"""
        handoff = _FakeHandoff()
        orch = _make_orch(handoff=handoff)
        orch.handle_turn(session_id="t6", text="我要投诉")
        assert handoff.calls[0]["reason"] is HandoffReason.HIGH_RISK

    def test_state_persisted(self) -> None:
        """处理后状态被写回：轮次 +1、意图入栈。"""
        orch = _make_orch()
        orch.handle_turn(session_id="t7", text="这个能机洗吗")
        state = orch.store.load("t7")
        assert state.turn_count >= 1
        assert state.intent_stack

    def test_empty_text_friendly_reply(self) -> None:
        """空白文本不崩，给出澄清提示。"""
        orch = _make_orch()
        res = orch.handle_turn(session_id="t8", text="   ")
        assert res.reply_content

    def test_enable_tools_false_still_answers(self) -> None:
        """禁用工具时不能抛异常，退化为 RAG 回答。"""
        orch = _make_orch()
        res = orch.handle_turn(
            session_id="t9", text="帮我查订单 2026****0001", enable_tools=False
        )
        assert res.reply_content

    def test_channel_recorded(self) -> None:
        """渠道透传到会话状态（渠道话术与统计都靠它）。"""
        orch = _make_orch()
        orch.handle_turn(session_id="t10", text="你好", channel=Channel.PDD.value)
        assert orch.store.load("t10").channel == Channel.PDD.value


# ---------------------------------------------------------------------------
# 降级隔离
# ---------------------------------------------------------------------------


class TestDegradation:
    """单步失败只降级自己那一路。"""

    def test_tool_failure_still_replies(self) -> None:
        """工具全挂 → 兜底话术 + 降级标记，不抛异常。"""
        orch = _make_orch(tools=_FakeTools(boom=True))
        res = orch.handle_turn(session_id="d1", text="帮我查订单 2026****0001")
        assert res.reply_content
        assert any("FAILED" in d for d in res.degraded)

    def test_tool_failure_does_not_kill_rag(self) -> None:
        """**工具挂了 RAG 仍要出结果**——这是并行分支的核心价值。"""
        orch = _make_orch(tools=_FakeTools(boom=True), answer=_FakeAnswer())
        res = orch.handle_turn(
            session_id="d2", text="我的订单 2026****0001 发货了吗，你们发货规则是什么"
        )
        assert res.reply_content
        assert "rag_answer" in res.actions or res.rag_hits

    def test_rag_failure_still_replies(self) -> None:
        """RAG 抛异常 → 兜底话术 + RAG_FAILED。"""
        orch = _make_orch(answer=_FakeAnswer(exc=RuntimeError("boom")))
        res = orch.handle_turn(session_id="d3", text="这个能机洗吗")
        assert res.reply_content
        assert "RAG_FAILED" in res.degraded

    def test_rag_failure_does_not_kill_tools(self) -> None:
        """RAG 挂了工具结果仍要出。"""
        tools = _FakeTools()
        orch = _make_orch(answer=_FakeAnswer(exc=RuntimeError("boom")), tools=tools)
        res = orch.handle_turn(
            session_id="d4", text="我的订单 2026****0001 发货了吗，你们发货规则是什么"
        )
        assert tools.calls
        assert res.reply_content

    def test_handoff_failure_falls_back(self) -> None:
        """工单服务挂了仍要给用户可读话术，不能 500。"""
        orch = _make_orch(handoff=_FakeHandoff(boom=True))
        res = orch.handle_turn(session_id="d5", text="转人工")
        assert res.reply_content
        assert "HANDOFF_FAILED" in res.degraded
        assert "请稍等" not in res.reply_content

    def test_no_evidence_marks_degraded(self) -> None:
        """检索无依据要标记降级（运维据此看知识库缺口）。"""
        orch = _make_orch(answer=_FakeAnswer(decision="NO_EVIDENCE"))
        res = orch.handle_turn(session_id="d6", text="能机洗吗")
        assert res.degraded or res.decision.get("rag_decision") == "NO_EVIDENCE"

    def test_tool_layer_absent(self) -> None:
        """**工具层整体不可用**（BE-07~09 未部署 / 商家接口未配）时不能崩。

        注意触发路径：请求先被判成 TOOL_QUERY（说明用户确实在查单），
        然后编排层发现注册表拿不到，此时必须退回可读话术。
        如果一开始就没有工具（`tool_registry=None` 且属性也返回 None），
        决策层会走 RAG 分支，根本到不了这里——所以这里要模拟
        「决策时可用、执行时不可用」的运行期失效。
        """
        orch = _make_orch(answer=_FakeAnswer(), tools=_FakeTools())
        # 先确认正常路径会走工具
        warm = orch.handle_turn(session_id="d7a", text="帮我查订单 2026****0001")
        assert ToolName.ORDER_QUERY.value in warm.actions

        # 运行期把注册表丢掉（等价于依赖配置被清空）
        orch._tools = None  # noqa: SLF001
        import app.services.orchestrator as mod

        class _RuntimeLost(mod.Orchestrator):
            @property
            def tool_registry(self) -> Any:
                return None

        orchid = orch
        orch.__class__ = _RuntimeLost  # type: ignore[assignment]
        res = orch.handle_turn(session_id="d7b", text="帮我查订单 2026****0002")
        assert res.reply_content
        assert "TOOL_LAYER_ABSENT" in res.degraded
        assert "请稍等" not in res.reply_content

    def test_handoff_service_absent(self) -> None:
        """转人工服务整体缺失时给出纯文字话术（不发链接，HC-1）。"""
        import app.services.orchestrator as mod

        class _NoHandoff(mod.Orchestrator):
            @property
            def handoff_service(self) -> Any:
                return None

        orch = _NoHandoff(
            store=SessionStore(cache=_MemCache()),
            answer_service=_FakeAnswer(),
            tool_registry=_FakeTools(),
            handoff_service=None,
        )
        res = orch.handle_turn(session_id="d8", text="转人工")
        assert res.reply_content
        assert res.handoff is not None
        assert res.handoff["offline"] is True
        assert res.handoff["ticket_no"] is None
        assert "http" not in res.reply_content

    def test_tool_outcome_data_captured(self) -> None:
        """工具返回的 data 要被记录（坐席卡片要靠它填 verified）。"""
        tools = _FakeTools()
        orch = _make_orch(tools=tools)
        res = orch.handle_turn(session_id="d9", text="帮我查订单 2026****0001")
        assert tools.calls
        assert res.actions


# ---------------------------------------------------------------------------
# 契约与耗时
# ---------------------------------------------------------------------------


class TestContractShape:
    """契约 2.4.1 五段结构。"""

    def test_five_sections(self) -> None:
        """必须正好含五段。"""
        body = _make_orch().handle_turn(session_id="c1", text="能机洗吗").to_contract()
        for key in ("reply", "actions", "rag_hits", "handoff", "latency_ms"):
            assert key in body

    def test_cards_always_empty(self) -> None:
        """**cards 恒为空**——HC-1 禁止任何跳转卡片。"""
        body = _make_orch().handle_turn(session_id="c2", text="能机洗吗").to_contract()
        assert body["reply"]["cards"] == []

    def test_msg_type_only_text_or_image(self) -> None:
        """只发 TEXT/IMAGE，不发 CARD/GOODS。"""
        body = _make_orch().handle_turn(session_id="c3", text="能机洗吗").to_contract()
        assert body["reply"]["msg_type"] in ("TEXT", "IMAGE")

    def test_handoff_null_when_not_handoff(self) -> None:
        """未转人工时 handoff 必须是 null 而不是空对象。"""
        body = _make_orch().handle_turn(session_id="c4", text="能机洗吗").to_contract()
        assert body["handoff"] is None

    def test_latency_has_self_section(self) -> None:
        """耗时要能区分「编排层自身」与外部依赖（DoD ⑤ 的考核对象）。"""
        lat = _make_orch().handle_turn(session_id="c5", text="你好").latency_ms
        assert "self" in lat
        assert "total" in lat
        assert lat["self"] <= lat["total"] + 1

    def test_rag_hits_do_not_leak_full_content(self) -> None:
        """rag_hits 只回摘要，不回全文（避免知识库内容泄漏到渠道）。"""
        res = _make_orch().handle_turn(session_id="c6", text="能机洗吗")
        for hit in res.rag_hits:
            assert "content" not in hit
            assert "chunk_id" in hit

    def test_turn_result_to_contract_keys(self) -> None:
        """TurnResult 空实例也能导出契约（不崩）。"""
        body = TurnResult().to_contract()
        assert body["reply"]["content"] == ""
        assert body["actions"] == []


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------


class TestSingleton:
    """单例管理。"""

    def test_get_orchestrator_returns_same(self) -> None:
        """重复取到同一实例（避免每轮重建会话仓库）。"""
        set_orchestrator(None)
        a = get_orchestrator()
        b = get_orchestrator()
        assert a is b
        set_orchestrator(None)

    def test_set_orchestrator_replaces(self) -> None:
        """可替换（测试注入用）。"""
        fake = _make_orch()
        set_orchestrator(fake)
        assert get_orchestrator() is fake
        set_orchestrator(None)


# ---------------------------------------------------------------------------
# ToolOutcome
# ---------------------------------------------------------------------------


class TestToolOutcome:
    """工具结果数据结构。"""

    def test_defaults(self) -> None:
        """默认值：成功、无数据、无错误。"""
        o = ToolOutcome(tool=ToolName.ORDER_QUERY, ok=True)
        assert o.data == {}
        assert o.text == ""
        assert o.degraded is False

    def test_failure_shape(self) -> None:
        """失败形状带错误信息与降级标记。"""
        o = ToolOutcome(tool=ToolName.ORDER_QUERY, ok=False, error="timeout", degraded=True)
        assert o.error == "timeout"
        assert o.degraded is True


# ---------------------------------------------------------------------------
# rag_hits 契约摘要（BE-04 契约 2.4.1）
# ---------------------------------------------------------------------------


class TestRagHitsBrief:
    """`rag_hits` 必须带知识类目，不能只回一个 chunk_id。"""

    def test_category_from_chunk_prefix(self) -> None:
        """chunk_id 前缀可推出类目。"""
        from app.services.orchestrator import _category_from_chunk_id

        assert _category_from_chunk_id("kb_faq:abc") == "常见问题"
        assert _category_from_chunk_id("kb_product:abc") == "商品知识"
        assert _category_from_chunk_id("kb_policy:abc") == "政策条款"
        assert _category_from_chunk_id("kb_manual:abc") == "人工录入"

    def test_unknown_prefix_returns_none(self) -> None:
        """推不出来的前缀返回 None，**不猜**。"""
        from app.services.orchestrator import _category_from_chunk_id

        assert _category_from_chunk_id("weird:abc") is None
        assert _category_from_chunk_id("") is None

    def test_hits_to_brief_carries_category_and_score(self) -> None:
        """摘要含 chunk_id / score / category 三键。"""
        from app.services.orchestrator import _hits_to_brief

        class _Ans:
            hits = ["kb_faq:1", "kb_product:2"]
            top_score = 0.81234

        brief = _hits_to_brief(_Ans())
        assert [h["chunk_id"] for h in brief] == ["kb_faq:1", "kb_product:2"]
        assert [h["category"] for h in brief] == ["常见问题", "商品知识"]
        assert brief[0]["score"] == 0.8123

    def test_hits_to_brief_prefers_explicit_metadata(self) -> None:
        """`AnswerResult` 若已带 hit_metadata，优先用真实类目而不是前缀推断。"""
        from app.services.orchestrator import _hits_to_brief

        class _Ans:
            hits = ["kb_faq:1"]
            top_score = 0.9
            hit_metadata = [{"category": "洗护保养"}]

        brief = _hits_to_brief(_Ans())
        assert brief[0]["category"] == "洗护保养"

    def test_hits_to_brief_empty(self) -> None:
        """无命中返回空列表。"""
        from app.services.orchestrator import _hits_to_brief

        class _Ans:
            hits: list[str] = []
            top_score = 0.0

        assert _hits_to_brief(_Ans()) == []


class TestToolLayerAbsent:
    """工具层整体不可用必须显式记降级，不能静默留空。"""

    def test_absent_tool_registry_marks_degraded(self, monkeypatch) -> None:
        """registry 为 None 时本轮 degraded 含 TOOL_LAYER_ABSENT。"""
        import app.services.orchestrator as mod

        orchid = _make_orch(answer=_FakeAnswer(), tools=_FakeTools())
        orchid.handle_turn(session_id="warm", text="帮我查订单 2026****0001")

        class _Lost(mod.Orchestrator):
            @property
            def tool_registry(self):  # noqa: ANN201
                return None

        orchid.__class__ = _Lost  # type: ignore[assignment]
        res = orchid.handle_turn(session_id="deg", text="帮我查订单 2026****0002")
        assert "TOOL_LAYER_ABSENT" in res.degraded
        assert res.reply_content  # 仍要给出回复，不能空着嘴
