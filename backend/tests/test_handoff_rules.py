"""BE-13 · 转人工触发判定引擎单元测试。

覆盖 DoD：
* ① 六条规则**每条 ≥ 20 例**、正负例分离，命中率 100%；
* ② 仲裁顺序（优先级降序、同优先级 rule_id 升序）；
* ③ 规则①④ 不可停用；
* ④ 判定 P95 ≤ 50ms；
* ⑤ 与 `orchestrator.decide_action` 在核心用例集上结论一致。

**为什么正负例要成对写**：转人工规则最容易出的错不是「该转没转」，
而是「不该转却转了」——后者会把正常咨询的用户推给人工，白烧坐席，
而且用户会觉得机器人很蠢。所以每条规则都配了反例集。
"""

from __future__ import annotations

import time

import pytest

from app.core.enums import Emotion, HandoffReason, IntentCategory, Priority
from app.services.handoff.rules import (
    ALWAYS_ON_REASONS,
    DEFAULT_RULES,
    HandoffRuleEngine,
    RuleSpec,
    rules_from_rows,
)
from app.services.intent import IntentResult
from app.services.orchestrator import decide_action
from app.services.session import SessionState

# ---------------------------------------------------------------------------
# 语料：每条规则 ≥ 20 条正例
# ---------------------------------------------------------------------------

#: 规则① 正例（显式要人工）
EXPLICIT_POSITIVES: list[str] = [
    "转人工",
    "我要转人工",
    "请转人工客服",
    "人工客服在吗",
    "人工客服在么",
    "找人工",
    "帮我找人工",
    "我要真人客服",
    "找个真人",
    "找个活人",
    "给我转个人",
    "换人工",
    "要人工",
    "找个人来",
    "真人呢",
    "叫你们经理来",
    "叫经理",
    "把经理叫来",
    "不要机器人",
    "智能客服不行",
    "转接人工",
    "接人工",
    "我要找人",
]

#: 规则① 反例（含「人/人工」但不是要人工）
EXPLICIT_NEGATIVES: list[str] = [
    "这个人偶多少钱",
    "人工费怎么算",
    "真人版玩偶有吗",
    "我要买这个",
    "这个材质是什么",
    "发货要多久",
    "能便宜点吗",
    "有没有优惠券",
    "支持七天无理由吗",
    "怎么申请退款",
    "我的快递到哪了",
    "这个有现货吗",
    "可以开发票吗",
    "包邮吗",
    "尺码怎么选",
    "什么材质做的",
    "颜色有几个",
    "什么时候补货",
    "怎么联系你们",
    "有赠品吗",
    "会员有什么权益",
]

#: 规则② 正例（负面情绪）
EMOTION_POSITIVES: list[str] = [
    "等了好几天了",
    "太慢了",
    "我很生气",
    "这也太差了吧",
    "你们怎么回事",
    "我很不满意",
    "气死我了",
    "什么玩意",
    "太恶心了",
    "无语",
    "垃圾服务",
    "差评",
    "假货",
    "你们就是骗子",
    "完全不满意",
    "很愤怒",
    "这体验太糟糕",
    "我要气炸了",
    "太失望了",
    "受不了了",
    "真差劲",
    "气人",
]

#: 规则② 反例（中性提问）
EMOTION_NEGATIVES: list[str] = [
    "这个多少钱",
    "什么时候发货",
    "支持退货吗",
    "有现货吗",
    "怎么使用",
    "包邮吗",
    "尺码表在哪",
    "能开发票吗",
    "有优惠吗",
    "快递几天到",
    "材质是什么",
    "颜色有哪些",
    "可以换货吗",
    "怎么保养",
    "有保修吗",
    "适合多大年龄",
    "重量多少",
    "产地哪里",
    "怎么下单",
    "有没有赠品",
    "会员怎么开通",
    "能指定快递吗",
]

#: 规则③ 正例（连续未解决）
UNRESOLVED_POSITIVES: list[str] = [
    "我还是没懂",
    "你再说一遍",
    "还是不行",
    "没解决啊",
    "答非所问",
    "你没回答我的问题",
    "这跟我的问题没关系",
    "还是那个问题",
    "没用",
    "能不能认真点",
    "我说的是另一件事",
    "重复了",
    "不是这个意思",
    "你理解错了",
    "还是不明白",
    "再解释一下",
    "这不算回答",
    "我要的是别的",
    "换个说法",
    "根本没解决",
    "还是不清楚",
    "你的回答不对",
]

