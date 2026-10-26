"""BE-13 · 转人工触发判定引擎（六条规则，可配置）。

## 为什么单独做一个引擎

`app/services/orchestrator.py` 的 `decide_action()` 只回答「这一轮该答、该查、
还是该转」；**为什么转、按哪条规则转、优先级多高** 是另一件事，且必须
**可配置、可审计、可回归**：

* 运营在后台改「连续 2 轮兜底」为「连续 3 轮」时，不应改代码；
* 复盘时要说清「这单是规则④命中的，不是规则②」；
* 六条规则的准确率要能被单测逐条钉住（DoD① 每条 ≥ 20 例、100%）。

因此规则以 `handoff_rules` 表为准（`backend/scripts/seed_reference_data.py`
写入 6 条），本模块负责**加载 + 判定**，不负责持久化。

## 规则表（与种子数据一致）

| 规则 | 名称 | `reason` | 阈值 | 优先级 |
| --- | --- | --- | --- | --- |
| ① | 用户显式请求人工 | `USER_REQUEST` | 0（词表命中即中） | 100 |
| ② | 用户情绪激动 | `EMOTION` | 连续负面 3 轮 | 90 |
| ③ | 连续未解决 | `UNRESOLVED` | 连续兜底 2 轮 / 轮次 > 10 | 80 |
| ④ | 高风险意图 | `HIGH_RISK` | 0（词表命中即中） | 100 |
| ⑤ | 检索无依据 | `UNRESOLVED` | 最高分 < 0.55 且属业务类 | 60 |
| ⑥ | 系统异常 | `SYSTEM_ERROR` | 0（异常标志即中） | 50 |

**仲裁顺序 = 优先级降序，同优先级按 `rule_id` 升序**。这保证：

* 「转人工，我要投诉」→ 规则①（`rule_id=1`）先于规则④，`reason=USER_REQUEST`
  （用户要的是「人」，投诉只是顺带说的诉求，见 `test_handoff_passes_reason_enum`
  与 `test_explicit_request_beats_high_risk`）；
* 规则①④ 优先级并列最高，**永远不被情绪/未解决规则盖掉**；
* 规则①④ 是 `enabled` 强制为 True 的两条（种子数据里写死），运营关不掉——
  关掉就意味着用户喊「转人工」AI 还在自顾自答，这是合规事故。

## 性能

纯内存字符串匹配 + 一次词表扫描，单次判定在微秒级；DoD④ 要求 P95 ≤ 50ms，
本模块 `judge()` 自带 `latency_ms` 便于断言。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.core.enums import Emotion, HandoffReason, IntentCategory, Priority
from app.schemas.handoff import REASON_DEFAULT_PRIORITY, REASON_CN

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 规则⑤「检索无依据」适用的意图大类。
#:
#: **只放 3 类**：商品咨询 / 售后服务 / 品牌信任——这三类问题一旦无依据，
#: 正确做法就是转人工。订单服务与营销活动**不放**：前者可由订单接口
#: 直接取数、后者有活动规则模板兜底，转人工只会白烧坐席。
#: 该集合与 `orchestrator.decide_action` 的第⑤步严格一致，
#: 由 `tests/test_handoff_rules.py::test_engine_agrees_with_orchestrator`
#: 逐例比对（回归 `test_no_evidence_non_business_intent_still_answers`：
#: 营销类无依据仍要 RAG 回答）。
BUSINESS_CATEGORIES: frozenset[IntentCategory] = frozenset(
    {
        IntentCategory.PRODUCT_CONSULT,
        IntentCategory.AFTER_SALE,
        IntentCategory.BRAND_TRUST,
    }
)

#: 显式请求人工的正则（与 `orchestrator._EXPLICIT_HANDOFF_RE` 保持同源语义）。
#:
#: 覆盖「转人工 / 找人工 / 要真人 / 人工客服在吗 / 换个人」等说法；
#: **不覆盖**「人工」单独出现（「人工客服电话是多少」是问知识，不是要转）。
EXPLICIT_HANDOFF_RE: re.Pattern[str] = re.compile(
    r"(?:转|找|要|换|叫|来|接)\s*(?:个|一位|位)?\s*(?:真人|人工|活人|人)"
    r"|(?:真人|人工)\s*(?:客服)?\s*(?:在吗|在么|呢|有没有)"
    r"|不要\s*(?:机器人|智能客服|AI)"
    r"|(?:叫|把)\s*(?:你们)?\s*经理"
)

#: 高风险正则（与 `orchestrator._HIGH_RISK_RE` 同源，另补平台/媒体口径）。
HIGH_RISK_RE: re.Pattern[str] = re.compile(
    r"12315|12345|消协|消保委|市场监管|工商(?:局)?|打官司|起诉|律师函|法院|报警|立案"
    r"|曝光|媒体|记者|职业打假|假一赔"
)

#: 负面情绪集合（`Emotion` 的中文值，兼容枚举成员名）。
NEGATIVE_EMOTIONS: frozenset[str] = frozenset({"不满", "愤怒", "DISSATISFIED", "ANGRY"})

#: 规则①④ 不可关闭（合规底线）
ALWAYS_ON_REASONS: frozenset[HandoffReason] = frozenset(
    {HandoffReason.USER_REQUEST, HandoffReason.HIGH_RISK}
)


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSpec:
    """一条转人工规则。

    Attributes:
        rule_id: 1~6，对应 `handoff_rules.rule_id`。
        name: 规则名（后台展示）。
        reason: 命中后写入工单的 `reason`。
        priority: 仲裁优先级，**数值越大越优先**。
        threshold: 阈值，语义随规则而变（见模块 docstring 表格）。
        keywords: 关键词表（规则①②④ 用）。
        enabled: 是否启用；①④ 恒为 True。
        description: 说明（后台展示）。
    """

    rule_id: int
    name: str
    reason: HandoffReason
    priority: int
    threshold: float = 0.0
    keywords: tuple[str, ...] = ()
    enabled: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        # ①④ 不可关闭：直接纠正，而不是抛错——种子数据/后台误操作都兜住
        if self.reason in ALWAYS_ON_REASONS and not self.enabled:
            object.__setattr__(self, "enabled", True)


#: 内置默认规则（DB 不可用或未 seed 时使用，与种子数据同值）。
#:
#: ⚠️ 规则④ 的关键词在种子数据基础上**补入「投诉/举报/欺诈」**：商家语料里
#: 这三类词的用户诉求本质是「要一个处理结果」，归 `HIGH_RISK` 比归
#: `EMOTION` 更符合工单路由（G5 投诉与升级组）。种子数据同步已补。
DEFAULT_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        rule_id=1,
        name="用户显式请求人工",
        reason=HandoffReason.USER_REQUEST,
        priority=100,
        threshold=0.0,
        keywords=(
            "转人工",
            "人工客服",
            "找人工",
            # ⚠️ 不能只写「真人」：`真人版玩偶有吗` 会被误判成转人工
            # （回归 `test_non_request_not_misclassified`）。动词结构由
            # `EXPLICIT_HANDOFF_RE` 负责，词表只放无歧义的整体词。
            "真人客服",
            "人工服务",
            "接人工",
            "我要找人",
            "不要机器人",
            "智能客服不行",
            "转接人工",
        ),
        enabled=True,
        description="用户明确要求真人服务，必须立即转，禁止任何挽留话术。",
    ),
    RuleSpec(
        rule_id=2,
        name="用户情绪激动",
        reason=HandoffReason.EMOTION,
        priority=90,
        threshold=3.0,
        keywords=(
            "垃圾",
            "骗子",
            "假货",
            "差评",
            "气死",
            "愤怒",
            "太差了",
            "什么玩意",
            "恶心",
            "无语",
        ),
        enabled=True,
        description="连续 3 轮负面情绪，或命中辱骂/威胁词表。",
    ),
    RuleSpec(
        rule_id=3,
        name="连续未解决",
        reason=HandoffReason.UNRESOLVED,
        priority=80,
        threshold=2.0,
        keywords=(),
        enabled=True,
        description="同一问题连续 2 轮兜底，或单会话轮次超过上限（10 轮）。",
    ),
    RuleSpec(
        rule_id=4,
        name="高风险意图",
        reason=HandoffReason.HIGH_RISK,
        priority=100,
        threshold=0.0,
        keywords=(
            "赔偿",
            "起诉",
            "律师",
            "法院",
            "报警",
            "立案",
            "曝光",
            "媒体",
            "记者",
            "打假",
            "职业打假",
            "消保委",
            "12315",
            "投诉",
            "举报",
            "欺诈",
            "法律程序",
        ),
        enabled=True,
        description="投诉/赔付/法律/平台介入/媒体曝光，一律 URGENT 转升级组。",
    ),
    RuleSpec(
        rule_id=5,
        name="检索无依据",
        reason=HandoffReason.UNRESOLVED,
        priority=60,
        threshold=0.55,
        keywords=(),
        enabled=True,
        description="最高分 < 0.55 且问题属业务类，AI 不得编造，转人工。",
    ),
    RuleSpec(
        rule_id=6,
        name="系统异常",
        reason=HandoffReason.SYSTEM_ERROR,
        priority=50,
        threshold=0.0,
        keywords=(),
        enabled=True,
        description="LLM / 检索 / 工具超时或报错，转人工并记录异常。",
    ),
)


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass
class RuleHit:
    """单条规则的命中详情（审计与前端「为什么转」用）。"""

    rule_id: int
    name: str
    reason: HandoffReason
    priority: int
    matched: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 友好字典。"""
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "reason": self.reason.value,
            "priority": self.priority,
            "matched": self.matched,
            "detail": self.detail,
        }


