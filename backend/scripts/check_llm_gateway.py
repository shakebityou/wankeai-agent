"""BE-16 LLM 网关 + AI-03 三道闸自检脚本。

逐条核对任务书 3.5 节 BE-16 的五项 DoD 与 AI-03 的闸逻辑：

| # | DoD | 检查方式 |
| --- | --- | --- |
| 1 | 主备切换成功率 100% | 注入必失败的主通道，断言自动切备且文本非空 |
| 2 | 相同问题缓存命中率 ≥ 15% | 用真实问句池重放两轮，统计第二轮命中率 |
| 3 | RAG 上下文只送 Top-3 | 注入 5 条片段，断言提示词只含 3 条 |
| 4 | 日用量达阈值 80% 告警 | 用小预算计量器跑到 80%，抓 warning 日志 |
| 5 | 不得调用境外模型 API | 断言出口域名白名单拦截 openai.com |

外加三道闸的独立用例（依据/事实/合规各若干）。

用法：

    python scripts/check_llm_gateway.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WK_ENV", "local")

from app.adapters.ports import LlmResult  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services.answer import (  # noqa: E402
    AnswerService,
    build_context_prompt,
    context_texts,
)
from app.services.gates import (  # noqa: E402
    extract_facts,
    gate_compliance,
    gate_evidence,
    gate_groundedness,
    run_gates,
)
from app.services.llm_gateway import (  # noqa: E402
    LlmGateway,
    TokenMeter,
    truncate_reply,
)

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一条断言。"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        FAILURES.append(f"{label}  {detail}")
        print(f"  [FAIL] {label}  {detail}")


class _Hit:
    """最小片段对象（只需 content / title / chunk_id）。"""

    def __init__(self, chunk_id: str, content: str, title: str = "") -> None:
        self.chunk_id = chunk_id
        self.content = content
        self.title = title


class _FakeProvider:
    """可编程的假 Provider。"""

    def __init__(self, *, text: str = "", fail: bool = False, name: str = "fake") -> None:
        self._text = text
        self._fail = fail
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, prompt: str, **kwargs: object) -> LlmResult:
        self.calls += 1
        if self._fail:
            raise ConnectionError("模拟主通道不可用")
        return LlmResult(
            text=self._text,
            prompt_tokens=10,
            completion_tokens=20,
            model="fake-model",
            provider=self._name,
        )

    async def stream(self, prompt: str, **kwargs: object):  # noqa: ANN201
        yield self._text

    async def health(self) -> bool:
        return not self._fail


class _MemCache:
    """最小内存缓存（避免自检依赖 Redis）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.store if k.startswith(prefix)]
        for k in keys:
            self.store.pop(k, None)
        return len(keys)


# ---------------------------------------------------------------------------
# 闸逻辑
# ---------------------------------------------------------------------------


def check_gate_evidence() -> None:
    """闸 ①：依据闸。"""
    print("\n=== 闸① 依据闸 ===")
    r = gate_evidence(decision="NO_EVIDENCE", hits=[_Hit("a", "x")])
    check(not r.passed, "NO_EVIDENCE 必须拦截", f"passed={r.passed}")

    r = gate_evidence(decision="DIRECT_ANSWER", hits=[])
    check(not r.passed, "有决策但片段为空也必须拦截")

    r = gate_evidence(decision="DIRECT_ANSWER", hits=[_Hit("a", "x")])
    check(r.passed, "有决策有片段放行")

    r = gate_evidence(decision="WEAK", hits=[_Hit("a", "x")])
    check(r.passed, "WEAK 有片段可放行（阈值分流已由 BE-05 决定）")


def check_gate_groundedness() -> None:
    """闸 ②：事实一致性闸。"""
    print("\n=== 闸② 事实闸 ===")
    ctx = ["毛绒玩偶下单后 48 小时内发货，支持 7 天无理由退货，满 99 元包邮。"]

    facts = extract_facts("48 小时内发货，7 天无理由，满 99 元包邮")
    kinds = {k for k, _ in facts}
    check("duration" in kinds, "抽出时效事实", str(facts))
    check("money" in kinds, "抽出金额事实", str(facts))

    r = gate_groundedness("我们 48 小时内发货，支持 7 天无理由退货。", ctx)
    check(r.passed, "数字有出处 -> 放行")

    r = gate_groundedness("我们 24 小时内发货。", ctx)
    check(not r.passed, "编造 24 小时 -> 拦截", str(r.violations))

    r = gate_groundedness("我们保证 48 小时内发货。", ctx)
    check(not r.passed, "无出处的「保证」-> 拦截", str(r.violations))

    r = gate_groundedness("满 199 元包邮。", ctx)
    check(not r.passed, "改金额 199 -> 拦截", str(r.violations))

    r = gate_groundedness("支持 7 天无理由退货。", ["7 天"], strict_promises=False)
    check(r.passed, "关闭承诺检查时数字仍比对")

    r = gate_groundedness("   ", ctx)
    check(not r.passed, "空回复拦截")