#: 规则③ 反例
UNRESOLVED_NEGATIVES: list[str] = [
    "谢谢",
    "明白了",
    "好的我知道了",
    "那再问一个",
    "这个多少钱",
    "什么时候发货",
    "有现货吗",
    "包邮吗",
    "怎么退货",
    "能开发票吗",
    "尺码怎么选",
    "颜色有哪些",
    "材质是什么",
    "几天到",
    "有优惠吗",
    "怎么保养",
    "有保修吗",
    "产地哪里",
    "怎么下单",
    "有赠品吗",
    "会员权益",
    "能指定快递吗",
]

#: 规则④ 正例（高风险）
HIGH_RISK_POSITIVES: list[str] = [
    "我要投诉",
    "投诉你们",
    "我要举报",
    "打 12315 举报",
    "打 12315",
    "找消协",
    "找消保委",
    "找市场监管局",
    "去工商局告你们",
    "我要曝光你们",
    "找媒体曝光",
    "联系记者",
    "去法院起诉",
    "我要打官司",
    "发律师函",
    "我要报警",
    "我要立案",
    "你们这是欺诈",
    "我要赔偿",
    "假一赔十",
    "职业打假的来了",
    "我要走法律程序",
]

#: 规则④ 反例
HIGH_RISK_NEGATIVES: list[str] = [
    "这个多少钱",
    "什么时候发货",
    "支持退货吗",
    "有现货吗",
    "怎么使用",
    "包邮吗",
    "尺码表在哪",
    "能开发票吗",
    "有优惠吗",
    "快递几天到",
    "材质是什么",
    "颜色有哪些",
    "可以换货吗",
    "怎么保养",
    "有保修吗",
    "适合多大年龄",
    "重量多少",
    "产地哪里",
    "怎么下单",
    "有没有赠品",
    "会员怎么开通",
    "能指定快递吗",
]

#: 规则⑤ 正例（无依据 + 业务类）
NO_EVIDENCE_CATEGORIES: list[IntentCategory] = [
    IntentCategory.PRODUCT_CONSULT,
    IntentCategory.AFTER_SALE,
    IntentCategory.BRAND_TRUST,
]

#: 规则⑥ 正例（系统异常）
SYSTEM_ERROR_POSITIVES: list[str] = [
    "这个多少钱",
    "什么时候发货",
    "支持退货吗",
    "有现货吗",
    "怎么使用",
    "包邮吗",
    "尺码表在哪",
    "能开发票吗",
    "有优惠吗",
    "快递几天到",
    "材质是什么",
    "颜色有哪些",
    "可以换货吗",
    "怎么保养",
    "有保修吗",
    "适合多大年龄",
    "重量多少",
    "产地哪里",
    "怎么下单",
    "有没有赠品",
    "会员怎么开通",
    "能指定快递吗",
]


def _engine() -> HandoffRuleEngine:
    """用内置默认规则构造引擎（不连 DB）。"""
    return HandoffRuleEngine(DEFAULT_RULES)


def _rule(rule_id: int) -> RuleSpec:
    """取指定规则。"""
    return next(r for r in DEFAULT_RULES if r.rule_id == rule_id)


# ---------------------------------------------------------------------------
# 规则① 显式请求
# ---------------------------------------------------------------------------


class TestRuleExplicitRequest:
    """规则①：用户显式请求人工（USER_REQUEST，优先级 100，不可停用）。"""

    @pytest.mark.parametrize("text", EXPLICIT_POSITIVES)
    def test_positive(self, text: str) -> None:
        """显式要人工的 23 种说法必须全部命中规则①。"""
        decision = _engine().judge(text=text)
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.USER_REQUEST, text
        assert decision.rule_id == 1, text

    @pytest.mark.parametrize("text", EXPLICIT_NEGATIVES)
    def test_negative(self, text: str) -> None:
        """含「人/人工」但不是要人工的说法**不得**触发规则①。"""
        decision = _engine().judge(
            text=text, intent_category=IntentCategory.PRODUCT_CONSULT
        )
        assert 1 not in decision.matched_rules, text

    def test_always_on(self) -> None:
        """规则① 不可停用：即使构造时传 enabled=False 也会被纠正为 True。"""
        rule = RuleSpec(
            rule_id=1,
            name="用户显式请求人工",
            reason=HandoffReason.USER_REQUEST,
            priority=100,
            enabled=False,
        )
        assert rule.enabled is True
        assert HandoffReason.USER_REQUEST in ALWAYS_ON_REASONS


