"""生成阶段三道闸（AI-03）——**防幻觉的最后防线**。

三道闸的职责边界必须分清，否则会出现两种典型事故：

    * 把「有没有依据」和「说不说人话」混在一起判定 → 有依据的正常回答
      被误拦，转人工率飙升；
    * 只做合规不做事实一致性 → 回答读起来顺、链接也没有，但里面的
      「3 天到货」「支持 30 天退换」全是编的，比明显报错更危险。

因此按**从硬到软**排三道，任何一道不放行就整条回答改用兜底话术：

| 闸 | 名称 | 判什么 | 不通过的处置 |
| --- | --- | --- | --- |
| ① | 依据闸（Evidence Gate） | 有没有可用的知识片段（`decision != NO_EVIDENCE` 且片段非空） | 直接兜底，**不调模型**（省钱且不可能幻觉） |
| ② | 事实闸（Groundedness Gate） | 回复里的每个**断言性数字/承诺**能否在上下文中找到出处 | 兜底或改写 |
| ③ | 合规闸（Compliance Gate） | 外链、绝对化用语、承诺改价、索要隐私、敏感信息 | 兜底（`OutboundGate` 已实现） |

**闸 ② 的判定思路（为什么不用 NLI 模型）**：引入一个 Cross-Encoder 做
NLI 会带来三个问题——多一次模型调用（P95 预算只有 2s）、判定不可解释
（运营问「为什么这条被拦」答不上来）、阈值难调（不同品类不同粒度）。
本实现走**可解释的规则比对**：抽出回复中的「事实单元」（数字+单位、
时效承诺、金额、比例），逐个回查上下文；只要有一个找不到出处就判不通过。
代价是可能漏掉「没有数字但语义相反」的幻觉（如把「不能机洗」说成
「可以机洗」），这一点由闸 ① 的「只送 Top-3 原文片段 + 提示词约束
只用片段原话」和 `TemplateProvider` 的抽取式生成共同兜住。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.compliance.outbound_gate import get_outbound_gate, scan_outbound
from app.core.logging import get_logger
from app.core.enums import RagDecision

logger = get_logger(__name__)

#: 兜底话术。**纯文字、无链接、不承诺时间**（HC-1 + 任务书 2.4.2）。
#: 依据商家《售后政策.md》与《物流发货SOP.md》的对外口径：
#: 查不到就说查不到，转人工处理，不臆测结果。
FALLBACK_NO_EVIDENCE = (
    "抱歉，这个我暂时查不到准确信息。我帮您转接人工客服，由同事为您核实，可以吗？"
)

#: 闸 ② 不通过时的兜底（比 NO_EVIDENCE 更保守：连模型输出都不用）
FALLBACK_NOT_GROUNDED = (
    "这个问题我需要确认一下细节，已为您转接人工客服，稍后由同事为您解答～"
)

#: 事实单元抽取：数字 + 常见单位。
#: 抽这些是因为**编造数字是成本最低、危害最大的幻觉**——
#: 「48 小时发货」写成「24 小时」，用户据此催单，客服无法自圆其说。
_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 时效：N 天 / N 个工作日 / N 小时 / N 分钟
    ("duration", re.compile(r"(\d+)\s*(?:个)?\s*(?:工作日|自然日|天|小时|分钟|秒)")),
    # 金额：N 元 / ￥N / ¥N
    ("money", re.compile(r"(?:[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*元)")),
    # 比例：N%
    ("percent", re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")),
    # 数量：N 件 / 只 / 个 / 盒 / 套 / 张 / 份
    ("quantity", re.compile(r"(\d+)\s*(?:件|只|个|盒|套|张|份|片|瓶|袋)")),
    # 次数：N 次 / N 轮
    ("times", re.compile(r"(\d+)\s*(?:次|轮|遍)")),
    # 天数区间：7 天内 / 15 日内
    ("window", re.compile(r"(\d+)\s*(?:天|日)\s*(?:内|以内|之内)")),
)

#: 承诺性动词（出现时要求上下文有对应依据，避免「我保证给您改价」这类话）。
_PROMISE_VERBS: tuple[str, ...] = (
    "保证",
    "承诺",
    "一定",
    "包退",
    "包换",
    "包赔",
    "赔付",
    "免运费",
    "包邮",
    "补发",
)

#: 转折/否定词。抽取数字做比对时用于**标注语境**，
#: 帮助人工复盘「上下文说的是『不支持 7 天无理由』，回复说成『支持 7 天』」。
_NEGATIONS: tuple[str, ...] = ("不", "无", "未", "非", "禁", "免")


@dataclass
class GateResult:
    """单道闸的判定结果。

    Attributes:
        passed: 是否放行。
        gate: 闸标识（`evidence` / `groundedness` / `compliance`）。
        reason: 不通过原因（可解释，直接写进日志与埋点）。
        violations: 具体命中的项（如未找到出处的数字列表）。
        flags: **未拦截但需告警**的项（绝对化用语、承诺性表述）。
            `OutboundGate` 把这些设计成 flag 级——拦掉会让正常话术
            大面积误伤（「绝对」在「绝对没问题」里是亲切语气），
            但运营需要知道模型在往这个方向漂，所以必须保留下来。
        rewritten: 若闸做了改写（脱敏/替换），这里给出改写后文本。
    """

    passed: bool = True
    gate: str = ""
    reason: str = ""
    violations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rewritten: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可日志字典。"""
        return {
            "gate": self.gate,
            "passed": self.passed,
            "reason": self.reason,
            "violations": list(self.violations),
            "flags": list(self.flags),
        }


