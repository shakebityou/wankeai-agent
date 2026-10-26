"""答案生成服务（BE-16 + AI-03 的组装层）。

把三块能力串成**一条可审计的回答流水线**：

    归一化问题 → RAG 六步检索 → 闸①依据闸 → 只送 Top-3 → LLM 网关
    → 闸②事实闸 → 闸③合规闸 → 出站文本

**为什么要有这一层而不是让编排层自己串**：这条链上有 5 个必须固化的
纪律点（只送 Top-3、NO_EVIDENCE 不调模型、片段原文同时用于闸②、
答案缓存键含上下文指纹、兜底回复不进缓存），散落在编排代码里迟早被
某次重构改坏，而每一条的失效都不会报错、只会**静默变差**。

与 `app.services.orchestrator` 的分工：编排层决定「该不该答」
（动作决策），本模块决定「答什么、答得准不准」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.enums import RagDecision
from app.core.logging import get_logger
from app.schemas.rag import RagRetrieveRequest, RagRetrieveResponse
from app.services.gates import (
    FALLBACK_NO_EVIDENCE,
    GateDecision,
    run_gates,
)
from app.services.llm_gateway import (
    LlmAnswer,
    LlmBudgetExceededError,
    LlmGateway,
    get_llm_gateway,
)
from app.services.rag.service import RagService, get_rag_service

logger = get_logger(__name__)

#: 提示词版本（改提示词必须升版本，否则缓存会复用旧提示词的答案）
SYSTEM_PROMPT = """你是「玩可爱」品牌的在线客服助手。严格遵守以下规则：

1. 只能依据下面提供的【知识片段】回答，片段里没有的信息一律不要编造。
2. 涉及价格、库存、订单、物流状态等**会变化的信息**，不得直接回答，
   应引导用户由人工核实或走查询（这些数据必须来自系统接口）。
3. 不得发送任何链接、二维码、店铺/商品跳转卡片，也不要说「点这里」「点击链接」。
4. 不得索要身份证、银行卡、验证码，不得引导加微信/QQ 等站外联系方式。
5. 回复不超过 120 字，口语化、简洁、有礼貌；不确定时直接说明并建议转人工。
6. 不得使用「最」「第一」「绝对」等绝对化用语，不得自行承诺折扣、改价或赔付。
"""


@dataclass
class AnswerResult:
    """一次回答的完整结果（编排层与审计都用它）。

    Attributes:
        text: 最终出站文本。
        decision: RAG 分流决策。
        top_score: 检索最高分。
        gate: 三道闸判定明细。
        usage: LLM 用量与来源。
        hits: 命中的片段 ID（审计）。
        latency_ms: 本模块内部耗时。
        degraded: 是否降级（无依据 / 预算耗尽 / 双通道失败）。
        degrade_reason: 降级原因。
    """

    text: str = ""
    decision: str = ""
    top_score: float = 0.0
    gate: GateDecision = field(default_factory=GateDecision)
    usage: Any = None
    hits: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    degraded: bool = False
    degrade_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可日志字典（不含回复原文）。"""
        return {
            "decision": self.decision,
            "top_score": round(self.top_score, 4),
            "hits": list(self.hits),
            "gate": self.gate.to_dict(),
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "latency_ms": round(self.latency_ms, 2),
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "text_length": len(self.text),
        }


def build_context_prompt(question: str, hits: list[Any], *, max_hits: int = 3) -> str:
    """拼装送模型的用户提示词。

    **上下文只送 Top-3**（BE-16 DoD）：片段越多，模型越容易把不同条款
    串在一起编答案，同时输入 token 线性上涨。

    Args:
        question: 用户问题（已归一化）。
        hits: 检索命中（按分数降序）。
        max_hits: 最多送几条。

    Returns:
        提示词文本。
    """
    # 总标题刻意不用「【知识片段】」：它会被 `【知识片段 \d+】` 的编号
    # 计数口径混进来，也让模型误以为这是第 0 条片段。
    lines: list[str] = ["【参考资料】"]
    for idx, hit in enumerate(hits[:max_hits], start=1):
        content = getattr(hit, "content", "") or ""
        title = getattr(hit, "title", "") or ""
        lines.append(f"【知识片段 {idx}】{title}")
        lines.append(content.strip())
        lines.append("")
    lines.append(f"【用户问题】{question.strip()}")
    return "\n".join(lines)


def context_texts(hits: list[Any], *, max_hits: int = 3) -> list[str]:
    """取出送模型的片段原文（同时用于闸 ② 的事实比对）。

    **必须与 `build_context_prompt` 用同一个切片**：若闸 ② 拿全量片段去
    比对，会把「模型没看到、但库里存在」的数字判为有据，闸就形同虚设。

    Args:
        hits: 检索命中。
        max_hits: 与提示词一致的条数。

    Returns:
        片段原文列表。
    """
    return [str(getattr(hit, "content", "") or "") for hit in hits[:max_hits]]