# ---------------------------------------------------------------------------
# 规则② 情绪激动
# ---------------------------------------------------------------------------


class TestRuleEmotion:
    """规则②：用户情绪激动（EMOTION，优先级 90）。"""

    @pytest.mark.parametrize("text", EMOTION_POSITIVES)
    def test_positive_by_emotion_flag(self, text: str) -> None:
        """分类器判为负面情绪即触发（不依赖词表）。"""
        decision = _engine().judge(text=text, emotion=Emotion.ANGRY)
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.EMOTION, text

    def test_positive_by_negative_streak(self) -> None:
        """连续 3 轮负面（阈值 3.0）触发。"""
        decision = _engine().judge(text="还是不行", negative_streak=3)
        assert decision.should_handoff is True
        assert decision.reason is HandoffReason.EMOTION

    @pytest.mark.parametrize("text", EMOTION_NEGATIVES)
    def test_negative(self, text: str) -> None:
        """中性提问、情绪中性**不得**触发规则②。"""
        decision = _engine().judge(
            text=text,
            emotion=Emotion.NEUTRAL,
            intent_category=IntentCategory.PRODUCT_CONSULT,
        )
        assert 2 not in decision.matched_rules, text

    def test_neutral_emotion_not_triggered(self) -> None:
        """中性情绪即便文本带「等了好几天」也不按情绪转（由规则③兜）。"""
        decision = _engine().judge(text="等了好几天了", emotion=Emotion.NEUTRAL)
        assert 2 not in decision.matched_rules


# ---------------------------------------------------------------------------
# 规则③ 连续未解决
# ---------------------------------------------------------------------------


class TestRuleUnresolved:
    """规则③：连续未解决（UNRESOLVED，优先级 80，阈值 2 轮 / 轮次 > 10）。"""

    @pytest.mark.parametrize("text", UNRESOLVED_POSITIVES)
    def test_positive_by_streak(self, text: str) -> None:
        """连续兜底 2 轮即触发。"""
        decision = _engine().judge(
            text=text,
            unresolved_count=2,
            intent_category=IntentCategory.PRODUCT_CONSULT,
        )
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.UNRESOLVED, text

    @pytest.mark.parametrize("text", UNRESOLVED_POSITIVES[:12])
    def test_positive_by_turn_limit(self, text: str) -> None:
        """会话轮次超过 10 轮即触发。"""
        decision = _engine().judge(
            text=text, turn_count=11, intent_category=IntentCategory.PRODUCT_CONSULT
        )
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.UNRESOLVED, text

    @pytest.mark.parametrize("text", UNRESOLVED_NEGATIVES)
    def test_negative(self, text: str) -> None:
        """只差 1 轮、轮次未超限时**不得**触发规则③。"""
        decision = _engine().judge(
            text=text,
            unresolved_count=1,
            turn_count=3,
            intent_category=IntentCategory.PRODUCT_CONSULT,
        )
        assert 3 not in decision.matched_rules, text


# ---------------------------------------------------------------------------
# 规则④ 高风险
# ---------------------------------------------------------------------------


class TestRuleHighRisk:
    """规则④：高风险意图（HIGH_RISK，优先级 100，不可停用）。"""

    @pytest.mark.parametrize("text", HIGH_RISK_POSITIVES)
    def test_positive(self, text: str) -> None:
        """投诉/举报/法律/媒体曝光必须转升级组。"""
        decision = _engine().judge(text=text)
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.HIGH_RISK, text
        assert decision.rule_id == 4, text
        assert decision.priority is Priority.URGENT, text

    @pytest.mark.parametrize("text", HIGH_RISK_NEGATIVES)
    def test_negative(self, text: str) -> None:
        """普通咨询**不得**触发规则④。"""
        decision = _engine().judge(
            text=text, intent_category=IntentCategory.PRODUCT_CONSULT
        )
        assert 4 not in decision.matched_rules, text

    def test_always_on(self) -> None:
        """规则④ 不可停用。"""
        rule = RuleSpec(
            rule_id=4,
            name="高风险意图",
            reason=HandoffReason.HIGH_RISK,
            priority=100,
            enabled=False,
        )
        assert rule.enabled is True


