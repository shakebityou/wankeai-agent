"""BE-16 LLM 网关 + AI-03 三道闸单元测试。

测试策略
--------
全部用**假 Provider / 假缓存**注入，不连任何真实模型 API：

* 真实 API 调用慢、要钱、结果不确定，测不了「主备切换是否只切一次」
  这类状态机行为；
* 网关的价值恰恰在于**故障时的行为**（超时、限流、返回空、返回带外链
  的文本），这些只能用可编程的假实现来构造。

与 `scripts/check_llm_gateway.py` 的分工：脚本是给交付验收看的
「DoD 逐条跑一遍」，本文件是给 CI 看的「每个行为都有断言」。
"""

from __future__ import annotations

import pytest

from app.adapters.ports import LlmResult
from app.services.answer import (
    SYSTEM_PROMPT,
    AnswerService,
    build_context_prompt,
    context_texts,
)
from app.services.gates import (
    FALLBACK_NO_EVIDENCE,
    GateResult,
    extract_facts,
    gate_evidence,
    gate_groundedness,
    run_gates,
)
from app.services.llm_gateway import (
    CACHE_PREFIX,
    MAX_REPLY_CHARS,
    PROMPT_VERSION,
    LlmBudgetExceededError,
    LlmGateway,
    TokenMeter,
    truncate_reply,
)


class _FakeProvider:
    """可控的假模型通道。"""

    def __init__(
        self,
        *,
        text: str = "您好，这款玩偶支持 7 天无理由退货。",
        name: str = "fake",
        fail: bool = False,
        fail_times: int | None = None,
    ) -> None:
        self.text = text
        self.name = name
        self.calls = 0
        self._fail = fail
        self._fail_times = fail_times

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
    ) -> LlmResult:
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"{self.name} 通道故障")
        if self._fail_times is not None and self.calls <= self._fail_times:
            raise TimeoutError(f"{self.name} 超时")
        return LlmResult(
            text=self.text,
            prompt_tokens=100,
            completion_tokens=30,
            model="fake-model",
            provider=self.name,
        )

    async def stream(self, prompt: str, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"{self.name} 通道故障")
        for piece in self.text:
            yield piece


