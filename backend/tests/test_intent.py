"""AI-01 意图识别 / AI-04 情绪识别 单测。"""

from __future__ import annotations

import pytest

from app.core.enums import Emotion, IntentCategory
from app.services.intent import (
    CORPUS_EMOTION_MAP,
    CORPUS_INTENT_MAP,
    INTENT_LABELS,
    LOW_CONFIDENCE_THRESHOLD,
    EmotionDetector,
    IntentClassifier,
    get_emotion_detector,
    get_intent_classifier,
)


@pytest.fixture()
def clf() -> IntentClassifier:
    return IntentClassifier()


@pytest.fixture()
def det() -> EmotionDetector:
    return EmotionDetector()


# ---------------------------------------------------------------------------
# 标签体系
# ---------------------------------------------------------------------------


class TestLabelTaxonomy:
    def test_at_least_30_labels(self) -> None:
        assert len(INTENT_LABELS) >= 30

    def test_covers_6_categories(self) -> None:
        assert len(set(INTENT_LABELS.values())) == 6

    def test_every_label_maps_to_real_category(self) -> None:
        for label, cat in INTENT_LABELS.items():
            assert isinstance(cat, IntentCategory), label

    def test_corpus_maps_cover_all_rows(self) -> None:
        assert set(CORPUS_INTENT_MAP) == {
            "商品咨询",
            "订单服务",
            "售后服务",
            "营销活动",
            "品牌信任",
            "兜底",
        }
        assert set(CORPUS_EMOTION_MAP) == {"中性", "满意", "不满", "愤怒"}


# ---------------------------------------------------------------------------
# 意图分类
# ---------------------------------------------------------------------------


class TestIntentClassifier:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("这个能机洗吗", IntentCategory.PRODUCT_CONSULT),
            ("奶油小熊 20cm 和 30cm 哪个好", IntentCategory.PRODUCT_CONSULT),
            ("我的订单怎么还没发货", IntentCategory.ORDER_SERVICE),
            ("运单号三天没动了", IntentCategory.ORDER_SERVICE),
            ("我要退款", IntentCategory.AFTER_SALE),
            ("掉毛很严重是不是次品", IntentCategory.AFTER_SALE),
            ("有优惠券吗", IntentCategory.MARKETING),
            ("满减怎么算", IntentCategory.MARKETING),
            ("有没有 3C 认证", IntentCategory.BRAND_TRUST),
            ("地址写错了改一下", IntentCategory.ORDER_SERVICE),
        ],
    )
    def test_common_questions(
        self, clf: IntentClassifier, text: str, expected: IntentCategory
    ) -> None:
        assert clf.classify(text).category is expected

    def test_complaint_goes_to_fallback_not_after_sale(
        self, clf: IntentClassifier
    ) -> None:
        # 商家语料把「投诉/维权」都标成兜底：投诉是「机器人不该自己处理」
        # 的典型，必须直通人工。这条是刻意与直觉相反的业务约定。
        got = clf.classify("我要投诉你们，服务太差")
        assert got.category is IntentCategory.FALLBACK

    def test_human_request_goes_to_fallback(self, clf: IntentClassifier) -> None:
        got = clf.classify("转人工")
        assert got.category is IntentCategory.FALLBACK
        assert got.label == "转人工请求"

    def test_llm_hint_boosts_but_does_not_override_strong_evidence(
        self, clf: IntentClassifier
    ) -> None:
        # 强证据（投诉）应压过模型给出的错误提示
        got = clf.classify("我要投诉，把你们经理叫来", llm_hint="商品规格")
        assert got.category is IntentCategory.FALLBACK

    def test_llm_hint_applied_when_no_strong_evidence(
        self, clf: IntentClassifier
    ) -> None:
        got = clf.classify("嗯嗯", llm_hint="洗护方式")
        assert got.label == "洗护方式"

    def test_empty_text_returns_fallback(self, clf: IntentClassifier) -> None:
        got = clf.classify("")
        assert got.category is IntentCategory.FALLBACK
        assert got.confidence == 0.0

    def test_evidence_is_populated(self, clf: IntentClassifier) -> None:
        got = clf.classify("这个能机洗吗")
        assert got.evidence, "可解释性要求必须给出命中词"
        assert any("洗" in e for e in got.evidence)

    def test_confidence_between_0_and_1(self, clf: IntentClassifier) -> None:
        for text in ["能机洗吗", "转人工", "你好", "随便说点什么"]:
            c = clf.classify(text).confidence
            assert 0.0 <= c <= 1.0

    def test_low_confidence_threshold_constant(self) -> None:
        assert 0.0 < LOW_CONFIDENCE_THRESHOLD < 1.0

    def test_to_dict_shape(self, clf: IntentClassifier) -> None:
        d = clf.classify("这个能机洗吗").to_dict()
        assert set(d) == {"category", "label", "confidence", "evidence", "candidates"}

    def test_singleton(self) -> None:
        assert get_intent_classifier() is get_intent_classifier()


# ---------------------------------------------------------------------------
# 情绪识别
# ---------------------------------------------------------------------------


class TestEmotionDetector:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("谢谢，很满意", Emotion.NEUTRAL),
            ("这个怎么洗", Emotion.NEUTRAL),
            ("太慢了，我都等半天了", Emotion.DISSATISFIED),
            ("我要投诉你们，骗子", Emotion.ANGRY),
            ("什么破店，以后再也不会买了", Emotion.ANGRY),
            ("我要打 12315 举报", Emotion.ANGRY),
        ],
    )
    def test_three_way(
        self, det: EmotionDetector, text: str, expected: Emotion
    ) -> None:
        assert det.detect(text).emotion is expected

    def test_negated_satisfaction_is_not_positive(self) -> None:
        det = EmotionDetector()
        r = det.detect("我很不满意")
        assert r.emotion is not Emotion.NEUTRAL

    def test_neutral_query_words_do_not_trigger_negative(self) -> None:
        # 「还没发货」是查询不是抱怨——这是刻意从词表里剔除的词，
        # 因为误报会把中性用户直接推到 BE-13 规则②转人工。
        det = EmotionDetector()
        assert det.detect("订单2026****7168还没发货，我要改成新地址").emotion is (
            Emotion.NEUTRAL
        )

    def test_streak_triggers_at_three(self) -> None:
        det = EmotionDetector()
        streak = 0
        flags: list[int] = []
        for i in range(1, 6):
            r = det.detect("太慢了，我很不满意", prev_streak=streak)
            streak = r.negative_streak
            if r.should_flag:
                flags.append(i)
        assert flags and flags[0] == 3

    def test_positive_resets_streak(self) -> None:
        det = EmotionDetector()
        assert det.detect("谢谢，很满意", prev_streak=2).negative_streak == 0

    def test_to_dict_shape(self, det: EmotionDetector) -> None:
        d = det.detect("太慢了").to_dict()
        assert set(d) == {"emotion", "score", "negative_streak", "should_flag", "evidence"}

    def test_singleton(self) -> None:
        assert get_emotion_detector() is get_emotion_detector()

    def test_threshold_is_configurable(self) -> None:
        det = EmotionDetector()
        r = det.detect("太慢了", prev_streak=1, threshold=2)
        assert r.should_flag is True
