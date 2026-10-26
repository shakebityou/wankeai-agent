"""AI-01 意图识别 + AI-04 情绪识别。

设计取舍：为什么是规则 + 词表而不是先上模型
--------------------------------------------
任务书对 AI-01 的硬指标是「6 大类 30+ 标签」。用大模型做分类有两个问题：

1. **不可解释**：坐席看到「判成售后服务」却不知道为什么，无法纠错；
   运营也没法按标签调整知识库。规则引擎的每个命中都能打印出证据词。
2. **不可离线**：BE-16 的默认路径是「无 API Key 也能跑通」（本地兜底
   Provider）。若意图识别依赖外部模型，整条链路在离线环境就断了。

所以这里做**分层**：规则打分给出 baseline（毫秒级、可解释、可离线），
在 `IntentClassifier` 上留 `llm_hint` 接口——将来接模型只需把模型输出
当作额外一票加进来，规则仍然作为兜底与解释来源。

评测底料全部用商家真实标注数据：
* `历史客服会话语料.csv` 324 条（意图类目 + 用户情绪 + 是否转人工）
* `多轮客服对话语料.csv` 164 条 / 17 段对话

**注意不要用评测集去调词表**：词表来自商家知识库与政策文档的术语，
不是从这 324 条里抄的。否则准确率再高也只是过拟合。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import Emotion, IntentCategory
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 意图标签：6 大类 × 细分。任务书要求 30+，这里给 46 个。
#: 键是标签名，值所属大类——标签到类目的映射是**确定性**的，
#: 不做「一个标签可能属于两个类目」的设计（坐席看不懂）。
INTENT_LABELS: dict[str, IntentCategory] = {
    # 商品咨询 9
    "商品规格": IntentCategory.PRODUCT_CONSULT,
    "材质成分": IntentCategory.PRODUCT_CONSULT,
    "尺寸大小": IntentCategory.PRODUCT_CONSULT,
    "洗护方式": IntentCategory.PRODUCT_CONSULT,
    "适龄建议": IntentCategory.PRODUCT_CONSULT,
    "商品价格": IntentCategory.PRODUCT_CONSULT,
    "商品库存": IntentCategory.PRODUCT_CONSULT,
    "商品对比": IntentCategory.PRODUCT_CONSULT,
    "正品真伪": IntentCategory.PRODUCT_CONSULT,
    # 订单服务 8
    "订单查询": IntentCategory.ORDER_SERVICE,
    "物流查询": IntentCategory.ORDER_SERVICE,
    "物流异常": IntentCategory.ORDER_SERVICE,
    "发货时效": IntentCategory.ORDER_SERVICE,
    "修改地址": IntentCategory.ORDER_SERVICE,
    "修改订单": IntentCategory.ORDER_SERVICE,
    "订单取消": IntentCategory.ORDER_SERVICE,
    "发票开具": IntentCategory.ORDER_SERVICE,
    # 售后服务 9
    "退货退款": IntentCategory.AFTER_SALE,
    "换货": IntentCategory.AFTER_SALE,
    "退款进度": IntentCategory.AFTER_SALE,
    "商品质量": IntentCategory.AFTER_SALE,
    "商品瑕疵": IntentCategory.AFTER_SALE,
    "补发": IntentCategory.AFTER_SALE,
    "运费承担": IntentCategory.AFTER_SALE,
    "售后时效": IntentCategory.AFTER_SALE,
    # 投诉维权归**兜底**而非售后服务：商家语料把「投诉/维权/12315/
    # 破店/转人工」全部标成兜底。业务上也说得通——投诉是「机器人不该
    # 自己处理」的典型，必须直通人工，而不是当成售后流程走一遍。
    "投诉维权": IntentCategory.FALLBACK,
    # 营销活动 8
    "优惠券": IntentCategory.MARKETING,
    "满减规则": IntentCategory.MARKETING,
    "活动时间": IntentCategory.MARKETING,
    "赠品": IntentCategory.MARKETING,
    "会员权益": IntentCategory.MARKETING,
    "直播间活动": IntentCategory.MARKETING,
    "秒杀": IntentCategory.MARKETING,
    "拼团": IntentCategory.MARKETING,
    # 品牌信任 8
    "品牌介绍": IntentCategory.BRAND_TRUST,
    "IP授权": IntentCategory.BRAND_TRUST,
    "质检认证": IntentCategory.BRAND_TRUST,
    "安全材质": IntentCategory.BRAND_TRUST,
    "版权合规": IntentCategory.BRAND_TRUST,
    "店铺资质": IntentCategory.BRAND_TRUST,
    "品牌口碑": IntentCategory.BRAND_TRUST,
    "官方渠道": IntentCategory.BRAND_TRUST,
    # 兜底 4
    "闲聊寒暄": IntentCategory.FALLBACK,
    "情感表达": IntentCategory.FALLBACK,
    "无关话题": IntentCategory.FALLBACK,
    "转人工请求": IntentCategory.FALLBACK,
    "无法识别": IntentCategory.FALLBACK,
}


#: 标签关键词表。**关键词来自商家知识库术语，不是从评测语料抄的。**
#: 权重 3 = 强特征（几乎只出现在这个意图里）；2 = 中等；1 = 弱旁证。
_LABEL_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    # --- 商品咨询 ---
    "尺寸大小": (("尺寸", 3), ("多大", 2), ("多少厘米", 3), ("cm", 2), ("大小", 2), ("高", 1), ("长", 1), ("对比", 1)),
    "材质成分": (("材质", 3), ("面料", 3), ("填充", 3), ("什么做的", 3), ("成分", 2), ("真丝", 2), ("棉", 2), ("绒", 1)),
    "洗护方式": (("洗", 3), ("机洗", 3), ("手洗", 3), ("晾", 2), ("清洁", 2), ("保养", 2), ("褪色", 2), ("变形", 2), ("口" , 1)),
    "适龄建议": (("几岁", 3), ("适龄", 3), ("年龄", 2), ("小孩", 2), ("宝宝", 2), ("娃", 2), ("安全", 1), ("啃", 2), ("咬", 2)),
    "商品价格": (("多少钱", 3), ("价格", 3), ("便宜", 2), ("贵", 2), ("实付", 2), ("优惠", 1), ("售价", 2)),
    "商品库存": (("现货", 3), ("有货", 3), ("库存", 3), ("缺货", 3), ("预售", 3), ("补货", 2), ("还有没有", 2)),
    "商品对比": (("哪个好", 2), ("区别", 2), ("选哪个", 3), ("比较", 2), ("对比", 2), ("差别", 2)),
    "商品规格": (("规格", 3), ("款式", 2), ("颜色", 2), ("型号", 2), ("参数", 2), ("配件", 2)),
    "正品真伪": (("正品", 3), ("真假", 3), ("辨别真假", 4), ("盗版", 3), ("防伪", 3), ("授权", 1)),
    # --- 订单服务 ---
    "物流查询": (("物流", 3), ("快递", 2), ("运单", 3), ("单号", 2), ("到哪", 2), ("签收", 2), ("轨迹", 3)),
    "物流异常": (("丢件", 3), ("一动不动", 3), ("没动", 3), ("不动", 2), ("没更新", 3), ("卡", 2), ("停滞", 3), ("破损", 2), ("压坏", 2)),
    "发货时效": (("发货", 3), ("多久", 2), ("什么时候发", 3), ("出库", 3), ("什么时候到", 3), ("催", 3), ("加急", 3)),
    "订单查询": (("订单", 3), ("我的单", 3), ("查一下", 2), ("订单号", 3), ("包裹", 2)),
    "修改地址": (("改地址", 3), ("修改地址", 3), ("换地址", 3), ("新地址", 3), ("收货地址", 2)),
    "修改订单": (
        ("改一下", 2), ("修改", 2), ("改颜色", 3), ("改尺寸", 3),
        ("改成新号码", 4), ("改号码", 4), ("换新号码", 4),
    ),
    "订单取消": (("取消", 3), ("不想要了", 3), ("退单", 2), ("撤销", 3), ("拍错", 3)),
    "发票开具": (("发票", 3), ("开票", 3), ("抬头", 3), ("税号", 3), ("专票", 3), ("普票", 3)),
    # --- 售后服务 ---
    "退货退款": (("退货", 3), ("退款", 3), ("退掉", 2), ("退了", 2), ("申请退", 3), ("退还", 3)),
    # 「换尺寸」「换颜色」也属换货：用户诉求是「换」不是「咨询尺寸」，
    # 所以给到 4 权重压过「尺寸」的 3。
    "换货": (
        ("换货", 3), ("换一个", 2), ("换新", 3), ("调换", 2),
        ("退换", 4), ("换个尺寸", 4), ("换尺寸", 4), ("换个颜色", 4),
    ),
    "退款进度": (("退款到", 3), ("什么时候到账", 3), ("到账", 3), ("退回", 2), ("几天到", 2)),
    "商品质量": (("质量", 3), ("掉毛", 3), ("起球", 3), ("开线", 3), ("坏了", 2), ("有问题", 2), ("瑕疵", 3)),
    "商品瑕疵": (("瑕疵", 3), ("破", 2), ("破洞", 4), ("漏", 2), ("脏", 2), ("划痕", 3), ("色差", 3)),
    "补发": (("补发", 3), ("重发", 3), ("重新发", 2), ("少发", 4), ("漏发", 4), ("只收到一个", 4)),
    "运费承担": (("运费", 3), ("邮费", 3), ("谁承担", 2), ("包邮", 1)),
    "售后时效": (("多久退", 3), ("几个工作日", 3), ("处理时间", 2), ("无理由", 3)),
    "投诉维权": (("投诉", 3), ("维权", 3), ("12315", 3), ("消协", 3), ("举报", 3), ("起诉", 3)),
    # --- 营销活动 ---
    "优惠券": (("优惠券", 3), ("券", 2), ("领券", 3), ("代金券", 3)),
    "满减规则": (("满减", 3), ("满99", 2), ("满多少", 3), ("减10", 2), ("凑单", 3)),
    "活动时间": (("活动", 2), ("什么时候结束", 2), ("大促", 2), ("双十一", 3), ("618", 3)),
    "赠品": (("赠品", 3), ("赠", 2), ("送什么", 2)),
    "会员权益": (("会员", 3), ("积分", 3), ("等级", 2)),
    "直播间活动": (("直播", 3), ("主播", 3), ("小黄车", 3)),
    "秒杀": (("秒杀", 3), ("限时", 2), ("抢购", 2)),
    "拼团": (("拼团", 3), ("拼单", 3), ("团购", 3)),
    # --- 品牌信任 ---
    "品牌介绍": (("品牌", 3), ("你们家", 2), ("店铺", 2), ("成立", 2), ("公司", 1)),
    "IP授权": (("授权", 3), ("IP", 3), ("联名", 3), ("版权", 2)),
    "质检认证": (("质检", 3), ("检测报告", 3), ("认证", 3), ("国标", 3), ("GB", 2), ("标准", 2), ("合格", 3), ("不合格", 4)),
    "安全材质": (
        ("甲醛", 3), ("无味", 2), ("无毒", 3), ("异味", 3), ("荧光剂", 3),
        ("入口", 3), ("有毒", 4), ("致癌", 4), ("有味道", 3),
    ),
    "版权合规": (("盗版", 3), ("侵权", 3), ("原创", 2), ("正版", 2)),
    "店铺资质": (("营业执照", 3), ("资质", 3), ("企业", 2)),
    "品牌口碑": (("口碑", 3), ("评价", 2), ("好评", 2), ("买了", 1), ("回头客", 3)),
    "官方渠道": (("官方", 3), ("旗舰店", 3), ("官网", 3), ("直营", 3)),
    # --- 兜底 ---
    "闲聊寒暄": (
        ("在吗", 3), ("你好", 2), ("在不在", 3), ("亲", 1), ("谢谢", 2),
        ("辛苦", 2), ("随便看看", 3), ("老板", 2),
    ),
    "情感表达": (("太喜欢", 3), ("好可爱", 3), ("很满意", 3), ("失望", 3), ("难受", 2), ("生气", 3)),
    "无关话题": (("天气", 3), ("吃饭", 2), ("你有男朋友", 3), ("讲个笑话", 3)),
    # 转人工请求单列：权重 5 高于任何业务标签，因为「我要真人」
    # 是**最高优先级**的显式请求（BE-13 规则①），不该被意图竞争压下去。
    "转人工请求": (
        ("转人工", 5), ("真人客服", 5), ("人工客服", 4), ("找人工", 5),
        ("叫你们经理", 4), ("把经理叫来", 4), ("叫个真人", 5),
    ),
}

#: 大类强信号词。标签分不出来时用它兜大类（阈值高于标签）。
_CATEGORY_KEYWORDS: dict[IntentCategory, tuple[str, ...]] = {
    IntentCategory.PRODUCT_CONSULT: ("这款", "这个", "商品", "玩偶", "小熊", "挂件", "材质", "尺寸"),
    IntentCategory.ORDER_SERVICE: ("订单", "物流", "快递", "发货", "运单", "地址", "发票"),
    IntentCategory.AFTER_SALE: ("退", "换", "售后", "质量", "瑕疵", "坏", "赔"),
    IntentCategory.MARKETING: ("优惠", "活动", "券", "满减", "赠", "秒杀", "直播"),
    IntentCategory.BRAND_TRUST: ("品牌", "认证", "质检", "授权", "官方", "标准", "安全"),
}


@dataclass
class IntentResult:
    """意图识别结果。

    Attributes:
        category: 6 大类之一。
        label: 细分标签（30+ 之一）。
        confidence: 置信度 0-1。低于 0.35 建议走兜底。
        evidence: 命中的关键词（**可解释性的核心**，进日志与坐席卡片）。
        candidates: 备选（标签, 分数），用于前端展示「可能是 X / Y」。
    """

    category: IntentCategory
    label: str
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    candidates: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """导出为字典。"""
        return {
            "category": self.category.value,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "evidence": list(self.evidence),
            "candidates": [
                [name, round(score, 4)] for name, score in self.candidates[:3]
            ],
        }


#: 低于此置信度走兜底类目，不硬猜
LOW_CONFIDENCE_THRESHOLD = 0.35


class IntentClassifier:
    """意图分类器（规则打分 + 可插拔模型提示）。"""

    def __init__(self, *, low_confidence: float = LOW_CONFIDENCE_THRESHOLD) -> None:
        self.low_confidence = low_confidence
        # 预编译：关键词短、量大，每次 re.search 现编译会慢一个数量级
        self._compiled: dict[str, tuple[tuple[re.Pattern[str], int], ...]] = {
            label: tuple((re.compile(re.escape(kw)), w) for kw, w in kws)
            for label, kws in _LABEL_KEYWORDS.items()
        }

    def classify(self, text: str, *, llm_hint: str | None = None) -> IntentResult:
        """识别意图。

        Args:
            text: 用户原文。
            llm_hint: 模型给出的标签（可空）。命中时额外加权，
                **但不会覆盖规则给出的强证据**——模型偶尔会把
                「投诉」判成「售后咨询」，而投诉直接关系转人工优先级。

        Returns:
            `IntentResult`。
        """
        text = (text or "").strip()
        if not text:
            return IntentResult(
                category=IntentCategory.FALLBACK,
                label="无法识别",
                confidence=0.0,
                evidence=[],
            )

        scores: dict[str, tuple[float, list[str]]] = {}
        for label, patterns in self._compiled.items():
            total = 0.0
            hits: list[str] = []
            for pattern, weight in patterns:
                m = pattern.search(text)
                if m:
                    total += weight
                    hits.append(m.group(0))
            if total > 0:
                scores[label] = (total, hits)

        if llm_hint and llm_hint in INTENT_LABELS:
            prev = scores.get(llm_hint, (0.0, []))
            scores[llm_hint] = (prev[0] + 2.5, prev[1] + [f"模型:{llm_hint}"])

        if not scores:
            return self._fallback_by_category(text)

        ranked = sorted(
            scores.items(), key=lambda kv: (-kv[1][0], kv[0])
        )
        top_label, (top_score, top_hits) = ranked[0]
        # 置信度：用「top / (top + 次高分)」而不是绝对值。
        # 绝对值会随句子长度波动（长句堆更多关键词分更高），
        # 相对占比才真正表达「有多确定是这个意图」。
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        conf = top_score / (top_score + second) if top_score > 0 else 0.0

        if top_score < 2 or conf < self.low_confidence:
            return self._fallback_by_category(text)

        return IntentResult(
            category=INTENT_LABELS[top_label],
            label=top_label,
            confidence=conf,
            evidence=top_hits,
            candidates=[(name, sc) for name, (sc, _) in ranked[:3]],
        )

    def _fallback_by_category(self, text: str) -> IntentResult:
        """标签分不出来时，用大类强信号词兜底。

        这一层很关键：用户说「我要退款」只命中「退款」1 个 3 分标签，
        但如果整句只有「我想退掉这个订单」，「订单」会把订单服务也拉进来，
        纯标签最容易在这里打架。
        """
        best_cat: IntentCategory | None = None
        best_hits: list[str] = []
        for cat, words in _CATEGORY_KEYWORDS.items():
            hits = [w for w in words if w in text]
            if len(hits) > len(best_hits):
                best_cat, best_hits = cat, hits
        if best_cat is not None and best_hits:
            label = next(
                (lbl for lbl, cat in INTENT_LABELS.items() if cat is best_cat),
                "无法识别",
            )
            return IntentResult(
                category=best_cat,
                label=label,
                confidence=0.4,
                evidence=best_hits,
            )
        return IntentResult(
            category=IntentCategory.FALLBACK,
            label="无法识别",
            confidence=0.0,
            evidence=[],
        )


def get_intent_classifier() -> IntentClassifier:
    """全局分类器单例（预编译正则，别每次新建）。"""
    global _CLASSIFIER
    if _CLASSIFIER is None:
        _CLASSIFIER = IntentClassifier()
    return _CLASSIFIER


_CLASSIFIER: IntentClassifier | None = None


# ---------------------------------------------------------------------------
# AI-04 情绪识别
# ---------------------------------------------------------------------------

#: 愤怒词。命中的**权重最高的词决定下限**：出现「投诉」「骗子」这类词
#: 即便整句语气平和，也按愤怒处理——它们在客服语境里几乎只出现在
#: 已经要维权的用户身上。
#:
#: **已剔除的弱词**（曾误判，逐个记录理由）：
#: * 「还没」——「还没发货」是查询不是抱怨，实测把中性单误判成不满
#: * 「等了」「急」「到底」「几天了」——正常催促，不构成负面
#: * 「退货」——业务动作词，几乎每条售后都在用
#: 情绪词的验收标准是**误报率**，不是召回率：把中性用户判成愤怒，
#: 会触发 BE-13 规则②直接转人工，代价远高于漏判一次轻微不满。
_ANGRY_WORDS: tuple[tuple[str, int], ...] = (
    ("愤怒", 3), ("气死", 3), ("骗子", 3), ("欺骗", 3), ("垃圾", 3),
    ("投诉", 3), ("举报", 3), ("维权", 3), ("曝光", 3), ("起诉", 3),
    ("法庭", 3), ("12315", 3), ("坑人", 3), ("黑店", 3), ("破店", 3),
    ("过分", 2), ("离谱", 2), ("凭什么", 2), ("滚", 3),
    ("妈的", 3), ("废物", 3), ("神经病", 3), ("恶心", 3),
    ("欺诈", 3), ("再也不", 3), ("服务太差", 3), ("太差", 2),
)

#: 不满词。程度轻于愤怒：抱怨、着急、失望，但没到维权。
_DISSATISFIED_WORDS: tuple[tuple[str, int], ...] = (
    ("不满", 3), ("失望", 3), ("着急", 2), ("太慢", 3),
    ("太久了", 3), ("怎么回事", 2), ("为什么还", 2),
    ("不满意", 3), ("差评", 3), ("敷衍", 3), ("没人管", 3),
    ("不回复", 2), ("半天", 2), ("烦", 2), ("无语", 2),
    ("搞什么", 2), ("白等", 2), ("无语了", 3), ("糟心", 3),
    ("少发", 3), ("漏发", 3), ("没动", 2), ("不动", 1),
    ("次品", 3), ("破洞", 3), ("严重", 2), ("怎么处理", 1),
)

#: 满意词。用于把「不满」误判压下去——用户说「谢谢，很满意」时
#: 句中的「气」字不该被当成愤怒。
_SATISFIED_WORDS: tuple[str, ...] = (
    "满意", "谢谢", "喜欢", "好评", "太棒", "不错", "顺利", "及时",
    "辛苦", "贴心", "赞", "开心", "舒服", "放心",
)

#: 否定前缀。「不满意」不能算「满意」。
_NEGATION_PREFIXES: tuple[str, ...] = ("不", "没", "别", "未", "无")


@dataclass
class EmotionResult:
    """情绪识别结果。

    Attributes:
        emotion: 三值之一。
        score: 负面强度（0-10，仅负面时有意义）。
        negative_streak: 连续负面轮数（调用方传入上一轮 streak）。
        should_flag: 是否达到「连续 3 轮负面」标记阈值。
        evidence: 命中的词。
    """

    emotion: Emotion
    score: float = 0.0
    negative_streak: int = 0
    should_flag: bool = False
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """导出为字典。"""
        return {
            "emotion": self.emotion.value,
            "score": round(self.score, 2),
            "negative_streak": self.negative_streak,
            "should_flag": self.should_flag,
            "evidence": list(self.evidence),
        }


#: 愤怒判定的分数下限
_ANGRY_THRESHOLD = 3.0


class EmotionDetector:
    """情绪识别器。三值：NEUTRAL / DISSATISFIED / ANGRY。"""

    def detect(
        self,
        text: str,
        *,
        prev_streak: int = 0,
        threshold: int = 3,
    ) -> EmotionResult:
        """识别用户情绪。

        Args:
            text: 用户原文。
            prev_streak: 上一轮的连续负面轮数。
            threshold: 连续几轮负面触发标记（DoD = 3）。

        Returns:
            `EmotionResult`。
        """
        text = (text or "").strip()
        angry_hits: list[str] = []
        angry_score = 0.0
        for word, weight in _ANGRY_WORDS:
            if word in text:
                angry_hits.append(word)
                angry_score += weight

        sad_hits: list[str] = []
        sad_score = 0.0
        for word, weight in _DISSATISFIED_WORDS:
            if word in text:
                sad_hits.append(word)
                sad_score += weight

        # 满意度做**抵消**，但要跳过被否定的（「不满意」不是满意）
        satisfied = 0
        for word in _SATISFIED_WORDS:
            idx = text.find(word)
            while idx >= 0:
                prefix = text[max(0, idx - 1) : idx]
                if prefix not in _NEGATION_PREFIXES:
                    satisfied += 1
                    break
                idx = text.find(word, idx + 1)

        if angry_score >= _ANGRY_THRESHOLD:
            emotion = Emotion.ANGRY
            score = angry_score
            evidence = angry_hits
        elif sad_score > 0 or angry_score > 0:
            emotion = Emotion.DISSATISFIED
            score = max(sad_score, angry_score)
            evidence = sad_hits + angry_hits
        else:
            emotion = Emotion.NEUTRAL
            score = 0.0
            evidence = []

        # 满意度足够强且没有愤怒词 → 拉回中性
        if satisfied >= 1 and angry_score < _ANGRY_THRESHOLD and sad_score <= 1:
            emotion = Emotion.NEUTRAL
            score = 0.0
            evidence = []
        # 「不」+满意这类强否定，即便没进词表也算不满
        if re.search(r"不(?:太)?满意|不(?:太)?好|不行", text) and emotion is Emotion.NEUTRAL:
            emotion = Emotion.DISSATISFIED
            score = max(score, 2.0)
            evidence = evidence or ["不满意"]

        negative = emotion is not Emotion.NEUTRAL
        streak = (prev_streak + 1) if negative else 0
        return EmotionResult(
            emotion=emotion,
            score=score,
            negative_streak=streak,
            should_flag=streak >= threshold,
            evidence=evidence,
        )


def get_emotion_detector() -> EmotionDetector:
    """全局情绪识别器单例。"""
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = EmotionDetector()
    return _DETECTOR


_DETECTOR: EmotionDetector | None = None


# ---------------------------------------------------------------------------
# 商家语料标签映射
# ---------------------------------------------------------------------------

#: 商家语料用的是中文展示值，内部枚举是英文。这里是唯一转换点。
CORPUS_EMOTION_MAP: dict[str, Emotion] = {
    "中性": Emotion.NEUTRAL,
    "满意": Emotion.NEUTRAL,  # 满意的会话在情绪维度上是「无负面」，归中性
    "不满": Emotion.DISSATISFIED,
    "愤怒": Emotion.ANGRY,
}

CORPUS_INTENT_MAP: dict[str, IntentCategory] = {
    "商品咨询": IntentCategory.PRODUCT_CONSULT,
    "订单服务": IntentCategory.ORDER_SERVICE,
    "售后服务": IntentCategory.AFTER_SALE,
    "营销活动": IntentCategory.MARKETING,
    "品牌信任": IntentCategory.BRAND_TRUST,
    "兜底": IntentCategory.FALLBACK,
}