@dataclass
class GateDecision:
    """三道闸的合并判定。

    Attributes:
        text: 最终出站文本（通过闸时是模型输出或改写后文本；不通过时是兜底话术）。
        passed: 三道闸是否全部放行。
        blocked_by: 拦下它的闸标识；全通过为空串。
        gates: 每道闸的明细（审计与排障用）。
        used_fallback: 是否使用了兜底话术。
    """

    text: str = ""
    passed: bool = True
    blocked_by: str = ""
    gates: list[GateResult] = field(default_factory=list)
    used_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        """导出为可日志字典（**不含回复原文**）。"""
        return {
            "passed": self.passed,
            "blocked_by": self.blocked_by,
            "used_fallback": self.used_fallback,
            "gates": [g.to_dict() for g in self.gates],
            "text_length": len(self.text),
        }


def gate_evidence(
    *,
    decision: str | RagDecision,
    hits: Sequence[Any] = (),
) -> GateResult:
    """闸 ①：依据闸。

    `NO_EVIDENCE` 时**必须**不通过——这是 BE-05 DoD 第 3 项的落地：
    `top_score < 0.55` 时不得用低分片段生成回答。

    Args:
        decision: 检索分流决策。
        hits: 送进上下文的片段（至少 1 条才算有依据）。

    Returns:
        `GateResult`。
    """
    value = decision.value if isinstance(decision, RagDecision) else str(decision)
    if value == RagDecision.NO_EVIDENCE.value:
        return GateResult(
            passed=False,
            gate="evidence",
            reason="检索判定 NO_EVIDENCE，无可用依据",
        )
    if not hits:
        return GateResult(
            passed=False,
            gate="evidence",
            reason=f"decision={value} 但上下文片段为空",
        )
    return GateResult(passed=True, gate="evidence")


def _norm(text: str) -> str:
    """归一化：去空白、全角数字转半角、统一百分号与货币符号。"""
    text = text.replace("％", "%").replace("￥", "¥").replace("\u3000", " ")
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19:  # 全角数字
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def extract_facts(text: str) -> list[tuple[str, str]]:
    """抽取事实单元。

    Args:
        text: 待分析文本。

    Returns:
        [(类别, 命中片段)]，按出现顺序、去重。
    """
    normalized = _norm(text)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in _FACT_PATTERNS:
        for match in pattern.finditer(normalized):
            snippet = match.group(0).strip()
            key = f"{kind}:{snippet}"
            if key in seen:
                continue
            seen.add(key)
            found.append((kind, snippet))
    return found


def _number_of(snippet: str) -> str | None:
    """从事实片段里取数字部分。"""
    match = re.search(r"\d+(?:\.\d+)?", snippet)
    return match.group(0) if match else None


def gate_groundedness(
    reply: str,
    context: Sequence[str],
    *,
    strict_promises: bool = True,
) -> GateResult:
    """闸 ②：事实一致性闸。

    **判定规则**（任一命中即不通过）：
    1. 回复中的每个「数字 + 单位」事实单元，其**数字**必须在上下文中出现过；
    2. `strict_promises=True` 时，承诺性动词必须在上下文中出现过。

    规则 1 只比对数字而比对完整片段，是因为上下文与回复的措辞必然不同
    （「48 小时内发货」vs「我们 48 小时发货哦」），要求片段逐字相同会
    把正常回答全部拦掉。数字才是幻觉的高危载体。

    Args:
        reply: 模型回复。
        context: 送进模型的片段原文列表。
        strict_promises: 是否启用承诺性动词检查。

    Returns:
        `GateResult`。
    """
    reply = reply or ""
    if not reply.strip():
        return GateResult(passed=False, gate="groundedness", reason="回复为空")

    corpus = _norm("\n".join(str(c) for c in context))
    corpus_numbers = set(re.findall(r"\d+(?:\.\d+)?", corpus))

    violations: list[str] = []
    for kind, snippet in extract_facts(reply):
        number = _number_of(snippet)
        if number is None:
            continue
        if number not in corpus_numbers:
            violations.append(f"{kind}:{snippet}")

    if strict_promises:
        for verb in _PROMISE_VERBS:
            if verb in reply and verb not in corpus:
                violations.append(f"promise:{verb}")

    if violations:
        return GateResult(
            passed=False,
            gate="groundedness",
            reason="回复中的事实在上下文中找不到出处",
            violations=violations,
        )
    return GateResult(passed=True, gate="groundedness")