class AnswerService:
    """答案生成服务。

    Attributes:
        rag: 检索服务。
        llm: LLM 网关。
        gates_enabled: 三道闸总开关（**生产必须 True**）。
    """

    def __init__(
        self,
        *,
        rag: RagService | None = None,
        llm: LlmGateway | None = None,
        gates_enabled: bool | None = None,
    ) -> None:
        self._rag = rag
        self._llm = llm
        if gates_enabled is None:
            gates_enabled = get_settings().compliance_gate_enabled
        self.gates_enabled = gates_enabled

    @property
    def rag(self) -> RagService:
        """检索服务（惰性）。"""
        if self._rag is None:
            self._rag = get_rag_service()
        return self._rag

    @property
    def llm(self) -> LlmGateway:
        """LLM 网关（惰性）。"""
        if self._llm is None:
            self._llm = get_llm_gateway()
        return self._llm

    def answer(
        self,
        question: str,
        *,
        session_id: str = "",
        channel: str = "",
        filters: dict[str, Any] | None = None,
        fallback_text: str = FALLBACK_NO_EVIDENCE,
    ) -> AnswerResult:
        """回答一个问题（RAG → 三道闸 → LLM → 三道闸）。

        Args:
            question: 用户问题。
            session_id: 会话 ID（单会话 token 预算用）。
            channel: 渠道（缓存键与话术口径）。
            filters: 检索元数据过滤（如按 SKU 限定）。
            fallback_text: 无依据时的兜底话术。

        Returns:
            `AnswerResult`。
        """
        import time

        started = time.perf_counter()
        payload = RagRetrieveRequest(
            query=question,
            # `filters` 不接受 None（Pydantic 会校验失败），未指定时干脆不传。
            **({"filters": filters} if filters else {}),
        )
        retrieval: RagRetrieveResponse = self.rag.retrieve(payload)
        hit_objs = list(retrieval.hits)
        top_score = float(retrieval.top_score or 0.0)

        base = AnswerResult(
            decision=retrieval.decision.value
            if isinstance(retrieval.decision, RagDecision)
            else str(retrieval.decision),
            top_score=top_score,
            hits=[getattr(h, "chunk_id", "") for h in hit_objs],
        )

        # 闸 ①：无依据直接兜底，**不调模型**（省钱且不可能幻觉）
        pre_gate = run_gates(
            reply="",
            context=[],
            decision=retrieval.decision,
            hits=hit_objs,
            fallback_text=fallback_text,
            enabled=self.gates_enabled,
            # 只跑闸 ①：此刻回复尚未生成，跑闸 ② 会因「回复为空」误拦截
            evidence_only=True,
        )
        if not pre_gate.passed:
            base.text = pre_gate.text
            base.gate = pre_gate
            base.degraded = True
            base.degrade_reason = "NO_EVIDENCE"
            base.latency_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "生成阶段短路：decision=%s top_score=%.3f，不调模型",
                base.decision,
                top_score,
            )
            return base

        # 只送 Top-3（与闸 ② 的比对集合同源）
        max_hits = get_settings().llm_context_top_n
        contexts = context_texts(hit_objs, max_hits=max_hits)
        prompt = build_context_prompt(question, hit_objs, max_hits=max_hits)

        try:
            answer: LlmAnswer = self.llm.complete(
                prompt,
                system=SYSTEM_PROMPT,
                session_id=session_id,
                question=question,
                context_fingerprints=contexts,
                channel=channel,
                use_cache=True,
            )
        except LlmBudgetExceededError as exc:
            # 预算耗尽不是故障：降级到兜底，并提示转人工
            logger.warning("LLM 预算耗尽，降级兜底：%s", exc)
            base.text = fallback_text
            base.gate = pre_gate
            base.degraded = True
            base.degrade_reason = "BUDGET_EXCEEDED"
            base.latency_ms = (time.perf_counter() - started) * 1000
            return base

        base.usage = answer.usage
        if answer.usage.degraded:
            base.text = fallback_text
            base.gate = pre_gate
            base.degraded = True
            base.degrade_reason = answer.usage.degrade_reason or "LLM_DEGRADED"
            base.latency_ms = (time.perf_counter() - started) * 1000
            return base

        # 闸 ②③：用**同一份** contexts 校验事实一致性
        final_gate = run_gates(
            reply=answer.text,
            context=contexts,
            decision=retrieval.decision,
            hits=hit_objs,
            fallback_text=fallback_text,
            enabled=self.gates_enabled,
        )
        base.text = final_gate.text
        base.gate = final_gate
        base.latency_ms = (time.perf_counter() - started) * 1000
        if not final_gate.passed:
            logger.warning(
                "最终闸拦截 gate=%s reason=%s",
                final_gate.blocked_by,
                final_gate.gates[-1].reason if final_gate.gates else "",
            )
        return base


_SERVICE: AnswerService | None = None


def get_answer_service() -> AnswerService:
    """答案服务单例。"""
    global _SERVICE  # noqa: PLW0603
    if _SERVICE is None:
        _SERVICE = AnswerService()
    return _SERVICE


def set_answer_service(service: AnswerService | None) -> None:
    """替换单例（测试注入）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = service