def check_gate_compliance() -> None:
    """闸 ③：合规闸。"""
    print("\n=== 闸③ 合规闸 ===")
    cases = [
        ("看这里 https://item.taobao.com/x", "scheme http"),
        ("详情见 www.taobao.com", "bare domain"),
        ("点这里跳转购买", "跳转话术"),
    ]
    for text, label in cases:
        r = gate_compliance(text)
        check(not r.passed, f"拦截：{label}", f"violations={r.violations[:2]}")

    # 绝对化用语是 `flag` 级（默认记录+告警，不硬拦），
    # 所以这里断言的是「被识别」，不是「被拦截」。
    r = gate_compliance("绝对是最好的毛绒玩偶")
    check("绝对" in r.flags or "最好的" in r.flags, "识别绝对化用语（flag 级）", str(r.flags[:2]))
    check(r.passed, "flag 级不硬拦（strict=False）")

    r = gate_compliance("毛绒玩偶支持 7 天无理由退货，请联系客服核实。")
    check(r.passed, "正常话术放行")


def check_run_gates() -> None:
    """三道闸串联。"""
    print("\n=== 三道闸串联 ===")
    ctx = ["毛绒玩偶 48 小时内发货。"]
    hits = [_Hit("a", ctx[0])]

    d = run_gates(reply="我们 48 小时内发货。", context=ctx, decision="DIRECT_ANSWER", hits=hits)
    check(d.passed and not d.used_fallback, "全通过 -> 用原回复")

    d = run_gates(reply="我们 12 小时内发货。", context=ctx, decision="DIRECT_ANSWER", hits=hits)
    check(not d.passed and d.blocked_by == "groundedness", "事实闸兜底", d.blocked_by)
    check(d.used_fallback and d.text, "兜底话术非空")

    d = run_gates(reply="随意", context=ctx, decision="NO_EVIDENCE", hits=[])
    check(not d.passed and d.blocked_by == "evidence", "依据闸优先拦截", d.blocked_by)

    d = run_gates(reply="", context=[], decision="DIRECT_ANSWER", hits=hits, evidence_only=True)
    check(d.passed, "预检模式只跑闸①，不因空回复误拦")

    d = run_gates(reply="任意", context=ctx, decision="DIRECT_ANSWER", hits=hits, enabled=False)
    check(d.passed and d.text == "任意", "总开关关闭时直通（仅调试用）")


# ---------------------------------------------------------------------------
# DoD 1 / 5：主备切换 + 域名白名单
# ---------------------------------------------------------------------------


def check_failover() -> None:
    """DoD 1：主备切换成功率 100%。"""
    print("\n=== DoD 1 主备切换 ===")
    primary = _FakeProvider(fail=True, name="primary-down")
    fallback = _FakeProvider(text="备用通道回答", name="fallback-up")
    gw = LlmGateway(primary=primary, fallback=fallback, cache=_MemCache())

    out = gw.complete("测试问题", use_cache=False)
    check(out.text == "备用通道回答", "主通道失败自动切备", f"provider={out.usage.provider}")
    check(out.usage.failover, "用量标注 failover")
    check(not out.usage.degraded, "未降级")

    # 连续失败达阈值后进入冷却，下一轮应直接先走备：
    # 冷却期内 `_call_with_failover` 把备放在第一顺位，主通道**一次都不该被调用**。
    primary.calls = 0
    fallback.calls = 0
    gw2 = LlmGateway(primary=primary, fallback=fallback, cache=_MemCache())
    for _ in range(get_settings().llm_failover_threshold):
        gw2.complete("q", use_cache=False)
    before = primary.calls
    gw2.complete("q", use_cache=False)
    check(
        primary.calls == before,
        "冷却期内不再先试主通道（主通道调用次数不增）",
        f"primary.calls {before}->{primary.calls}",
    )

    # 双通道皆挂 -> 降级
    gw3 = LlmGateway(
        primary=_FakeProvider(fail=True),
        fallback=_FakeProvider(fail=True),
        cache=_MemCache(),
    )
    out = gw3.complete("q", use_cache=False)
    check(out.text == "" and out.usage.degraded, "双通道皆挂 -> 降级空文本")
    check(bool(out.usage.degrade_reason), "降级原因非空", out.usage.degrade_reason[:40])


