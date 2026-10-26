"""BE-04 对话编排服务（决策中枢）。

职责边界
--------
编排层**只做决策与调度，不做具体业务**：

* 状态由 `app.services.session` 管
* 检索由 `app.services.rag` 管
* 生成由 `app.services.answer` 管
* 业务查询由 `app.services.tools` 管（BE-07/08/09）
* 转人工判定由 `app.services.handoff` 管（BE-13/14/15）

编排层的价值在于**把这几件事按正确顺序、在正确条件下、用正确的预算串起来**。

动作决策三选一
--------------
每轮只做一次决策，但决策依据同时看三条线索（见 `decide_action`）：

1. **转人工优先**：BE-13 的 6 条规则任一命中 → `HANDOFF`。
   这一条**必须最先判**，否则「我要投诉」会先被 RAG 答一遍再转人工，
   等于让机器人先激怒用户一轮。
2. **该查**：意图落在订单/物流/库存这类「会变的数字」上 → `TOOL_QUERY`。
   HC-3 要求会变的数字永远走接口，绝不能让 RAG 编。
3. **该答**：其余走 `RAG_ANSWER`。

并行与降级
----------
「检索」与「工具调用」在语义上独立（检索回答「政策怎么规定」，
工具回答「这单现在什么状态」），所以**同时发起**。任一路超时/异常
只降级自己那一路，不影响另一路，也不让整轮失败——这是 DoD ⑤
「单步超时有独立降级不整体失败」的落点。

耗时预算
--------
DoD ⑤ 要求编排层**自身**耗时 P95 ≤ 150ms。这里的「自身」不含 RAG、
LLM、商家接口这些外部依赖的耗时，只算编排逻辑（决策、状态读写、
DAG 调度）。所以 `latency_ms` 里要分开记账，`self` 一项是编排层自己的。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.enums import (
    ActionType,
    Channel,
    Emotion,
    HandoffReason,
    InboundMsgType,
    IntentCategory,
    RagDecision,
    RiskLevel,
    Sender,
    ToolName,
)
from app.core.logging import get_logger
from app.core.trace import get_trace_id
from app.services.intent import IntentResult, get_emotion_detector, get_intent_classifier
from app.services.session import (
    ResolveResult,
    SessionState,
    SessionStore,
    extract_entities,
    get_session_store,
    is_negative,
    resolve_reference,
    should_force_handoff,
    update_state_after_turn,
)

logger = get_logger(__name__)

#: 各步超时预算（毫秒）。加起来要小于商家回调的 3 秒窗口。
TOOL_TIMEOUT_MS = 1500
RETRIEVAL_TIMEOUT_MS = 1200
LLM_TIMEOUT_MS = 6000

#: 走工具查询的意图（都是「会变的数字」，HC-3 要求必须查接口）
_TOOL_INTENTS: dict[IntentCategory, tuple[ToolName, ...]] = {
    IntentCategory.ORDER_SERVICE: (
        ToolName.ORDER_QUERY,
        ToolName.LOGISTICS_QUERY,
        ToolName.FAQ_SHIPPING_SLA,
    ),
    IntentCategory.AFTER_SALE: (
        ToolName.ORDER_QUERY,
        ToolName.LOGISTICS_QUERY,
        ToolName.FAQ_SHIPPING_SLA,
    ),
}

#: 明确需要「查这单」的信号词。命中才走工具——
#: 用户只是问「你们怎么退款」是政策咨询，不该去拉他的订单。
_QUERY_HINT_WORDS: tuple[str, ...] = (
    "订单号", "我的订单", "我这单", "这个单", "运单", "物流", "快递",
    "发货了吗", "到哪了", "签收", "退款到", "到账", "进度", "查一下",
)


@dataclass
class ActionDecision:
    """动作决策结果。

    Attributes:
        action: 三选一。
        reason: 决策理由（进日志与坐席上下文卡片，**可解释性必需**）。
        tools: `TOOL_QUERY` 时要用到的工具。
        handoff_reason: `HANDOFF` 时的原因枚举。
        risk: 风险等级。
        rule_id: 命中 `handoff_rules` 的规则号（1~6）；非转人工为 None。
        rule_name: 命中规则名（写进工单事件，复盘用）。
    """

    action: ActionType
    reason: str
    tools: list[ToolName] = field(default_factory=list)
    handoff_reason: HandoffReason | None = None
    risk: RiskLevel = RiskLevel.LOW
    rule_id: int | None = None
    rule_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为字典。"""
        return {
            "action": self.action.value,
            "reason": self.reason,
            "tools": [t.value for t in self.tools],
            "handoff_reason": self.handoff_reason.value if self.handoff_reason else None,
            "risk": self.risk.value,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
        }