# ---------------------------------------------------------------------------
# 规则⑤ 检索无依据
# ---------------------------------------------------------------------------


class TestRuleNoEvidence:
    """规则⑤：检索无依据（UNRESOLVED，优先级 60，阈值 0.55）。"""

    @pytest.mark.parametrize("category", NO_EVIDENCE_CATEGORIES)
    @pytest.mark.parametrize(
        "text",
        [
            "这个支持以旧换新吗",
            "你们的售后政策是什么",
            "这个品牌有官方授权吗",
            "可以定制吗",
            "有线下门店吗",
            "这个和另一个有什么区别",
            "能刻字吗",
            "退货运费谁出",
            "这个材质环保吗",
            "支持货到付款吗",
            "有没有质检报告",
            "能开专票吗",
            "这个型号停产了吗",
            "有配套的配件吗",
            "保修多久",
            "可以分期吗",
            "这个尺寸多大",
            "有隐藏款吗",
            "能预定吗",
            "这个是不是正品",
        ],
    )
    def test_positive(self, text: str, category: IntentCategory) -> None:
        """业务类问题无依据必须转（不得编造）。"""
        decision = _engine().judge(
            text=text,
            intent_category=category,
            retrieval_decision="NO_EVIDENCE",
            top_score=0.2,
        )
        assert decision.should_handoff is True, (text, category)
        assert decision.reason is HandoffReason.UNRESOLVED, (text, category)

    @pytest.mark.parametrize(
        "category",
        [IntentCategory.ORDER_SERVICE, IntentCategory.MARKETING, IntentCategory.FALLBACK],
    )
    def test_negative_non_business(self, category: IntentCategory) -> None:
        """订单/营销/闲聊无依据**不转**（有模板话术兜底）。"""
        decision = _engine().judge(
            text="这个支持以旧换新吗",
            intent_category=category,
            retrieval_decision="NO_EVIDENCE",
            top_score=0.2,
        )
        assert 5 not in decision.matched_rules

    def test_negative_high_score(self) -> None:
        """检索有依据（分数够高）时**不转**。"""
        decision = _engine().judge(
            text="这个支持以旧换新吗",
            intent_category=IntentCategory.AFTER_SALE,
            retrieval_decision="ANSWER",
            top_score=0.86,
        )
        assert 5 not in decision.matched_rules

    def test_negative_without_no_evidence(self) -> None:
        """决策不是 NO_EVIDENCE 时，即便分数低也不按规则⑤转。"""
        decision = _engine().judge(
            text="这个支持以旧换新吗",
            intent_category=IntentCategory.AFTER_SALE,
            retrieval_decision="CLARIFY",
            top_score=0.1,
        )
        assert 5 not in decision.matched_rules


# ---------------------------------------------------------------------------
# 规则⑥ 系统异常
# ---------------------------------------------------------------------------


class TestRuleSystemError:
    """规则⑥：系统异常（SYSTEM_ERROR，优先级 50）。"""

    @pytest.mark.parametrize("text", SYSTEM_ERROR_POSITIVES)
    def test_positive(self, text: str) -> None:
        """任何一轮发生系统异常都要转人工兜底。"""
        decision = _engine().judge(
            text=text,
            intent_category=IntentCategory.PRODUCT_CONSULT,
            system_error=True,
        )
        assert decision.should_handoff is True, text
        assert decision.reason is HandoffReason.SYSTEM_ERROR, text
        assert decision.rule_id == 6, text

    def test_negative(self) -> None:
        """无异常时规则⑥不命中。"""
        decision = _engine().judge(
            text="这个多少钱", intent_category=IntentCategory.PRODUCT_CONSULT
        )
        assert 6 not in decision.matched_rules


# ---------------------------------------------------------------------------
# 仲裁
# ---------------------------------------------------------------------------