def check_domain_allowlist() -> None:
    """DoD 5：不得调用境外模型 API。"""
    print("\n=== DoD 5 出口域名白名单 ===")
    from app.adapters.llm_provider import OpenAiCompatProvider

    # 白名单校验是 `OpenAiCompatProvider._assert_allowlisted`（实例方法，
    # 因为它校验的是**本实例配置的 base_url**）。这里直接构实例来验。
    blocked = 0
    for url in (
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1",
        "https://generativelanguage.googleapis.com/v1",
        "https://api.mistral.ai/v1",
    ):
        provider = OpenAiCompatProvider(base_url=url, api_key="k", model="m")
        try:
            provider._assert_allowlisted()
        except PermissionError:
            blocked += 1
    check(blocked == 4, "境外域名全部拦截", f"blocked={blocked}/4")

    allowed = 0
    for url in (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://open.bigmodel.cn/api/paas/v4",
    ):
        provider = OpenAiCompatProvider(base_url=url, api_key="k", model="m")
        try:
            provider._assert_allowlisted()
            allowed += 1
        except PermissionError:
            pass
    check(allowed == 2, "境内白名单域名放行", f"allowed={allowed}/2")


# ---------------------------------------------------------------------------
# DoD 2：缓存命中率
# ---------------------------------------------------------------------------


def check_cache_hit_rate() -> None:
    """DoD 2：相同问题缓存命中率 ≥ 15%。"""
    print("\n=== DoD 2 缓存命中率 ===")
    cache = _MemCache()
    provider = _FakeProvider(text="固定回答")
    gw = LlmGateway(primary=provider, fallback=provider, cache=cache)

    # 真实客服问句池（模拟当天被反复问到的 20 个问题）
    questions = [
        "奶油小熊怎么洗",
        "盲盒可以退换吗",
        "多久发货",
        "毛绒玩偶会掉毛吗",
        "有发票吗",
        "可以指定颜色吗",
        "支持七天无理由吗",
        "运费谁承担",
        "预售款什么时候发",
        "抱枕可以机洗吗",
        "尺寸怎么选",
        "适合几岁小朋友",
        "有 IP 授权吗",
        "包装是礼盒吗",
        "可以改地址吗",
        "为什么还没发货",
        "怎么申请退货",
        "瑕疵品怎么处理",
        "能开发票吗",
        "会员有优惠吗",
    ]
    ctx = ["客服知识片段原文"]
    # 第一轮：全 miss
    for q in questions:
        gw.complete(q, question=q, context_fingerprints=ctx, use_cache=True)
    first_hits = gw.stats["cache_hits"]
    check(first_hits == 0, "首轮全部 miss", f"hits={first_hits}")

    # 第二轮：同问题重放（真实场景中热门问题会被反复问）
    for q in questions:
        gw.complete(q, question=q, context_fingerprints=ctx, use_cache=True)
    second_hits = gw.stats["cache_hits"]
    rate = second_hits / len(questions)
    check(rate == 1.0, "第二轮全部命中（100%）", f"hit_rate={rate:.0%}")
    check(rate >= 0.15, "满足 DoD ≥ 15%", f"{rate:.0%}")

    # 上下文变化必须不命中：同一问题换商品，答案必须重算
    before = gw.stats["cache_hits"]
    gw.complete(
        "奶油小熊怎么洗",
        question="奶油小熊怎么洗",
        context_fingerprints=["完全不同的商品知识片段"],
        use_cache=True,
    )
    check(gw.stats["cache_hits"] == before, "上下文不同 -> 不命中缓存")


# ---------------------------------------------------------------------------
# DoD 3：上下文只送 Top-3
# ---------------------------------------------------------------------------


def check_top3_context() -> None:
    """DoD 3：RAG 上下文只送 Top-3。"""
    print("\n=== DoD 3 上下文只送 Top-3 ===")
    hits = [_Hit(f"c{i}", f"第 {i} 条知识内容") for i in range(1, 6)]
    prompt = build_context_prompt("问题", hits, max_hits=3)
    check(prompt.count("【知识片段") == 3, "提示词只含 3 个片段", f"count={prompt.count('【知识片段')}")
    check("第 4 条知识内容" not in prompt, "第 4 条未送")
    check("第 5 条知识内容" not in prompt, "第 5 条未送")

    texts = context_texts(hits, max_hits=3)
    check(len(texts) == 3, "事实闸比对集合同样只有 3 条", f"len={len(texts)}")
    check(get_settings().llm_context_top_n == 3, "配置项 llm_context_top_n=3")


# ---------------------------------------------------------------------------
# DoD 4：日用量 80% 告警
# ---------------------------------------------------------------------------