@dataclass
class HandoffDecision:
    """转人工判定结果。

    Attributes:
        should_handoff: 是否转人工。
        reason: 命中规则的 `reason`；不转时为 None。
        rule_id: 命中的规则号；不转时为 None。
        rule_name: 命中规则名。
        priority: 建议优先级（由 `reason` 推导，可被调用方覆盖）。
        hits: 全部六条规则的命中情况（含未命中），用于解释与回归。
        latency_ms: 判定耗时（DoD④ P95 ≤ 50ms 的考核值）。
    """

    should_handoff: bool = False
    reason: HandoffReason | None = None
    rule_id: int | None = None
    rule_name: str = ""
    priority: Priority = Priority.NORMAL
    hits: list[RuleHit] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def matched_rules(self) -> list[int]:
        """命中的规则号（升序）。"""
        return [h.rule_id for h in self.hits if h.matched]

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 友好字典。"""
        return {
            "should_handoff": self.should_handoff,
            "reason": self.reason.value if self.reason else None,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "priority": self.priority.value,
            "matched_rules": self.matched_rules,
            "hits": [h.to_dict() for h in self.hits],
            "latency_ms": round(self.latency_ms, 3),
        }


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------


def _coerce_reason(value: Any) -> HandoffReason | None:
    """把 DB 里的 `reason` 字符串转枚举，未知值返回 None（跳过该规则）。"""
    if isinstance(value, HandoffReason):
        return value
    try:
        return HandoffReason(str(value))
    except ValueError:
        return None


def rules_from_rows(rows: Iterable[Any]) -> tuple[RuleSpec, ...]:
    """把 `handoff_rules` 表行（ORM 对象或 dict）转成 `RuleSpec`。

    未知 `reason` 的行会被**跳过**而不是整表失败——后台脏数据不该让
    转人工整体不可用。
    """
    out: list[RuleSpec] = []
    for row in rows:
        get = row.get if isinstance(row, dict) else lambda k, r=row: getattr(r, k, None)
        reason = _coerce_reason(get("reason"))
        if reason is None:
            continue
        keywords = get("keywords") or ()
        if isinstance(keywords, str):
            keywords = tuple(k.strip() for k in keywords.split(",") if k.strip())
        out.append(
            RuleSpec(
                rule_id=int(get("rule_id") or 0),
                name=str(get("name") or ""),
                reason=reason,
                priority=int(get("priority") or 0),
                threshold=float(get("threshold") or 0.0),
                keywords=tuple(str(k) for k in keywords),
                enabled=bool(get("enabled", True)),
                description=str(get("description") or ""),
            )
        )
    return tuple(sorted(out, key=lambda r: r.rule_id))


def load_rules() -> tuple[RuleSpec, ...]:
    """从 `handoff_rules` 表加载规则；表不存在/为空/DB 不可用时回退默认值。

    **永不抛异常**：转人工是合规兜底链路，不能因为一次数据库抖动就整体失效。
    """
    try:
        from app.db.engine import session_scope
        from app.db.models.ticket import HandoffRule

        with session_scope() as session:
            rows = session.query(HandoffRule).order_by(HandoffRule.rule_id).all()
            rules = rules_from_rows(rows)
        return rules or DEFAULT_RULES
    except Exception:  # noqa: BLE001 - 任何 DB 问题都回退内置规则
        return DEFAULT_RULES


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class HandoffRuleEngine:
    """六条规则的判定引擎（线程内无状态，可安全复用）。"""

    def __init__(self, rules: Sequence[RuleSpec] | None = None) -> None:
        self._rules: tuple[RuleSpec, ...] = tuple(rules) if rules else DEFAULT_RULES

    # -- 属性 ---------------------------------------------------------------

    @property
    def rules(self) -> tuple[RuleSpec, ...]:
        """当前生效规则（按 `rule_id` 升序）。"""
        return self._rules

    def reload(self) -> None:
        """从数据库重新加载规则（后台改完规则后调用）。"""
        self._rules = load_rules()

    # -- 判定 ---------------------------------------------------------------

    def judge(
        self,
        *,
        text: str = "",
        intent_label: str = "",
        intent_category: IntentCategory | str | None = None,
        emotion: Emotion | str | None = None,
        negative_streak: int = 0,
        unresolved_count: int = 0,
        turn_count: int = 0,
        retrieval_decision: str | None = None,
        top_score: float | None = None,
        system_error: bool = False,
        max_turns: int = 10,
    ) -> HandoffDecision:
        """判定本轮是否转人工。

        Args:
            text: 用户原话。
            intent_label: 意图细分标签（如「转人工请求」「投诉维权」）。
            intent_category: 意图大类。
            emotion: 当前情绪（枚举或中文/英文值）。
            negative_streak: 连续负面情绪轮次。
            unresolved_count: 同一问题连续兜底轮次。
            turn_count: 本会话累计轮次。
            retrieval_decision: RAG 决策（`NO_EVIDENCE` 时规则⑤可能命中）。
            top_score: 检索最高分；None 表示「已判定为无依据」，按 0 处理。
            system_error: 本轮是否发生系统异常。
            max_turns: 轮次上限（默认 10，与 `Settings.max_turns_per_session` 一致）。

        Returns:
            `HandoffDecision`；`should_handoff=False` 时 `reason/rule_id` 为 None。
        """
        started = time.perf_counter()
        category = self._norm_category(intent_category)
        emo = self._norm_emotion(emotion)
        ctx = _JudgeContext(
            text=text or "",
            intent_label=intent_label or "",
            category=category,
            emotion=emo,
            negative_streak=int(negative_streak or 0),
            unresolved_count=int(unresolved_count or 0),
            turn_count=int(turn_count or 0),
            retrieval_decision=(retrieval_decision or "").upper(),
            top_score=0.0 if top_score is None else float(top_score),
            system_error=bool(system_error),
            max_turns=int(max_turns or 10),
        )

        hits: list[RuleHit] = []
        winner: RuleSpec | None = None
        # 仲裁：优先级降序，同优先级 rule_id 升序（①先于④）
        for rule in sorted(self._rules, key=lambda r: (-r.priority, r.rule_id)):
            if not rule.enabled:
                hits.append(
                    RuleHit(
                        rule_id=rule.rule_id,
                        name=rule.name,
                        reason=rule.reason,
                        priority=rule.priority,
                        matched=False,
                        detail="规则已停用",
                    )
                )
                continue
            matched, detail = self._match(rule, ctx)
            hits.append(
                RuleHit(
                    rule_id=rule.rule_id,
                    name=rule.name,
                    reason=rule.reason,
                    priority=rule.priority,
                    matched=matched,
                    detail=detail,
                )
            )
            if matched and winner is None:
                winner = rule

        latency = (time.perf_counter() - started) * 1000.0
        if winner is None:
            return HandoffDecision(hits=hits, latency_ms=latency)
        return HandoffDecision(
            should_handoff=True,
            reason=winner.reason,
            rule_id=winner.rule_id,
            rule_name=winner.name,
            priority=REASON_DEFAULT_PRIORITY.get(winner.reason, Priority.NORMAL),
            hits=hits,
            latency_ms=latency,
        )

    # -- 单条规则匹配 -------------------------------------------------------

    def _match(self, rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        """匹配单条规则，返回 (是否命中, 说明)。"""
        if rule.rule_id == 1:
            return self._match_explicit(rule, ctx)
        if rule.rule_id == 2:
            return self._match_emotion(rule, ctx)
        if rule.rule_id == 3:
            return self._match_unresolved(rule, ctx)
        if rule.rule_id == 4:
            return self._match_high_risk(rule, ctx)
        if rule.rule_id == 5:
            return self._match_no_evidence(rule, ctx)
        if rule.rule_id == 6:
            return self._match_system_error(rule, ctx)
        # 未知规则号：按关键词兜底匹配（后台新增规则时不必改代码）
        if rule.keywords:
            hit = _first_keyword(ctx.text, rule.keywords)
            return (bool(hit), f"命中关键词「{hit}」" if hit else "")
        return False, ""

    @staticmethod
    def _match_explicit(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        if ctx.intent_label == "转人工请求":
            return True, "意图标签=转人工请求"
        hit = _first_keyword(ctx.text, rule.keywords)
        if hit:
            return True, f"命中关键词「{hit}」"
        m = EXPLICIT_HANDOFF_RE.search(ctx.text)
        if m:
            return True, f"命中句式「{m.group(0)}」"
        return False, ""

    @staticmethod
    def _match_emotion(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        if ctx.negative_streak >= int(rule.threshold or 1):
            return True, f"连续负面 {ctx.negative_streak} 轮 ≥ {int(rule.threshold)}"
        if ctx.emotion is not None:
            return True, f"当前情绪={ctx.emotion}"
        hit = _first_keyword(ctx.text, rule.keywords)
        if hit:
            return True, f"命中情绪词「{hit}」"
        return False, ""

    @staticmethod
    def _match_unresolved(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        threshold = int(rule.threshold or 2)
        if ctx.unresolved_count >= threshold:
            return True, f"连续兜底 {ctx.unresolved_count} 轮 ≥ {threshold}"
        if ctx.turn_count > ctx.max_turns:
            return True, f"会话轮次 {ctx.turn_count} > 上限 {ctx.max_turns}"
        return False, ""

    @staticmethod
    def _match_high_risk(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        if ctx.intent_label == "投诉维权":
            return True, "意图标签=投诉维权"
        hit = _first_keyword(ctx.text, rule.keywords)
        if hit:
            return True, f"命中关键词「{hit}」"
        m = HIGH_RISK_RE.search(ctx.text)
        if m:
            return True, f"命中高风险句式「{m.group(0)}」"
        return False, ""

    @staticmethod
    def _match_no_evidence(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        if ctx.retrieval_decision != "NO_EVIDENCE":
            return False, ""
        if ctx.category is not None and ctx.category not in BUSINESS_CATEGORIES:
            return False, f"意图 {ctx.category.value} 非业务类，不转"
        if ctx.top_score >= float(rule.threshold or 0.55):
            return False, f"最高分 {ctx.top_score:.2f} ≥ {rule.threshold:.2f}"
        return True, f"检索无依据（最高分 {ctx.top_score:.2f} < {rule.threshold:.2f}）"

    @staticmethod
    def _match_system_error(rule: RuleSpec, ctx: "_JudgeContext") -> tuple[bool, str]:
        if ctx.system_error:
            return True, "本轮发生系统异常"
        return False, ""

    # -- 归一化 -------------------------------------------------------------

    @staticmethod
    def _norm_emotion(value: Emotion | str | None) -> str | None:
        """归一情绪：非负面一律返回 None（中性不触发规则②）。"""
        if value is None:
            return None
        raw = value.value if isinstance(value, Emotion) else str(value)
        return raw if raw in NEGATIVE_EMOTIONS else None

    @staticmethod
    def _norm_category(
        value: IntentCategory | str | None,
    ) -> IntentCategory | None:
        """归一意图大类；未知值返回 None（规则⑤按「业务类」放行）。"""
        if value is None:
            return None
        if isinstance(value, IntentCategory):
            return value
        try:
            return IntentCategory(str(value))
        except ValueError:
            return None


@dataclass
class _JudgeContext:
    """一次判定所需的全部输入（避免 `_match` 传一长串参数）。"""

    text: str
    intent_label: str
    category: IntentCategory | None
    emotion: str | None
    negative_streak: int
    unresolved_count: int
    turn_count: int
    retrieval_decision: str
    top_score: float
    system_error: bool
    max_turns: int


def _first_keyword(text: str, keywords: Iterable[str]) -> str | None:
    """返回第一个命中的关键词（保持词表顺序，便于解释）。"""
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
        if kw and kw.lower() in lowered:
            return kw
    return None


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_ENGINE: HandoffRuleEngine | None = None


def get_rule_engine() -> HandoffRuleEngine:
    """进程内规则引擎单例（首次调用时从 DB 加载，失败回退内置规则）。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = HandoffRuleEngine(load_rules())
    return _ENGINE


def set_rule_engine(engine: HandoffRuleEngine | None) -> None:
    """替换规则引擎单例（单测注入用；传 None 复位）。"""
    global _ENGINE
    _ENGINE = engine


__all__ = [
    "ALWAYS_ON_REASONS",
    "BUSINESS_CATEGORIES",
    "DEFAULT_RULES",
    "EXPLICIT_HANDOFF_RE",
    "HIGH_RISK_RE",
    "HandoffDecision",
    "HandoffRuleEngine",
    "NEGATIVE_EMOTIONS",
    "REASON_CN",
    "RuleHit",
    "RuleSpec",
    "get_rule_engine",
    "load_rules",
    "rules_from_rows",
    "set_rule_engine",
]
