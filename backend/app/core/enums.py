"""数据字典与枚举总表（唯一口径）。

严格对齐《玩可爱智能客服Agent_前后端交付任务书》2.6.1 节「枚举」表。
**本文件是前后端共用枚举的唯一来源**，任何新增枚举值必须走 2.7.2 节契约变更流程。

来源标注：
    - 「商家接口」= 契约 A，已冻结，项目组只做调用方
    - 「项目组」  = 契约 B/C，由项目组架构师定义
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类：JSON 序列化即字面值，便于与商家接口对齐。"""

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return str(self.value)


# ---------------------------------------------------------------------------
# 渠道与消息（来源：商家接口）
# ---------------------------------------------------------------------------


class Channel(StrEnum):
    """消息渠道。来源：商家接口 2.6.1。"""

    TAOBAO = "TAOBAO"
    TMALL = "TMALL"
    PDD = "PDD"
    DOUYIN = "DOUYIN"
    WECHAT = "WECHAT"


class InboundMsgType(StrEnum):
    """入站消息类型。来源：商家接口 + 项目组扩展。

    商家 0.1 节仅定义 TEXT/IMAGE/CARD/GOODS；VOICE 是项目组在接入层的扩展映射，
    对应待商家确认项 C-02（语音消息在平台回调中的实际 msg_type）。
    """

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VOICE = "VOICE"  # 项目组扩展，契约项 C-02 待商家 IT 确认
    CARD = "CARD"
    GOODS = "GOODS"


class OutboundMsgType(StrEnum):
    """出站消息类型。来源：商家 0.8 节硬约束。

    ⚠️ 本期**实际只使用 TEXT 与 IMAGE**。CARD/GOODS 接口定义保留、字段占位保留，
    但本期不实现对接（订单/商品卡片一律改用纯文本分行呈现）。
    """

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    CARD = "CARD"  # 本期不接入
    GOODS = "GOODS"  # 本期不接入


class Direction(StrEnum):
    """消息方向。来源：项目组。"""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class Sender(StrEnum):
    """发送方。来源：商家接口。"""

    USER = "USER"
    BOT = "BOT"
    AGENT = "AGENT"


class TextSource(StrEnum):
    """归一化文本的来源，用于区分置信度来源。来源：项目组。"""

    ORIGINAL = "ORIGINAL"
    ASR = "ASR"
    OCR = "OCR"
    VLM = "VLM"
    CLASSIFIER = "CLASSIFIER"


# ---------------------------------------------------------------------------
# 业务状态枚举（来源：商家接口）
# ---------------------------------------------------------------------------


class OrderStatus(StrEnum):
    """订单状态。来源：商家接口 1.5 节。"""

    CREATED = "CREATED"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CLOSED = "CLOSED"


class LogisticsStatus(StrEnum):
    """物流状态。来源：商家接口 2.5 节。"""

    PENDING = "PENDING"
    COLLECTED = "COLLECTED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"
    RETURNED = "RETURNED"


class StockStatus(StrEnum):
    """库存状态。来源：商家接口 3.5 节。

    ⚠️ **对外答复一律用中文口径，不暴露英文枚举**（见 BE-09）。
    """

    IN_STOCK = "IN_STOCK"
    PRESALE = "PRESALE"
    OUT_OF_STOCK = "OUT_OF_STOCK"


class CompanyCode(StrEnum):
    """快递公司编码。来源：商家接口 2.5 节。"""

    ZTO = "ZTO"  # 中通
    YTO = "YTO"  # 圆通
    SF = "SF"  # 顺丰


class TicketStatus(StrEnum):
    """工单状态。来源：商家接口 5.5 节。"""

    OPEN = "OPEN"
    PROCESSING = "PROCESSING"
    CLOSED = "CLOSED"


class Priority(StrEnum):
    """优先级。来源：商家接口 5.2 节。"""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class HandoffReason(StrEnum):
    """转人工原因。来源：商家接口 5.2 节，5 值。

    须与人工工作台一致，属待商家确认项 C-01。
    """

    USER_REQUEST = "USER_REQUEST"  # 规则① 显式请求
    EMOTION = "EMOTION"  # 规则② 情绪
    UNRESOLVED = "UNRESOLVED"  # 规则③ 未解决（含规则⑤ 检索失败的一种归入）
    HIGH_RISK = "HIGH_RISK"  # 规则④ 高风险意图（含规则⑤ 另一种归入）
    SYSTEM_ERROR = "SYSTEM_ERROR"  # 规则⑥ 系统异常