class _MemCache:
    """极简内存缓存（异步接口，与 Redis 适配器同签名）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        """取。"""
        return self.data.get(key)

    async def set(self, key: str, value, ttl: int | None = None):  # noqa: ANN001, ANN201
        """存。"""
        self.data[key] = value
        return True

    async def delete(self, key: str):  # noqa: ANN201
        """删。"""
        self.data.pop(key, None)
        return True

    async def delete_prefix(self, prefix: str) -> int:  # noqa: ANN201
        """按前缀删。"""
        keys = [k for k in self.data if k.startswith(prefix)]
        for k in keys:
            del self.data[k]
        return len(keys)


class _Hit:
    """检索命中的最小替身。"""

    def __init__(self, content: str, title: str = "条款") -> None:
        self.content = content
        self.title = title


# ---------------------------------------------------------------------------
# AI-03 闸①：依据闸
# ---------------------------------------------------------------------------


class TestGateEvidence:
    """闸①：没有依据就绝不生成。"""

    def test_no_evidence_blocks(self) -> None:
        r = gate_evidence(decision="NO_EVIDENCE", hits=[])
        assert not r.passed
        assert r.gate == "evidence"

    def test_empty_hits_blocks_even_with_decision(self) -> None:
        # 有 decision 但片段为空，等价于没有依据
        assert not gate_evidence(decision="DIRECT_ANSWER", hits=[]).passed

    def test_direct_answer_with_hits_passes(self) -> None:
        assert gate_evidence(decision="DIRECT_ANSWER", hits=[_Hit("7 天无理由")]).passed

    def test_weak_with_hits_passes(self) -> None:
        # WEAK 是否答由 BE-05 阈值分流决定，闸只关心「有没有片段」
        assert gate_evidence(decision="WEAK", hits=[_Hit("运费由商家承担")]).passed


# ---------------------------------------------------------------------------
# AI-03 闸②：事实闸
# ---------------------------------------------------------------------------


class TestGateGroundedness:
    """闸②：回复里的每个数字都必须能在上下文里找到出处。"""

    def test_extract_facts_by_kind(self) -> None:
        facts = extract_facts("48 小时内发货，7 天无理由，售价 99 元。")
        kinds = {k for k, _ in facts}
        assert "duration" in kinds
        assert "money" in kinds

    def test_grounded_numbers_pass(self) -> None:
        ctx = "现货商品 48 小时内发出，支持 7 天无理由退货。"
        r = gate_groundedness("我们会 48 小时内发出，支持 7 天无理由退货。", [ctx])
        assert r.passed, r.violations

    def test_fabricated_duration_blocked(self) -> None:
        ctx = "现货商品 48 小时内发出。"
        r = gate_groundedness("我们 24 小时内一定发出。", [ctx])
        assert not r.passed
        assert any("24" in v for v in r.violations)

    def test_fabricated_price_blocked(self) -> None:
        ctx = "该商品售价 99 元。"
        r = gate_groundedness("现在只要 199 元。", [ctx])
        assert not r.passed

    def test_promise_without_source_blocked(self) -> None:
        ctx = "支持 7 天无理由退货。"
        r = gate_groundedness("我保证一定能退。", [ctx])
        assert not r.passed

    def test_promise_check_can_be_disabled(self) -> None:
        # 关闭承诺检查后，数字仍比对（不能因此放过编造的数字）
        ctx = "支持 7 天无理由退货。"
        r = gate_groundedness(
            "48 小时发货，我保证。", [ctx], strict_promises=False
        )
        assert not r.passed  # 48 小时无出处，仍然拦

    def test_empty_reply_blocked(self) -> None:
        assert not gate_groundedness("", ["有内容"]).passed


# ---------------------------------------------------------------------------
# AI-03 闸③：合规闸
# ---------------------------------------------------------------------------


class TestGateCompliance:
    """闸③：外链 / 跳转话术一律拦。"""

    @pytest.mark.parametrize(
        "text",
        [
            "详情见 https://item.taobao.com/1",
            "到 www.taobao.com 看",
            "点这里跳转购买",
        ],
    )
    def test_external_links_blocked(self, text: str) -> None:
        from app.services.gates import gate_compliance

        assert not gate_compliance(text).passed

    def test_absolute_phrase_is_flag_not_block(self) -> None:
        from app.services.gates import gate_compliance

        r = gate_compliance("这是绝对最好的选择")
        # 绝对化用语是 flag 级：记录+告警，不硬拦
        assert r.passed
        assert r.flags

    def test_clean_text_passes(self) -> None:
        from app.services.gates import gate_compliance

        r = gate_compliance("毛绒玩偶支持 7 天无理由退货，请联系客服核实。")
        assert r.passed


# ---------------------------------------------------------------------------
# 三道闸串联
# ---------------------------------------------------------------------------


class TestRunGates:
    """三闸串联的短路顺序。"""

    def test_evidence_gate_short_circuits(self) -> None:
        d = run_gates(
            reply="随便答",
            context=[""],
            decision="NO_EVIDENCE",
            hits=[],
            fallback_text=FALLBACK_NO_EVIDENCE,
        )
        assert not d.passed
        assert d.blocked_by == "evidence"
        assert d.used_fallback

    def test_groundedness_blocks_second(self) -> None:
        d = run_gates(
            reply="我们 24 小时发货。",
            context=["48 小时内发货。"],
            decision="DIRECT_ANSWER",
            hits=[_Hit("48 小时内发货。")],
            fallback_text="兜底",
        )
        assert not d.passed
        assert d.blocked_by == "groundedness"

    def test_all_pass(self) -> None:
        ctx = "现货商品 48 小时内发出。"
        d = run_gates(
            reply="我们会 48 小时内发出。",
            context=[ctx],
            decision="DIRECT_ANSWER",
            hits=[_Hit(ctx)],
            fallback_text="兜底",
        )
        assert d.passed
        assert not d.used_fallback

    def test_evidence_only_skips_later_gates(self) -> None:
        # 生成前预检：回复还是空串，只该跑闸①，不能被闸②误拦
        d = run_gates(
            reply="",
            context=["有内容"],
            decision="DIRECT_ANSWER",
            hits=[_Hit("有内容")],
            fallback_text="兜底",
            evidence_only=True,
        )
        assert d.passed

    def test_gates_can_be_disabled(self) -> None:
        d = run_gates(
            reply="看 https://taobao.com",
            context=[""],
            decision="NO_EVIDENCE",
            hits=[],
            fallback_text="兜底",
            enabled=False,
        )
        assert d.passed


# ---------------------------------------------------------------------------
# BE-16 网关：主备切换
# ---------------------------------------------------------------------------


class TestGatewayFailover:
    """主备切换只该切一次，且冷却期内不再白试主通道。"""

    def _gw(self, primary, fallback, cache=None) -> LlmGateway:  # noqa: ANN001
        return LlmGateway(primary=primary, fallback=fallback, cache=cache or _MemCache())

    def test_primary_ok_no_failover(self) -> None:
        p, f = _FakeProvider(name="p"), _FakeProvider(name="f")
        ans = self._gw(p, f).complete("q", use_cache=False)
        assert not ans.usage.failover
        assert f.calls == 0

    def test_primary_down_switches_to_fallback(self) -> None:
        p = _FakeProvider(name="p", fail=True)
        f = _FakeProvider(name="f")
        ans = self._gw(p, f).complete("q", use_cache=False)
        assert ans.usage.failover
        assert not ans.usage.degraded

    def test_cooldown_does_not_retry_primary(self) -> None:
        """**回归用例**：备通道成功不得解除冷却。

        早期实现里 `_mark_success()` 不带 role，备通道一成功就清了冷却，
        导致下一轮又先试主、又超时、又切备——每轮白付一次主通道超时。
        """
        p = _FakeProvider(name="p", fail=True)
        f = _FakeProvider(name="f")
        gw = self._gw(p, f)
        for _ in range(5):
            gw.complete("q", use_cache=False)
        # 冷却期内主通道应只在最初几次被调用过，之后不再增长
        calls_after_warmup = p.calls
        gw.complete("q", use_cache=False)
        assert p.calls == calls_after_warmup

    def test_both_down_degrades(self) -> None:
        p = _FakeProvider(name="p", fail=True)
        f = _FakeProvider(name="f", fail=True)
        ans = self._gw(p, f).complete("q", use_cache=False)
        assert ans.usage.degraded
        assert ans.text == ""


# ---------------------------------------------------------------------------
# BE-16 网关：缓存与预算
# ---------------------------------------------------------------------------


class TestGatewayCacheAndBudget:
    """缓存命中率与 token 预算。"""

    def test_same_question_hits_cache(self) -> None:
        p = _FakeProvider(name="p")
        gw = LlmGateway(primary=p, fallback=_FakeProvider(), cache=_MemCache())
        a1 = gw.complete("q", question="这个怎么洗", use_cache=True)
        a2 = gw.complete("q", question="这个怎么洗", use_cache=True)
        assert not a1.from_cache
        assert a2.from_cache
        assert p.calls == 1  # 第二次没打模型

    def test_different_context_misses_cache(self) -> None:
        """换商品必须换答案，所以缓存键含上下文指纹。"""
        p = _FakeProvider(name="p")
        gw = LlmGateway(primary=p, fallback=_FakeProvider(), cache=_MemCache())
        gw.complete("q", question="这个怎么洗", context_fingerprints=["A"], use_cache=True)
        gw.complete("q", question="这个怎么洗", context_fingerprints=["B"], use_cache=True)
        assert p.calls == 2

    def test_cache_key_contains_prompt_version(self) -> None:
        """改提示词版本必须换缓存键。

        `PROMPT_VERSION` 被塞进 payload 一起哈希，**明文不出现在 key 里**
        ——所以不能断言 `PROMPT_VERSION in key`，而要断言「版本变了 key 就变」。
        这才是「提示词版本化」真正要保证的行为：升级提示词后旧缓存自动失效，
        否则老提示词生成的话术会继续被发出去。
        """
        gw = LlmGateway(primary=_FakeProvider(), fallback=_FakeProvider(), cache=_MemCache())
        key = gw.cache_key("这个怎么洗", context_fingerprints=["a"], channel="TAOBAO")
        assert key.startswith(CACHE_PREFIX)

        import app.services.llm_gateway as mod

        original = mod.PROMPT_VERSION
        try:
            mod.PROMPT_VERSION = original + "-bumped"
            key2 = gw.cache_key("这个怎么洗", context_fingerprints=["a"], channel="TAOBAO")
        finally:
            mod.PROMPT_VERSION = original
        assert key != key2, "提示词版本变更后缓存键必须改变"
        # 同参数同版本必须稳定（否则永不命中）
        assert gw.cache_key("这个怎么洗", context_fingerprints=["a"], channel="TAOBAO") == key

    def test_cache_key_isolated_by_channel(self) -> None:
        gw = LlmGateway(primary=_FakeProvider(), fallback=_FakeProvider(), cache=_MemCache())
        a = gw.cache_key("怎么洗", context_fingerprints=["x"], channel="TAOBAO")
        b = gw.cache_key("怎么洗", context_fingerprints=["x"], channel="WECHAT")
        assert a != b, "不同渠道话术口径不同，不能共用缓存"

    def test_clear_cache(self) -> None:
        gw = LlmGateway(primary=_FakeProvider(), fallback=_FakeProvider(), cache=_MemCache())
        gw.complete("q", question="x", use_cache=True)
        removed = gw.clear_cache()
        assert removed >= 1

    def test_budget_alert_once_per_day(self, caplog) -> None:  # noqa: ANN001
        """日用量达 80% 告警，且**同日只告警一次**。

        `TokenMeter` 没有公开的 `alert_count` 属性——告警走 `logger.warning`，
        状态记在私有的 `_alerted_day` 上。所以这里数日志条数，既验证了
        告警真的发出去了，也验证了去重。
        """
        import logging

        meter = TokenMeter(daily_budget=1000, session_limit=500, alert_ratio=0.8)
        with caplog.at_level(logging.WARNING, logger="app.services.llm_gateway"):
            meter.check(session_id="s1", estimated=10)  # 79% 以下，不告警
            assert not [r for r in caplog.records if "告警阈值" in r.getMessage()]
            meter.record(total_tokens=810, session_id="s1")  # 81%，触发
            fired = len([r for r in caplog.records if "告警阈值" in r.getMessage()])
            assert fired == 1
            meter.record(total_tokens=50, session_id="s1")  # 同日再涨，不重复告警
            again = len([r for r in caplog.records if "告警阈值" in r.getMessage()])
            assert again == fired, "同日只告警一次"

    def test_budget_exhausted_raises(self) -> None:
        meter = TokenMeter(daily_budget=100, session_limit=100, alert_ratio=0.8)
        meter.record(total_tokens=100, session_id="s1")
        with pytest.raises(LlmBudgetExceededError):
            meter.check(session_id="s1", estimated=1)

    def test_session_limit_independent(self) -> None:
        meter = TokenMeter(daily_budget=1_000_000, session_limit=100, alert_ratio=0.8)
        meter.record(total_tokens=100, session_id="s1")
        with pytest.raises(LlmBudgetExceededError):
            meter.check(session_id="s1", estimated=1)
        # 换会话不受影响
        meter.check(session_id="s2", estimated=1)


# ---------------------------------------------------------------------------
# 出口字数
# ---------------------------------------------------------------------------


class TestTruncateReply:
    """单条 ≤ 120 字（BE-02 / BE-16）。"""

    def test_short_text_untouched(self) -> None:
        assert truncate_reply("短句。") == "短句。"

    def test_case2_boundary_after_half(self) -> None:
        text = "填充内容" * 15 + "啊" * 3 + "。" + "后续内容" * 30
        out = truncate_reply(text, limit=MAX_REPLY_CHARS)
        print("CASE2 len=", len(out), "tail=", repr(out[-3:]))
        assert len(out) <= MAX_REPLY_CHARS
        assert out.endswith("。")
        assert "后续内容" not in out

    def test_hard_cut_when_no_punctuation(self) -> None:
        out = truncate_reply("啊" * 300, limit=120)
        assert len(out) <= 121
        assert out.endswith("…")


# ---------------------------------------------------------------------------
# 提示词与上下文
# ---------------------------------------------------------------------------


class TestContextPrompt:
    """RAG 上下文只送 Top-3。"""

    def _hits(self, n: int) -> list[_Hit]:
        return [_Hit(f"片段{i}正文", f"标题{i}") for i in range(1, n + 1)]

    def test_only_top3_sent(self) -> None:
        prompt = build_context_prompt("问题", self._hits(5), max_hits=3)
        assert "片段3正文" in prompt
        assert "片段4正文" not in prompt
        assert "片段5正文" not in prompt

    def test_no_scaffolding_in_prompt_header(self) -> None:
        """总标题不能写成「【知识片段】」——会被编号计数口径混进来。"""
        prompt = build_context_prompt("问题", self._hits(3))
        assert prompt.count("【知识片段 ") == 3

    def test_context_texts_same_slice(self) -> None:
        hits = self._hits(5)
        assert len(context_texts(hits, max_hits=3)) == 3
        # 与提示词同一批，否则闸②会用模型没看到的片段判「有据」
        assert context_texts(hits, max_hits=3)[2] == "片段3正文"

    def test_system_prompt_has_hard_rules(self) -> None:
        assert "片段" in SYSTEM_PROMPT
        assert "120" in SYSTEM_PROMPT or "字" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# AnswerService 端到端（假检索 + 假模型）
# ---------------------------------------------------------------------------


class _FakeRag:
    """假检索：固定返回给定 decision 与片段。"""

    def __init__(self, decision: str, hits: list[_Hit], top_score: float = 0.9) -> None:
        self._decision = decision
        self._hits = hits
        self._top = top_score

    def retrieve(self, payload):  # noqa: ANN001, ANN201
        from app.schemas.rag import RagRetrieveResponse

        return RagRetrieveResponse(
            hits=[
                {
                    "chunk_id": f"c{i}",
                    "content": h.content,
                    "title": h.title,
                    "score": 0.9,
                    "metadata": {},
                }
                for i, h in enumerate(self._hits)
            ],
            top_score=self._top,
            decision=self._decision,
            thresholds={"direct": 0.72, "fallback": 0.55},
            rewritten_query="q",
            candidates_count=len(self._hits),
            vector_hits=len(self._hits),
            bm25_hits=0,
            degraded=[],
            latency_ms=1,
        )


class TestAnswerService:
    """BE-04 之前的最小闭环：检索 → 闸① → 生成 → 闸②③。"""

    def _svc(self, rag, llm_text="这款支持 7 天无理由退货。"):  # noqa: ANN001
        p = _FakeProvider(text=llm_text)
        gw = LlmGateway(primary=p, fallback=_FakeProvider(), cache=_MemCache())
        return AnswerService(rag=rag, llm=gw, gates_enabled=True), p

    def test_no_evidence_does_not_call_model(self) -> None:
        svc, provider = self._svc(_FakeRag("NO_EVIDENCE", []))
        result = svc.answer("怎么退款", session_id="s1")
        assert provider.calls == 0, "无依据时调用模型就是白花钱 + 有幻觉风险"
        assert result.text == FALLBACK_NO_EVIDENCE

    def test_grounded_answer_passes(self) -> None:
        ctx = "支持 7 天无理由退货。"
        svc, provider = self._svc(
            _FakeRag("DIRECT_ANSWER", [_Hit(ctx)]),
            llm_text="支持 7 天无理由退货。",
        )
        result = svc.answer("怎么退款", session_id="s1")
        assert provider.calls == 1
        assert result.gate.passed
        assert not result.degraded

    def test_fabrication_replaced_by_fallback(self) -> None:
        ctx = "48 小时内发货。"
        svc, _ = self._svc(
            _FakeRag("DIRECT_ANSWER", [_Hit(ctx)]),
            llm_text="我们 24 小时内发货。",
        )
        result = svc.answer("多久发货", session_id="s1")
        assert result.gate.blocked_by == "groundedness"
        assert result.text != "我们 24 小时内发货。"

    def test_gate_result_defaults(self) -> None:
        r = GateResult()
        assert r.passed is True
        assert r.violations == []
        assert r.flags == []