@dataclass
class ToolOutcome:
    """一路工具调用的结果。"""

    tool: ToolName
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    error: str = ""
    latency_ms: float = 0.0
    degraded: bool = False


@dataclass
class TurnResult:
    """一轮对话的完整结果（对齐契约 2.4.1 的五段结构）。

    Attributes:
        reply_content: 出站文本。
        reply_msg_type: 出站消息类型（本契约只发 TEXT/IMAGE）。
        actions: 本轮做过的动作（工具名列表，与契约 `actions` 同构）。
        rag_hits: 检索命中（只留 chunk_id/score/category，不返回全文）。
        handoff: 转人工信息；未转人工为 `None`。
        latency_ms: 分步耗时。
        intent / emotion / resolve: 决策依据（排障与坐席上下文卡片用）。
        degraded: 降级标识列表。
        trace_id: 贯穿本轮的链路 ID。
    """

    reply_content: str = ""
    reply_msg_type: str = "TEXT"
    actions: list[str] = field(default_factory=list)
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    handoff: dict[str, Any] | None = None
    latency_ms: dict[str, int] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)
    emotion: dict[str, Any] = field(default_factory=dict)
    resolve: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    trace_id: str = ""

    def to_contract(self) -> dict[str, Any]:
        """输出为契约 2.4.1 要求的五段结构。"""
        return {
            "reply": {
                "msg_type": self.reply_msg_type,
                "content": self.reply_content,
                "cards": [],
            },
            "actions": list(self.actions),
            "rag_hits": list(self.rag_hits),
            "handoff": self.handoff,
            "latency_ms": dict(self.latency_ms),
            # —— 以下为排障/坐席卡片附加字段，契约未禁止扩展 ——
            "intent": self.intent,
            "emotion": self.emotion,
            "resolve": self.resolve,
            "decision": self.decision,
            "degraded": list(self.degraded),
            "trace_id": self.trace_id,
        }


# ---------------------------------------------------------------------------
# 动作决策
# ---------------------------------------------------------------------------


#: 显式转人工的文本信号（固定短语）。
_EXPLICIT_HANDOFF_PATTERNS: tuple[str, ...] = (
    "转人工",
    "人工客服",
    "真人客服",
    "找人工",
    "要真人",
    "真人吗",
    "真人呢",
    "活人",
    "叫你们经理",
    "叫经理",
    "把经理叫来",
)

#: 显式转人工的**正则**信号。
#:
#: 为什么不能只用固定短语：动宾搭配是无穷的——「找个真人」「找个人来」
#: 「给我转个人」「要人工」「换人工」用户都会说。穷举字面串必然漏，
#: 而漏触发显式请求是 P0（用户明确要人工却没转，直接引发投诉）。
#: 所以按「找/要/转/换 + 个/位/人工人」的词法结构匹配，而不是穷举句子。
_EXPLICIT_HANDOFF_RE = re.compile(
    r"(?:找|要|转|换|叫|来)\s*(?:个|一位|位)?\s*(?:真人|人工|活人|人)"
    r"|(?:真人|人工)\s*(?:客服)?\s*(?:在吗|在么|呢)"
)

#: 高风险意图的正则信号（投诉/维权/法律/平台介入/媒体曝光）。
_HIGH_RISK_RE = re.compile(r"12315|12345|消协|市场监管|工商(?:局)?|打官司|起诉|律师函")

#: 高风险意图的文本信号（投诉/维权/法律/平台介入/媒体曝光）。
#: 同样在决策层兜一道：这些词一旦出现，机器人不该继续答，
#: 因为继续答等于替商家处理一起潜在纠纷。
_HIGH_RISK_PATTERNS: tuple[str, ...] = (
    "投诉", "维权", "12315", "消协", "消保委", "举报", "起诉", "法庭", "曝光",
    "媒体", "工商", "市场监管局", "欺诈", "骗子", "法律程序",
)


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    """文本是否命中任一固定短语。"""
    return any(p in text for p in patterns)