class TestArbitration:
    """规则仲裁：优先级降序，同优先级 rule_id 升序。"""

    def test_explicit_beats_high_risk(self) -> None:
        """「转人工，我要投诉」→ 规则①（USER_REQUEST），不是规则④。"""
        decision = _engine().judge(text="转人工，我要投诉")
        assert decision.reason is HandoffReason.USER_REQUEST
        assert decision.rule_id == 1
        # 两条都命中，仲裁取 rule_id 小的
        assert {1, 4} <= set(decision.matched_rules)

    def test_high_risk_beats_emotion(self) -> None:
        """投诉 + 情绪负面 → HIGH_RISK（优先级 100 > 90）。"""
        decision = _engine().judge(text="我要投诉", emotion=Emotion.ANGRY)
        assert decision.reason is HandoffReason.HIGH_RISK
        assert 4 in decision.matched_rules
        assert 2 in decision.matched_rules

    def test_high_risk_beats_unresolved(self) -> None:
        """投诉 + 轮次超限 → HIGH_RISK。"""
        decision = _engine().judge(text="我要投诉", turn_count=12)
        assert decision.reason is HandoffReason.HIGH_RISK

    def test_system_error_lowest_priority(self) -> None:
        """系统异常 + 显式请求 → USER_REQUEST（100 > 50）。"""
        decision = _engine().judge(text="转人工", system_error=True)
        assert decision.reason is HandoffReason.USER_REQUEST

    def test_no_handoff_returns_none_reason(self) -> None:
        """完全不触发时 `should_handoff=False`、`reason=None`。"""
        decision = _engine().judge(
            text="这个多少钱", intent_category=IntentCategory.PRODUCT_CONSULT
        )
        assert decision.should_handoff is False
        assert decision.reason is None
        assert decision.rule_id is None

    def test_disabled_rule_skipped(self) -> None:
        """停用的规则不参与仲裁（规则②停用后情绪不再触发）。"""
        rules = tuple(
            RuleSpec(
                rule_id=r.rule_id,
                name=r.name,
                reason=r.reason,
                priority=r.priority,
                threshold=r.threshold,
                keywords=r.keywords,
                enabled=False if r.rule_id == 2 else r.enabled,
                description=r.description,
            )
            for r in DEFAULT_RULES
        )
        decision = HandoffRuleEngine(rules).judge(
            text="等了好几天了", emotion=Emotion.ANGRY
        )
        assert 2 not in decision.matched_rules

    def test_decision_to_dict_shape(self) -> None:
        """`to_dict()` 结构稳定（前端「为什么转」面板直接消费）。"""
        payload = _engine().judge(text="我要投诉").to_dict()
        assert set(payload) >= {
            "should_handoff",
            "reason",
            "rule_id",
            "rule_name",
            "priority",
            "matched_rules",
            "hits",
            "latency_ms",
        }
        assert payload["reason"] == "HIGH_RISK"
        assert payload["priority"] == "URGENT"
        assert len(payload["hits"]) == 6


# ---------------------------------------------------------------------------
# 加载 / 性能 / 与编排层一致性
# ---------------------------------------------------------------------------


class TestLoading:
    """规则加载与脏数据容错。"""

    def test_rules_from_rows(self) -> None:
        """DB 行 → `RuleSpec`，未知 reason 的行被跳过。"""
        rows = [
            {
                "rule_id": 1,
                "name": "显式",
                "reason": "USER_REQUEST",
                "priority": 100,
                "threshold": 0,
                "keywords": ["转人工"],
                "enabled": True,
                "description": "",
            },
            {
                "rule_id": 9,
                "name": "脏数据",
                "reason": "NOT_A_REASON",
                "priority": 1,
            },
        ]
        rules = rules_from_rows(rows)
        assert len(rules) == 1
        assert rules[0].rule_id == 1
        assert rules[0].keywords == ("转人工",)

    def test_rules_from_rows_string_keywords(self) -> None:
        """关键词以逗号字符串存储时也能解析。"""
        rules = rules_from_rows(
            [
                {
                    "rule_id": 4,
                    "name": "高风险",
                    "reason": "HIGH_RISK",
                    "priority": 100,
                    "keywords": "投诉,举报",
                }
            ]
        )
        assert rules[0].keywords == ("投诉", "举报")

    def test_always_on_corrected_from_rows(self) -> None:
        """DB 里把①④ 置为停用，加载时也会被纠正为启用。"""
        rules = rules_from_rows(
            [
                {
                    "rule_id": 1,
                    "name": "显式",
                    "reason": "USER_REQUEST",
                    "priority": 100,
                    "enabled": False,
                }
            ]
        )
        assert rules[0].enabled is True


class TestPerformance:
    """DoD④：判定 P95 ≤ 50ms。"""

    def test_p95_under_50ms(self) -> None:
        """连续 200 次判定，P95 必须远低于 50ms。"""
        engine = _engine()
        samples: list[float] = []
        for i in range(200):
            started = time.perf_counter()
            engine.judge(text=f"这个多少钱 {i}", intent_category=IntentCategory.PRODUCT_CONSULT)
            samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95)]
        assert p95 < 50.0, f"P95={p95:.2f}ms 超出预算"