def check_budget_alert() -> None:
    """DoD 4：日用量达阈值 80% 告警。"""
    print("\n=== DoD 4 日用量告警 ===")
    records: list[str] = []

    class _Catcher(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append(record.getMessage())

    handler = _Catcher()
    lg = logging.getLogger("app.services.llm_gateway")
    lg.addHandler(handler)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        meter = TokenMeter(daily_budget=1000, session_limit=100000, alert_ratio=0.8)
        meter.record(total_tokens=790)
        check(not any("告警阈值" in r for r in records), "79% 不告警")
        meter.record(total_tokens=20)
        check(any("告警阈值" in r for r in records), "81% 触发告警", records[-1][:50] if records else "")

        # 同一天只告警一次，避免日志刷屏
        n = len([r for r in records if "告警阈值" in r])
        meter.record(total_tokens=50)
        n2 = len([r for r in records if "告警阈值" in r])
        check(n2 == n, "同日只告警一次", f"{n}->{n2}")

        # 预算耗尽必须拒绝
        from app.services.llm_gateway import LlmBudgetExceededError

        meter2 = TokenMeter(daily_budget=100, session_limit=1000, alert_ratio=0.8)
        meter2.record(total_tokens=100)
        try:
            meter2.check(session_id="s", estimated=10)
            check(False, "预算耗尽必须抛异常")
        except LlmBudgetExceededError:
            check(True, "预算耗尽抛 LlmBudgetExceededError")

        # 单会话预算
        meter3 = TokenMeter(daily_budget=10**9, session_limit=100, alert_ratio=0.8)
        meter3.record(total_tokens=95, session_id="sX")
        try:
            meter3.check(session_id="sX", estimated=10)
            check(False, "单会话超限必须抛异常")
        except LlmBudgetExceededError:
            check(True, "单会话预算独立生效")
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


# ---------------------------------------------------------------------------
# 出口字数
# ---------------------------------------------------------------------------


def check_truncate() -> None:
    """出口字数 ≤ 120 字且按句截断。"""
    print("\n=== 出口字数约束 ===")
    long_text = "第一句说明。" * 30
    out = truncate_reply(long_text, 120)
    check(len(out) <= 120, "截断后 ≤ 120 字", f"len={len(out)}")
    check(out.endswith("。"), "按句末截断而非硬切", out[-6:])

    short = "很短的回答。"
    check(truncate_reply(short, 120) == short, "短文本不截断")

    no_punct = "字" * 200
    out2 = truncate_reply(no_punct, 120)
    check(len(out2) <= 121, "无标点时硬切并加省略号", f"len={len(out2)}")


# ---------------------------------------------------------------------------
# 端到端：真实 RAG + 三道闸
# ---------------------------------------------------------------------------


def check_end_to_end() -> None:
    """端到端：真实商家知识 → 检索 → 三道闸 → 回答。"""
    print("\n=== 端到端（真实商家材料）===")
    svc = AnswerService()

    r = svc.answer("奶油小熊怎么洗", session_id="e2e", channel="TAOBAO")
    check(r.decision in ("DIRECT_ANSWER", "WEAK"), "相关问句检索到依据", r.decision)
    check(r.gate.passed, "通过三道闸", r.gate.blocked_by or "-")
    check(len(r.text) <= 120, "回答 ≤ 120 字", f"len={len(r.text)}")
    check("[知识片段" not in r.text, "回复不含提示词脚手架")
    check("相：" not in r.text, "回复不含相似问标签")
    check(r.usage is not None and not r.usage.degraded, "非降级")

    # 无依据必须拒答且不调模型
    from app.services.llm_gateway import get_llm_gateway

    gw = get_llm_gateway()
    before = gw.stats["calls"]
    r2 = svc.answer("请用文言文写一首关于台风的诗", session_id="e2e2")
    after = gw.stats["calls"]
    check(r2.decision == "NO_EVIDENCE", "无关问题判 NO_EVIDENCE", r2.decision)
    check(r2.gate.blocked_by == "evidence", "依据闸拦截", r2.gate.blocked_by)
    check(after == before, "无依据时不调模型（省成本、零幻觉）", f"calls {before}->{after}")
    check("http" not in r2.text and "链接" not in r2.text, "兜底话术无外链")


def main() -> int:
    """跑全部检查。"""
    print("=" * 60)
    print("BE-16 LLM 网关 + AI-03 三道闸 自检")
    print("=" * 60)

    check_gate_evidence()
    check_gate_groundedness()
    check_gate_compliance()
    check_run_gates()
    check_failover()
    check_domain_allowlist()
    check_cache_hit_rate()
    check_top3_context()
    check_budget_alert()
    check_truncate()
    check_end_to_end()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"FAILED {FAILED} / PASSED {PASSED}")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"ALL PASSED ({PASSED} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