class ReviewStatus(StrEnum):
    """知识条目审核状态。来源：商家 FAQ 表 review_status 列。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class Emotion(StrEnum):
    """用户情绪。来源：商家语料「用户情绪」列，中文展示为 中性/不满/愤怒。"""

    NEUTRAL = "NEUTRAL"
    DISSATISFIED = "DISSATISFIED"
    ANGRY = "ANGRY"


# ---------------------------------------------------------------------------
# 项目组自定义枚举
# ---------------------------------------------------------------------------


class RagDecision(StrEnum):
    """RAG 阈值分流决策。来源：项目组 2.4.2 节，阈值硬性不可改。

    | 值              | 条件                  | 编排层动作                                   |
    |-----------------|-----------------------|----------------------------------------------|
    | DIRECT_ANSWER   | top_score >= 0.72     | 正常生成回复                                 |
    | WEAK            | 0.55 <= score < 0.72  | 生成回复但降低断言强度，或追加「如有出入请以人工为准」 |
    | NO_EVIDENCE     | top_score < 0.55      | **必须**走兜底或转人工，**不得生成**              |
    """

    DIRECT_ANSWER = "DIRECT_ANSWER"
    WEAK = "WEAK"
    NO_EVIDENCE = "NO_EVIDENCE"


class RiskLevel(StrEnum):
    """风险等级，用于 UMO.flags.risk_level。来源：项目组。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class KnowledgeType(StrEnum):
    """知识类型，决定切分策略。来源：商家《商品分类体系表.csv》「知识库切分单元」列。

    切分单元（**非项目组自拟**）：
        商品知识 = 一款一单元 200-400 字
        售后政策 = 一条政策一单元 100-300 字
        FAQ      = 一问一答一单元 50-200 字
        长文档   = 标题层级递归 300-500 字
        重叠     = 15%
    """

    PRODUCT = "PRODUCT"  # 商品知识
    POLICY = "POLICY"  # 售后政策
    FAQ = "FAQ"  # 问答
    DOC = "DOC"  # 长文档


class ActionType(StrEnum):
    """编排层动作类型（BE-04 动作决策三选一：该答 / 该查 / 该转）。"""

    RAG_ANSWER = "rag_answer"  # 该答
    TOOL_QUERY = "tool_query"  # 该查
    HANDOFF = "handoff"  # 该转


class ToolName(StrEnum):
    """可调用的业务工具名，用于 actions / tried_actions 记录。"""

    ORDER_QUERY = "order_query"
    LOGISTICS_QUERY = "logistics_query"
    INVENTORY_QUERY = "inventory_query"
    PRODUCT_QUERY = "product_query"
    FAQ_SHIPPING_SLA = "faq_shipping_sla"
    TICKET_CREATE = "ticket_create"


class IntentCategory(StrEnum):
    """意图 6 大类。来源：商家《历史客服会话语料.csv》「意图类目」列（已标注 324 条）。"""

    PRODUCT_CONSULT = "商品咨询"
    ORDER_SERVICE = "订单服务"
    AFTER_SALE = "售后服务"
    MARKETING = "营销活动"
    BRAND_TRUST = "品牌信任"
    FALLBACK = "兜底"


class ImageRoute(StrEnum):
    """图片五路分流结果。来源：FR-03 / AI-07。"""

    PRODUCT_IMAGE = "商品图"
    ORDER_SCREENSHOT = "订单截图"
    DEFECT_IMAGE = "瑕疵图"
    SIZE_CHART = "尺码表"
    MEME_IRRELEVANT = "表情包"


class HealthStatus(StrEnum):
    """依赖健康检查状态。"""

    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"


__all__ = [
    "ActionType",
    "Channel",
    "CompanyCode",
    "Direction",
    "Emotion",
    "HandoffReason",
    "HealthStatus",
    "ImageRoute",
    "InboundMsgType",
    "IntentCategory",
    "KnowledgeType",
    "LogisticsStatus",
    "OrderStatus",
    "OutboundMsgType",
    "Priority",
    "RagDecision",
    "ReviewStatus",
    "RiskLevel",
    "Sender",
    "StockStatus",
    "StrEnum",
    "TextSource",
    "TicketStatus",
    "ToolName",
]