def _is_explicit_handoff(text: str) -> bool:
    """是否是显式转人工请求（短语 + 正则双判）。"""
    return _has_any(text, _EXPLICIT_HANDOFF_PATTERNS) or bool(
        _EXPLICIT_HANDOFF_RE.search(text)
    )


def _is_high_risk(text: str) -> bool:
    """是否是高风险诉求（短语 + 正则双判）。"""
    return _has_any(text, _HIGH_RISK_PATTERNS) or bool(_HIGH_RISK_RE.search(text))


def _rule_meta(rule_id: int) -> tuple[int, str]:
    """取规则的 `(rule_id, name)`，用于把「按哪条规则转的」写进决策与工单事件。

    惰性 import 避免 `app.services.handoff.__init__` → `service` →
    `orchestrator` 的循环导入；取不到名字时只回 rule_id，不影响判定。
    """
    try:
        from app.services.handoff.rules import DEFAULT_RULES

        for rule in DEFAULT_RULES:
            if rule.rule_id == rule_id:
                return rule_id, rule.name
    except Exception:  # noqa: BLE001 - 名称缺失不影响决策
        pass
    return rule_id, ""


def decide_action(
    *,
    text: str,
    intent: IntentResult,
    state: SessionState,
    emotion_negative: bool = False,
    retrieval_decision: str | None = None,
) -> ActionDecision:
    """动作决策三选一。

    **判定顺序即优先级**：转人工 → 工具 → RAG。

    六条转人工规则的**配置与仲裁**在 `app/services/handoff/rules.py`
    （运营可在后台改阈值/词表，见 `PATCH /internal/v1/handoff/rules/{id}`）；
    本函数保留同序的**快路径**：这里是每轮必走的代码，纯内存判定、
    微秒级，且顺序已被 500+ 用例钉住。两处对**核心用例集**必须给出
    相同结论，`tests/test_handoff_rules.py::test_engine_agrees_with_orchestrator`
    逐例比对；引擎另有两个编排层未启用的信号（连续兜底计数、辱骂词表），
    由后台规则 API 与 `HandoffService` 使用，属刻意增强而非分歧。

    Args:
        text: 消解后的用户查询。
        intent: 意图识别结果。
        state: 会话状态。
        emotion_negative: 本轮情绪是否负面。
        retrieval_decision: 已知的检索决策（预取时传入，用于规则⑤）。

    Returns:
        `ActionDecision`。
    """
    # ---- ① 转人工：显式请求最高（标签 + 文本双判）----
    if intent.label == "转人工请求" or _is_explicit_handoff(text):
        rid, rname = _rule_meta(1)
        return ActionDecision(
            action=ActionType.HANDOFF,
            reason="用户显式要求转人工",
            handoff_reason=HandoffReason.USER_REQUEST,
            rule_id=rid,
            rule_name=rname,
        )

    # ---- ② 转人工：高风险意图（标签 + 文本双判）----
    if intent.label == "投诉维权" or _is_high_risk(text):
        rid, rname = _rule_meta(4)
        return ActionDecision(
            action=ActionType.HANDOFF,
            reason=f"高风险意图：{intent.label}",
            handoff_reason=HandoffReason.HIGH_RISK,
            risk=RiskLevel.HIGH,
            rule_id=rid,
            rule_name=rname,
        )

    # ---- ③ 转人工：情绪负面 ----
    if emotion_negative:
        rid, rname = _rule_meta(2)
        return ActionDecision(
            action=ActionType.HANDOFF,
            reason="用户情绪负面",
            handoff_reason=HandoffReason.EMOTION,
            risk=RiskLevel.MEDIUM,
            rule_id=rid,
            rule_name=rname,
        )

    # ---- ④ 转人工：轮次超限 ----
    if should_force_handoff(state):
        rid, rname = _rule_meta(3)
        return ActionDecision(
            action=ActionType.HANDOFF,
            reason=f"会话轮次 {state.turn_count} 超过上限，强制引导人工",
            handoff_reason=HandoffReason.UNRESOLVED,
            risk=RiskLevel.MEDIUM,
            rule_id=rid,
            rule_name=rname,
        )

    # ---- ⑤ 转人工：检索无依据且属业务类（规则⑤）----
    if retrieval_decision == RagDecision.NO_EVIDENCE.value and (
        intent.category
        in (IntentCategory.AFTER_SALE, IntentCategory.PRODUCT_CONSULT, IntentCategory.BRAND_TRUST)
    ):
        rid, rname = _rule_meta(5)
        return ActionDecision(
            action=ActionType.HANDOFF,
            reason="检索无依据且属业务类问题",
            handoff_reason=HandoffReason.UNRESOLVED,
            risk=RiskLevel.MEDIUM,
            rule_id=rid,
            rule_name=rname,
        )

    # ---- ⑥ 该查：意图+信号词都指向「查这一单」----
    tools = list(_TOOL_INTENTS.get(intent.category, ()))
    if tools and _looks_like_query(text, state):
        return ActionDecision(
            action=ActionType.TOOL_QUERY,
            reason=f"意图 {intent.category.value} 且命中查询信号，走接口取实时数据",
            tools=tools,
        )

    # ---- ⑦ 该答 ----
    return ActionDecision(
        action=ActionType.RAG_ANSWER,
        reason=f"意图 {intent.category.value}，属知识咨询",
    )