def gate_compliance(text: str) -> GateResult:
    """闸 ③：合规闸（复用 `OutboundGate`）。

    命中外链/跳转话术/绝对化用语/承诺改价/索要隐私时**替换为兜底话术**，
    而不是简单删除违规片段——删一半的句子会变成病句，反而让用户困惑。

    Args:
        text: 待出站文本。

    Returns:
        `GateResult`（`rewritten` 为最终可发文本）。
    """
    scan = scan_outbound(text)
    if scan.blocked or scan.violations:
        return GateResult(
            passed=False,
            gate="compliance",
            reason="出站合规闸拦截",
            violations=list(scan.violations),
            rewritten=scan.text or FALLBACK_NO_EVIDENCE,
        )
    # 未拦截但被脱敏/被标记：放行，用处理后的文本。
    # `flags` 必须透传出去，否则绝对化用语会静默消失在日志里。
    return GateResult(
        passed=True,
        gate="compliance",
        rewritten=scan.text,
        flags=list(scan.flags),
    )


def run_gates(
    *,
    reply: str,
    context: Sequence[str],
    decision: str | RagDecision,
    hits: Sequence[Any] = (),
    fallback_text: str = FALLBACK_NO_EVIDENCE,
    strict_promises: bool = True,
    enabled: bool = True,
    evidence_only: bool = False,
) -> GateDecision:
    """依次执行三道闸。

    顺序**不可调换**：依据闸在最前，因为它能省掉闸 ②③ 的无效计算；
    事实闸在合规闸前，因为「编造的数字」比「语气不合规」更该优先被发现。

    Args:
        reply: 模型回复。
        context: 送进模型的片段原文。
        decision: 检索分流决策。
        hits: 片段对象（仅用其数量判空）。
        fallback_text: 兜底话术。
        strict_promises: 是否启用承诺性动词检查。
        enabled: 总开关。**生产必须 True**；关闭仅用于本地调试对照。
        evidence_only: 只跑闸 ①。生成前的**预检**用——此时还没有回复，
            若照常跑闸 ② 会因为「回复为空」判失败，把有依据的好问句
            误判成无依据。这个参数就是为了避免那次误判。

    Returns:
        `GateDecision`。
    """
    if not enabled:
        return GateDecision(text=reply, passed=True, gates=[])

    gates: list[GateResult] = []

    # 闸 ① 依据闸
    g1 = gate_evidence(decision=decision, hits=hits)
    gates.append(g1)
    if not g1.passed:
        logger.info("闸①依据闸拦截：%s", g1.reason)
        return _fallback(fallback_text, g1.gate, gates)
    if evidence_only:
        # 预检：只关心「有没有依据」，回复还没生成，不判事实与合规
        return GateDecision(text="", passed=True, gates=gates)

    # 闸 ② 事实闸
    g2 = gate_groundedness(reply, context, strict_promises=strict_promises)
    gates.append(g2)
    if not g2.passed:
        logger.warning(
            "闸②事实闸拦截：%s，命中=%s",
            g2.reason,
            g2.violations[:5],
        )
        return _fallback(FALLBACK_NOT_GROUNDED, g2.gate, gates)

    # 闸 ③ 合规闸
    g3 = gate_compliance(reply)
    gates.append(g3)
    if not g3.passed:
        logger.warning("闸③合规闸拦截：%s", g3.violations[:5])
        return _fallback(g3.rewritten or fallback_text, g3.gate, gates)

    return GateDecision(
        text=g3.rewritten or reply,
        passed=True,
        blocked_by="",
        gates=gates,
    )


def _fallback(text: str, blocked_by: str, gates: list[GateResult]) -> GateDecision:
    """构造兜底判定。"""
    return GateDecision(
        text=text,
        passed=False,
        blocked_by=blocked_by,
        gates=gates,
        used_fallback=True,
    )