class TestEngineAgreesWithOrchestrator:
    """引擎与 `orchestrator.decide_action` 在核心用例集上结论一致。"""

    @staticmethod
    def _orchestrator(
        text: str,
        category: IntentCategory,
        *,
        label: str = "通用",
        emotion_negative: bool = False,
        retrieval_decision: str | None = None,
        turn_count: int = 0,
    ):
        return decide_action(
            text=text,
            intent=IntentResult(category=category, label=label, confidence=0.9),
            state=SessionState(session_id="s", turn_count=turn_count),
            emotion_negative=emotion_negative,
            retrieval_decision=retrieval_decision,
        )

    @pytest.mark.parametrize("text", EXPLICIT_POSITIVES[:10])
    def test_explicit(self, text: str) -> None:
        """显式请求：两边都转 USER_REQUEST。"""
        engine = _engine().judge(text=text)
        orch = self._orchestrator(text, IntentCategory.FALLBACK)
        assert engine.reason is HandoffReason.USER_REQUEST
        assert orch.handoff_reason is HandoffReason.USER_REQUEST

    @pytest.mark.parametrize("text", HIGH_RISK_POSITIVES[:10])
    def test_high_risk(self, text: str) -> None:
        """高风险：两边都转 HIGH_RISK。"""
        engine = _engine().judge(text=text)
        orch = self._orchestrator(text, IntentCategory.FALLBACK)
        assert engine.reason is HandoffReason.HIGH_RISK
        assert orch.handoff_reason is HandoffReason.HIGH_RISK

    @pytest.mark.parametrize("text", EXPLICIT_NEGATIVES[:8])
    def test_plain_consult(self, text: str) -> None:
        """普通咨询：两边都不转。"""
        engine = _engine().judge(
            text=text, intent_category=IntentCategory.PRODUCT_CONSULT
        )
        orch = self._orchestrator(text, IntentCategory.PRODUCT_CONSULT)
        assert engine.should_handoff is False, text
        assert orch.handoff_reason is None, text

    def test_emotion(self) -> None:
        """情绪负面：两边都转 EMOTION。"""
        engine = _engine().judge(text="等了好几天了", emotion=Emotion.DISSATISFIED)
        orch = self._orchestrator(
            "等了好几天了", IntentCategory.ORDER_SERVICE, emotion_negative=True
        )
        assert engine.reason is HandoffReason.EMOTION
        assert orch.handoff_reason is HandoffReason.EMOTION

    def test_turn_limit(self) -> None:
        """轮次超限：两边都转 UNRESOLVED。"""
        engine = _engine().judge(text="我还是没懂", turn_count=11)
        orch = self._orchestrator(
            "我还是没懂", IntentCategory.PRODUCT_CONSULT, turn_count=11
        )
        assert engine.reason is HandoffReason.UNRESOLVED
        assert orch.handoff_reason is HandoffReason.UNRESOLVED

    @pytest.mark.parametrize("category", NO_EVIDENCE_CATEGORIES)
    def test_no_evidence_business(self, category: IntentCategory) -> None:
        """业务类无依据：两边都转 UNRESOLVED。"""
        engine = _engine().judge(
            text="这个支持以旧换新吗",
            intent_category=category,
            retrieval_decision="NO_EVIDENCE",
            top_score=0.2,
        )
        orch = self._orchestrator(
            "这个支持以旧换新吗", category, retrieval_decision="NO_EVIDENCE"
        )
        assert engine.reason is HandoffReason.UNRESOLVED
        assert orch.handoff_reason is HandoffReason.UNRESOLVED

    @pytest.mark.parametrize(
        "category",
        [IntentCategory.ORDER_SERVICE, IntentCategory.MARKETING, IntentCategory.FALLBACK],
    )
    def test_no_evidence_non_business(self, category: IntentCategory) -> None:
        """非业务类无依据：两边都不转。"""
        engine = _engine().judge(
            text="今天天气不错",
            intent_category=category,
            retrieval_decision="NO_EVIDENCE",
            top_score=0.2,
        )
        orch = self._orchestrator(
            "今天天气不错", category, retrieval_decision="NO_EVIDENCE"
        )
        assert engine.should_handoff is False
        assert orch.handoff_reason is None