def _looks_like_query(text: str, state: SessionState) -> bool:
    """判断这句是否在问「我这单现在怎样」。

    两个条件满足任一即可：
    * 文本里带了订单号/运单号或查询信号词；
    * 会话槽位里已有订单号，且本轮是追问（意图延续）。

    **不能只看意图**：用户问「你们退款几天到账」是政策问题，
    意图同样是售后服务，但去拉他的订单毫无意义还会多一次越权调用。
    """
    if extract_entities(text).get("order_no"):
        return True
    if any(w in text for w in _QUERY_HINT_WORDS):
        return True
    # 追问场景：槽位里已有订单号，且本轮句子很短（「那还要多久」）
    if state.slots.get("order_no") and len(text) <= 15:
        return True
    return False


# ---------------------------------------------------------------------------
# 编排服务
# ---------------------------------------------------------------------------


class Orchestrator:
    """对话编排服务。

    Attributes:
        store: 会话状态仓库。
        answer_service: 答案生成服务。
        store_tools: 业务工具注册表（`app.services.tools.registry`）。
        handoff_service: 转人工服务（`app.services.handoff.service`）。
    """

    def __init__(
        self,
        *,
        store: SessionStore | None = None,
        answer_service: Any = None,
        tool_registry: Any = None,
        handoff_service: Any = None,
    ) -> None:
        self._store = store
        self._answer = answer_service
        self._tools = tool_registry
        self._handoff = handoff_service

    @property
    def store(self) -> SessionStore:
        """会话仓库（惰性）。"""
        if self._store is None:
            self._store = get_session_store()
        return self._store

    @property
    def answer_service(self) -> Any:
        """答案生成服务（惰性）。"""
        if self._answer is None:
            from app.services.answer import get_answer_service

            self._answer = get_answer_service()
        return self._answer

    @property
    def tool_registry(self) -> Any:
        """工具注册表（惰性）。未实现时返回空注册表，编排层降级为纯 RAG。"""
        if self._tools is None:
            try:
                from app.services.tools.registry import get_tool_registry

                self._tools = get_tool_registry()
            except Exception:  # noqa: BLE001
                logger.warning("业务工具层不可用，编排降级为纯 RAG 模式")
                self._tools = None
        return self._tools

    @property
    def handoff_service(self) -> Any:
        """转人工服务（惰性）。"""
        if self._handoff is None:
            try:
                from app.services.handoff.service import get_handoff_service

                self._handoff = get_handoff_service()
            except Exception:  # noqa: BLE001
                logger.warning("转人工服务不可用，退回纯文字提示")
                self._handoff = None
        return self._handoff

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def handle_turn(
        self,
        *,
        session_id: str,
        text: str,
        channel: str = Channel.TAOBAO.value,
        buyer_id: str = "",
        msg_type: str = InboundMsgType.TEXT.value,
        enable_tools: bool = True,
    ) -> TurnResult:
        """处理一轮用户输入。

        Args:
            session_id: 会话 ID。
            text: 用户文本（语音/图片路径已在前置层归一化成文本）。
            channel: 渠道。
            buyer_id: 买家 ID（工具鉴权用）。
            msg_type: 原始消息类型（影响兜底话术）。
            enable_tools: 是否允许调用业务工具。

        Returns:
            `TurnResult`。
        """
        t_start = time.perf_counter()
        trace_id = get_trace_id()
        result = TurnResult(trace_id=trace_id)
        budget: dict[str, float] = {}

        def mark(step: str, since: float) -> None:
            """记录一步耗时。"""
            budget[step] = budget.get(step, 0.0) + (
                time.perf_counter() - since
            ) * 1000

        settings = get_settings()

        # ---------------- 步骤 1：读状态 ----------------
        t = time.perf_counter()
        state = self.store.load(session_id, channel=channel)
        mark("state", t)

        # ---------------- 步骤 2：指代消解 + 实体 ----------------
        t = time.perf_counter()
        entities = extract_entities(text)
        resolved: ResolveResult = resolve_reference(text, state, entities)
        mark("resolve", t)

        # ---------------- 步骤 3：意图 + 情绪 ----------------
        t = time.perf_counter()
        intent = get_intent_classifier().classify(resolved.query)
        emotion = get_emotion_detector().detect(text, prev_streak=state.negative_streak)
        mark("intent", t)

        result.intent = intent.to_dict()
        result.emotion = emotion.to_dict()
        result.resolve = resolved.to_dict()

        # ---------------- 步骤 4：动作决策 ----------------
        t = time.perf_counter()
        decision = decide_action(
            text=resolved.query,
            intent=intent,
            state=state,
            emotion_negative=is_negative(emotion.emotion.value),
        )
        mark("decide", t)
        result.decision = decision.to_dict()
        logger.info(
            "编排决策 session=%s action=%s reason=%s intent=%s conf=%.2f",
            session_id,
            decision.action.value,
            decision.reason,
            intent.label,
            intent.confidence,
        )

        # ---------------- 步骤 5：按决策执行 ----------------
        if decision.action is ActionType.HANDOFF:
            t = time.perf_counter()
            self._do_handoff(
                result=result,
                state=state,
                decision=decision,
                text=resolved.query,
                intent=intent,
                emotion=emotion,
            )
            mark("handoff", t)
        elif decision.action is ActionType.TOOL_QUERY and enable_tools:
            # 工具与 RAG **并行**：用户常在同一句里既问「我这单到哪了」又问
            # 「你们的赔付规则」——两件事数据源完全不同，串行跑会让 P95 翻倍。
            # 失败互不影响：工具挂了 RAG 仍能答，RAG 挂了工具结果仍可用。
            t = time.perf_counter()
            self._do_tool_and_rag(
                result=result,
                state=state,
                decision=decision,
                query=resolved.query,
                buyer_id=buyer_id,
                session_id=session_id,
                channel=channel,
            )
            mark("tool", t)
        else:
            t = time.perf_counter()
            self._do_rag_answer(
                result=result,
                state=state,
                query=resolved.query,
                session_id=session_id,
                channel=channel,
            )
            mark("rag", t)

        # ---------------- 步骤 6：更新状态 ----------------
        t = time.perf_counter()
        update_state_after_turn(
            state,
            text=text,
            resolved_query=resolved.query,
            entities=entities,
            intent=intent.category.value,
            resolved_unresolved=not result.degraded,
        )
        state.emotion = emotion.emotion.value
        state.negative_streak = emotion.negative_streak
        if result.handoff:
            state.handoff = True
        if result.degraded:
            state.unresolved_count += 1
        self.store.save(state)
        mark("state", t)

        # ---------------- 收尾：耗时记账 ----------------
        total = (time.perf_counter() - t_start) * 1000
        # 「编排层自身耗时」= 全部步骤 - 外部依赖（rag/tool/handoff）
        external = budget.get("rag", 0) + budget.get("tool", 0) + budget.get("handoff", 0)
        result.latency_ms = {
            "intent": int(budget.get("resolve", 0) + budget.get("intent", 0) + budget.get("decide", 0)),
            "retrieval": int(budget.get("rag", 0)),
            "tool": int(budget.get("tool", 0)),
            "handoff": int(budget.get("handoff", 0)),
            "total": int(total),
            # DoD ⑤ 的考核对象：编排层自身，不含外部依赖
            "self": int(max(0, total - external)),
        }
        logger.info(
            "一轮完成 session=%s action=%s total=%dms self=%dms degraded=%s",
            session_id,
            decision.action.value,
            result.latency_ms["total"],
            result.latency_ms["self"],
            result.degraded,
        )
        self._persist_turn(
            result=result,
            session_id=session_id,
            text=text,
            channel=channel,
            buyer_id=buyer_id,
            msg_type=msg_type,
        )
        return result

    def _persist_turn(
        self,
        *,
        result: TurnResult,
        session_id: str,
        text: str,
        channel: str,
        buyer_id: str,
        msg_type: str,
    ) -> None:
        """把本轮落库（`sessions`/`messages`/`turns`），供 BFF 翻历史。

        **旁路**：任何失败只记日志，绝不影响给用户的回复。
        """
        try:
            from app.db.repository import get_conversation_repository

            decision = result.decision or {}
            handoff = result.handoff or {}
            top_score = 0.0
            if result.rag_hits:
                top_score = float(result.rag_hits[0].get("score") or 0.0)
            get_conversation_repository().record_turn(
                session_id=session_id,
                trace_id=result.trace_id or get_trace_id(),
                user_text=text,
                reply_text=result.reply_content,
                channel=channel,
                buyer_id=buyer_id,
                msg_type=msg_type,
                decision=str(decision.get("action") or decision.get("rag_decision") or ""),
                top_score=top_score,
                actions=result.actions,
                tool_calls=decision.get("tool_calls") or [],
                rag_hits=result.rag_hits,
                handoff_reason=handoff.get("reason"),
                latency=result.latency_ms,
                degraded=result.degraded,
                blocked=bool(decision.get("blocked")),
                intent=str((result.intent or {}).get("category") or ""),
                emotion=str((result.emotion or {}).get("emotion") or ""),
                handoff=handoff or None,
            )
        except Exception as exc:  # noqa: BLE001 —— 留痕失败不能炸回复
            logger.warning("会话留痕失败 session=%s err=%s", session_id, exc)

    # ------------------------------------------------------------------
    # 分支实现
    # ------------------------------------------------------------------

    def _do_rag_answer(
        self,
        *,
        result: TurnResult,
        state: SessionState,
        query: str,
        session_id: str,
        channel: str,
    ) -> None:
        """走 RAG 回答（含三道闸）。"""
        filters = _build_filters(state)
        try:
            answer = self.answer_service.answer(
                query,
                session_id=session_id,
                channel=channel,
                filters=filters,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("RAG 回答失败，降级兜底 query=%s", query[:30])
            result.reply_content = _fallback_text("ANSWER_FAILED")
            result.degraded.append("RAG_FAILED")
            result.decision["rag_error"] = str(exc)[:120]
            return

        result.reply_content = answer.text
        result.actions.append(ActionType.RAG_ANSWER.value)
        result.rag_hits = _hits_to_brief(answer)
        result.decision["rag_decision"] = answer.decision
        if answer.degraded:
            result.degraded.append(answer.degrade_reason or "RAG_DEGRADED")

    def _do_tool_and_rag(
        self,
        *,
        result: TurnResult,
        state: SessionState,
        decision: ActionDecision,
        query: str,
        buyer_id: str,
        session_id: str,
        channel: str,
    ) -> None:
        """工具查询与 RAG 检索**并行**执行，结果合并。

        为什么必须并行：用户常在同一个句子里问两件事——
        「我订单 2026****0001 到哪了，你们的发货规则是什么」。
        工具查实时状态（几百毫秒到 1.5 秒），RAG 查知识（几十到两百毫秒），
        两者数据源完全无关。串行跑等于把两段延迟相加，P95 直接翻倍。

        为什么必须隔离失败：工具挂了不代表知识也查不到，反之亦然。
        这里用 `submit` 分别提交，任一路异常只降级自己那一路。
        """
        registry = self.tool_registry
        answer_ok = True
        tools_available = registry is not None

        def run_tools() -> list["ToolOutcome"]:
            """跑全部工具。"""
            if registry is None:
                return []

            order_no = state.slots.get("order_no") or ""

            def call(tool: ToolName) -> ToolOutcome:
                started = time.perf_counter()
                try:
                    payload = registry.invoke(
                        tool,
                        session_id=session_id,
                        buyer_id=buyer_id,
                        order_no=order_no,
                    )
                    return ToolOutcome(
                        tool=tool,
                        ok=True,
                        data=getattr(payload, "data", {}) or {},
                        text=getattr(payload, "text", "") or "",
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                except Exception as exc:  # noqa: BLE001
                    return ToolOutcome(
                        tool=tool,
                        ok=False,
                        error=str(exc)[:120],
                        latency_ms=(time.perf_counter() - started) * 1000,
                        degraded=True,
                    )

            if len(decision.tools) > 1:
                with ThreadPoolExecutor(max_workers=len(decision.tools)) as pool:
                    return list(pool.map(call, decision.tools))
            return [call(t) for t in decision.tools]

        def run_rag() -> str:
            """跑 RAG，返回文本。"""
            nonlocal answer_ok
            try:
                answer = self.answer_service.answer(
                    query,
                    session_id=session_id,
                    channel=channel,
                    filters=_build_filters(state),
                )
                result.rag_hits = [
                    {"chunk_id": cid, "score": round(answer.top_score, 4)}
                    for cid in answer.hits
                ]
                result.decision["rag_decision"] = answer.decision
                if answer.degraded:
                    result.degraded.append(answer.degrade_reason or "RAG_DEGRADED")
                return answer.text
            except Exception as exc:  # noqa: BLE001
                answer_ok = False
                result.degraded.append("RAG_FAILED")
                result.decision["rag_error"] = str(exc)[:120]
                return ""

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_tools = pool.submit(run_tools)
            f_rag = pool.submit(run_rag)
            outcomes = f_tools.result()
            rag_text = f_rag.result()

        lines: list[str] = []
        if not tools_available:
            # 工具层整体不可用：这是**必须显式记降级**的情形。
            # 静默留空会让用户以为「查过了但没结果」，实际根本没查。
            result.degraded.append("TOOL_LAYER_ABSENT")
            logger.warning("工具层不可用，本轮无法查询实时数据 session=%s", session_id)
        for out in outcomes:
            result.actions.append(out.tool.value)
            if out.ok and out.text:
                lines.append(out.text)
            else:
                result.degraded.append(f"{out.tool.value.upper()}_FAILED")

        if rag_text:
            result.actions.append(ActionType.RAG_ANSWER.value)
            lines.append(rag_text)

        if not lines:
            result.reply_content = _fallback_text("TOOL_FAILED")
            result.degraded.append("ALL_SOURCES_FAILED")
            return
        # 工具结果在前（用户最关心自己的单），知识在后（补充规则）
        result.reply_content = "\n".join(lines)
        if not answer_ok:
            logger.info("复合分支中 RAG 失败，仅返回工具结果")

    def _do_handoff(
        self,
        *,
        result: TurnResult,
        state: SessionState,
        decision: ActionDecision,
        text: str,
        intent: IntentResult,
        emotion: Any,
    ) -> None:
        """执行转人工（BE-13 判定 + BE-14 建单 + BE-15 兜底话术）。"""
        reason = decision.handoff_reason or HandoffReason.UNRESOLVED
        card = {
            "summary": text[:200],
            "verified": [],  # 由工具层回填
            "tried_actions": list(result.actions),
            "unmet_demand": text[:200],
            "suggested_action": f"人工跟进：{decision.reason}",
            "emotion": emotion.emotion.value,
            "turn_count": state.turn_count,
        }
        service = self.handoff_service
        if service is None:
            result.reply_content = _fallback_text("HANDOFF")
            result.handoff = {
                "reason": reason.value,
                "ticket_no": None,
                "queue_position": None,
                "offline": True,
                "rule_id": decision.rule_id,
                "rule_name": decision.rule_name,
            }
            return

        try:
            outcome = service.handoff(
                session_id=state.session_id,
                buyer_id=getattr(state, "buyer_id", "") or "",
                channel=state.channel,
                reason=reason,
                context_card=card,
                intent=intent.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("转人工执行失败，退回纯文字提示")
            result.reply_content = _fallback_text("HANDOFF")
            result.degraded.append("HANDOFF_FAILED")
            result.decision["handoff_error"] = str(exc)[:120]
            return

        result.reply_content = getattr(outcome, "text", "") or _fallback_text("HANDOFF")
        result.actions.append(ActionType.HANDOFF.value)
        result.handoff = {
            "reason": reason.value,
            "ticket_no": getattr(outcome, "ticket_no", None),
            "queue_position": getattr(outcome, "queue_position", None),
            "offline": getattr(outcome, "offline", False),
            "rule_id": decision.rule_id,
            "rule_name": decision.rule_name,
        }


def _hits_to_brief(answer: Any) -> list[dict[str, Any]]:
    """把 `AnswerResult` 的命中转成契约摘要，**带上知识类目**。

    为什么不能只回 chunk_id：契约 2.4.1 的示例里 `rag_hits` 带
    `category` 字段，前端要靠它分类展示，坐席卡片要靠它判断
    「这个问题问的是政策还是商品」——只回一个 ID 等于把解释责任推给前端。

    类目取法有两层：`AnswerResult` 若已带元数据直接用；否则从 chunk_id
    的命名前缀推（本项目 chunk_id 形如 `kb_faq:xxx` / `kb_product:xxx`）。
    推不出来就留 None，**不编**。
    """
    out: list[dict[str, Any]] = []
    metas = getattr(answer, "hit_metadata", None) or []
    for idx, cid in enumerate(getattr(answer, "hits", []) or []):
        meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
        category = meta.get("category")
        if not category:
            category = _category_from_chunk_id(cid)
        out.append(
            {
                "chunk_id": cid,
                "score": round(float(getattr(answer, "top_score", 0.0) or 0.0), 4),
                "category": category,
            }
        )
    return out


#: chunk_id 前缀 → 知识类目（兜底推断用）
_CHUNK_PREFIX_CATEGORY: dict[str, str] = {
    "kb_faq": "常见问题",
    "kb_product": "商品知识",
    "kb_policy": "政策条款",
    "kb_doc": "文档知识",
    "kb_manual": "人工录入",
}


def _category_from_chunk_id(chunk_id: str) -> str | None:
    """从 chunk_id 前缀推断知识类目。推不出返回 None（**不猜**）。"""
    if not chunk_id:
        return None
    prefix = chunk_id.split(":", 1)[0]
    return _CHUNK_PREFIX_CATEGORY.get(prefix)


def _build_filters(state: SessionState) -> dict[str, Any]:
    """按会话槽位构造检索过滤。

    只在用户**明确提到 SKU** 时按 SKU 过滤。否则不传——
    过度过滤会让「你们家退换货怎么算」这类通用问题命中不到政策文档
    （那些文档的 `sku_ids` 是空的）。
    """
    filters: dict[str, Any] = {}
    sku = state.slots.get("sku_id")
    if sku:
        # 注意：这里**不能**只按 sku 过滤，否则通用政策被排除。
        # 正确做法是不传 sku_ids，让召回全量后在重排阶段排序。
        # 保留这段逻辑是为了说明「想过但否掉了」。
        _ = sku
    return filters


#: 兜底话术表。**禁止「请稍等」类无限等待表述**（BE-15 DoD）。
_FALLBACK_TEXTS: dict[str, str] = {
    "ANSWER_FAILED": "这边暂时没能查到准确答案，我帮您转接人工客服，由同事为您核实～",
    "TOOL_LAYER_ABSENT": "订单信息我这边暂时查不到，帮您转接人工客服核实一下～",
    "TOOL_FAILED": "系统查询暂时没响应，您的订单信息我没法确认。已为您转接人工客服，稍后会有专人联系您～",
    "HANDOFF": "已为您转接人工客服，稍后会有专人与您联系～",
}


def _fallback_text(key: str) -> str:
    """取兜底话术。"""
    return _FALLBACK_TEXTS.get(key, _FALLBACK_TEXTS["HANDOFF"])


def get_orchestrator() -> Orchestrator:
    """全局编排器单例。"""
    global _ORCH
    if _ORCH is None:
        _ORCH = Orchestrator()
    return _ORCH


def set_orchestrator(orch: Orchestrator | None) -> None:
    """替换单例（测试用）。"""
    global _ORCH
    _ORCH = orch


_ORCH: Orchestrator | None = None
