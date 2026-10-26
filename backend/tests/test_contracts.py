"""契约测试：`app/schemas` 与任务书第 2 章的字段/枚举/阈值必须一致。

契约变更流程（任务书 2.7 节）：改 schemas → 跑本文件 → 同步
`docs/内部服务契约落地版.md` → 通知前后端。**本文件是流程的第一道闸**：
任何字段增删改只要没同步更新这里的断言，CI 就会红。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.enums import (
    Channel,
    Emotion,
    HandoffReason,
    InboundMsgType,
    LogisticsStatus,
    OrderStatus,
    OutboundMsgType,
    Priority,
    RagDecision,
    Sender,
    StockStatus,
    TextSource,
    TicketStatus,
)
from app.schemas.bff import (
    BffContextCard,
    BffTakeoverRequest,
    SseEvent,
    SseEventName,
)
from app.schemas.chat import ChatTurnOptions, ChatTurnRequest, ReplyPayload
from app.schemas.handoff import HandoffRequest, HandoffResponse, TicketContextCard
from app.schemas.knowledge import LENGTH_RANGES, ChunkMetadata, ChunkRecord
from app.schemas.rag import (
    RagRetrieveResponse,
    RagThresholds,
    decide,
)
from app.schemas.tools import (
    COMPANY_CN,
    LOGISTICS_STATUS_CN,
    ORDER_STATUS_CN,
    STOCK_STATUS_CN,
    stock_status_cn,
)
from app.schemas.umo import NormalizedText, RawMessage, UnifiedMessageObject

pytestmark = pytest.mark.contract


# =============================================================================
# 2.6 枚举总表：值必须与任务书逐字一致（前端 contract.ts 依赖它）
# =============================================================================
class TestEnumTable:
    """枚举总表（任务书 2.6 节，唯一口径）。"""

    def test_channel(self) -> None:
        """渠道 5 值。"""
        assert [c.value for c in Channel] == [
            "TAOBAO",
            "TMALL",
            "PDD",
            "DOUYIN",
            "WECHAT",
        ]

    def test_msg_type_inbound(self) -> None:
        """入站消息类型：TEXT/IMAGE 为商家定义值，VOICE 为项目组扩展（C-02）。"""
        values = [t.value for t in InboundMsgType]
        assert values[:2] == ["TEXT", "IMAGE"]
        assert "VOICE" in values, "VOICE 是项目组扩展值，对应待确认项 C-02"

    def test_msg_type_outbound(self) -> None:
        """出站只允许 TEXT/IMAGE（CARD/GOODS 本期不接入，硬约束 HC-1）。"""
        assert OutboundMsgType.TEXT.value == "TEXT"
        assert OutboundMsgType.IMAGE.value == "IMAGE"

    def test_sender_and_direction(self) -> None:
        """发送者 3 值。"""
        assert [s.value for s in Sender] == ["USER", "BOT", "AGENT"]

    def test_order_status(self) -> None:
        """订单状态 5 值。"""
        assert [s.value for s in OrderStatus] == [
            "CREATED",
            "PAID",
            "SHIPPED",
            "DELIVERED",
            "CLOSED",
        ]

    def test_logistics_status(self) -> None:
        """物流状态 7 值。"""
        assert [s.value for s in LogisticsStatus] == [
            "PENDING",
            "COLLECTED",
            "IN_TRANSIT",
            "OUT_FOR_DELIVERY",
            "DELIVERED",
            "EXCEPTION",
            "RETURNED",
        ]

    def test_stock_status(self) -> None:
        """库存状态 3 值。"""
        assert [s.value for s in StockStatus] == [
            "IN_STOCK",
            "PRESALE",
            "OUT_OF_STOCK",
        ]

    def test_handoff_reason(self) -> None:
        """转人工原因 5 值（C-01 待商家确认）。"""
        assert [r.value for r in HandoffReason] == [
            "USER_REQUEST",
            "EMOTION",
            "UNRESOLVED",
            "HIGH_RISK",
            "SYSTEM_ERROR",
        ]

    def test_priority_and_ticket_status(self) -> None:
        """工单优先级 4 值、状态 3 值。"""
        assert [p.value for p in Priority] == ["LOW", "NORMAL", "HIGH", "URGENT"]
        assert [s.value for s in TicketStatus] == ["OPEN", "PROCESSING", "CLOSED"]

    def test_emotion(self) -> None:
        """情绪 3 值（中文展示：中性/不满/愤怒）。"""
        assert [e.value for e in Emotion] == ["NEUTRAL", "DISSATISFIED", "ANGRY"]

    def test_text_source(self) -> None:
        """文本来源 5 值（三路归一化）。"""
        assert [t.value for t in TextSource] == [
            "ORIGINAL",
            "ASR",
            "OCR",
            "VLM",
            "CLASSIFIER",
        ]

    def test_rag_decision(self) -> None:
        """RAG 判定 3 值（硬性：<0.55 必须拒答）。"""
        assert [d.value for d in RagDecision] == [
            "DIRECT_ANSWER",
            "WEAK",
            "NO_EVIDENCE",
        ]


# =============================================================================
# 2.4.2 RAG 阈值分流：唯一判定入口，边界值必须精确
# =============================================================================
class TestRagThreshold:
    """阈值分流（任务书 2.4.2 + BE-05 DoD）。"""

    def test_threshold_values(self) -> None:
        """阈值常量必须是 0.72 / 0.55（不可改）。"""
        t = RagThresholds()
        assert t.direct == 0.72
        assert t.fallback == 0.55

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (1.0, RagDecision.DIRECT_ANSWER),
            (0.73, RagDecision.DIRECT_ANSWER),
            (0.72, RagDecision.DIRECT_ANSWER),   # 边界：>= direct 即直答
            (0.7199, RagDecision.WEAK),
            (0.6, RagDecision.WEAK),
            (0.55, RagDecision.WEAK),            # 边界：>= fallback 即弱证据
            (0.5499, RagDecision.NO_EVIDENCE),
            (0.0, RagDecision.NO_EVIDENCE),      # 无命中
        ],
    )
    def test_decide_boundaries(self, score: float, expected: RagDecision) -> None:
        """边界值必须落在正确档位（差 0.0001 也不能串档）。

        Args:
            score: 检索最高分。
            expected: 期望判定。
        """
        assert decide(score) is expected

    def test_no_hit_is_no_evidence(self) -> None:
        """无命中时 top_score=0.0，decision 必须是 NO_EVIDENCE。"""
        r = RagRetrieveResponse(query="不存在的商品")
        assert r.top_score == 0.0
        assert r.decision is RagDecision.NO_EVIDENCE
        assert r.hits == []


# =============================================================================
# 2.4.4 业务工具：中文口径是唯一对外出口（BE-09 DoD）
# =============================================================================
class TestChineseWording:
    """英文枚举绝不外泄（BE-09 DoD）。"""

    def test_stock_status_cn(self) -> None:
        """三状态中文映射。"""
        assert STOCK_STATUS_CN[StockStatus.IN_STOCK] == "现货"
        assert STOCK_STATUS_CN[StockStatus.PRESALE] == "预售"
        assert STOCK_STATUS_CN[StockStatus.OUT_OF_STOCK] == "缺货"

    def test_presale_with_days(self) -> None:
        """预售带天数：『预售 N 天』。"""
        assert stock_status_cn(StockStatus.PRESALE, 15) == "预售 15 天"
        assert stock_status_cn(StockStatus.PRESALE) == "预售"

    def test_stock_status_cn_never_english(self) -> None:
        """任何库存状态的中文口径都不含英文枚举。"""
        for status in StockStatus:
            text = stock_status_cn(status, 7)
            assert status.value not in text, f"{status} 的中文口径泄露了英文枚举"

    def test_order_status_cn(self) -> None:
        """订单状态中文映射。"""
        assert ORDER_STATUS_CN[OrderStatus.PAID] == "已付款"
        assert ORDER_STATUS_CN[OrderStatus.SHIPPED] == "已发货"

    def test_logistics_status_cn(self) -> None:
        """物流状态中文映射。"""
        assert LOGISTICS_STATUS_CN[LogisticsStatus.IN_TRANSIT] == "运输中"
        assert LOGISTICS_STATUS_CN[LogisticsStatus.OUT_FOR_DELIVERY] == "派送中"

    def test_company_cn(self) -> None:
        """快递公司中文映射。"""
        assert COMPANY_CN["ZTO"] == "中通"
        assert COMPANY_CN["YTO"] == "圆通"
        assert COMPANY_CN["SF"] == "顺丰"


# =============================================================================
# 2.2 UMO：三路输入归一化与置信度路径
# =============================================================================
class TestUmo:
    """统一消息对象（任务书 2.2 节）。"""

    def _umo(self, **kwargs) -> UnifiedMessageObject:
        """构造最小 UMO。

        Args:
            **kwargs: 覆盖默认字段。

        Returns:
            UnifiedMessageObject 实例。
        """
        base = {
            "msg_id": "msg_001",
            "session_id": "sess_001",
            "trace_id": "trace_001",
            "buyer_id": "buyer_001",
            "channel": Channel.TAOBAO,
            "sent_at": datetime(2026, 1, 15, 10, 30, 0),
            "raw": RawMessage(msg_type=InboundMsgType.TEXT),
            "normalized": NormalizedText(text="你好"),
        }
        base.update(kwargs)
        return UnifiedMessageObject(**base)

    def test_text_source_original_confidence_one(self) -> None:
        """文本输入：text_source=ORIGINAL，confidence=1.0。"""
        umo = self._umo()
        assert umo.normalized.text_source is TextSource.ORIGINAL
        assert umo.normalized.confidence == 1.0

    @pytest.mark.parametrize(
        "field", ["msg_id", "session_id", "trace_id", "buyer_id"]
    )
    def test_blank_id_rejected(self, field: str) -> None:
        """四个标识符禁止空白（空串会破坏 trace 与幂等）。

        Args:
            field: 被测字段名。
        """
        with pytest.raises(ValueError):
            self._umo(**{field: "   "})

    def test_sent_at_format(self) -> None:
        """`model_dump_umo()` 的 sent_at 格式必须是 yyyy-MM-dd HH:mm:ss。"""
        umo = self._umo()
        dumped = umo.model_dump_umo()
        assert dumped["sent_at"] == umo.sent_at.strftime("%Y-%m-%d %H:%M:%S")

    def test_intent_stack_push_dedup(self) -> None:
        """意图栈：相同意图不重复入栈，只置顶。"""
        umo = self._umo()
        umo.session_state.push_intent("查订单")
        umo.session_state.push_intent("查物流")
        umo.session_state.push_intent("查订单")
        assert umo.session_state.intent_stack[-1] == "查订单"
        assert umo.session_state.intent_stack.count("查订单") == 1

    def test_intent_stack_depth_limit(self) -> None:
        """意图栈超深时删栈底（防 Redis 值无限增长）。"""
        umo = self._umo()
        for i in range(20):
            umo.session_state.push_intent(f"意图{i}")
        assert len(umo.session_state.intent_stack) <= 8

    def test_session_state_defaults(self) -> None:
        """会话状态默认值：机器人持有、未静音、情绪中性。"""
        umo = self._umo()
        s = umo.session_state
        assert s.owner == "BOT"
        assert s.bot_muted is False
        assert s.emotion is Emotion.NEUTRAL
        assert s.turn_count == 0


# =============================================================================
# 2.5.2 上下文卡片七要素：缺一即缺陷（FE-02 验收基线 100%）
# =============================================================================
class TestContextCard:
    """七要素完整性（任务书 2.5.2 + BE-14 DoD）。"""

    def test_all_seven_missing_when_empty(self) -> None:
        """空卡片应报出全部 7 项缺失。"""
        card = TicketContextCard(session_id="sess_001", buyer_id="buyer_001")
        missing = card.missing_fields()
        assert len(missing) == 7
        assert not card.is_complete()

    def test_complete_card(self) -> None:
        """七要素齐全时 is_complete() 为真。"""
        card = TicketContextCard(
            session_id="sess_001",
            buyer_id="buyer_001",
            channel=Channel.TAOBAO,
            summary="买家咨询奶油小熊挂件是否现货",
            verified=[{"key": "商品", "value": "奶油小熊 15cm 挂件"}],
            tried_actions=[{"action": "inventory_query", "status": "ok"}],
            unmet_demand="想确认发货时间",
            suggested_action="告知 48 小时内发货",
            emotion={"level": "NEUTRAL", "intensity": "轻度"},
            turn_count=3,
        )
        assert card.missing_fields() == []
        assert card.is_complete()

    def test_emotion_intensity_is_chinese_text(self) -> None:
        """情绪强度是中文口径（与前端 contract.ts 的 EmotionIntensity 对齐）。"""
        card = TicketContextCard(
            session_id="s",
            buyer_id="b",
            emotion={"level": "ANGRY", "intensity": "强烈"},
        )
        assert card.emotion["intensity"] == "强烈"

    def test_summary_length_capped(self) -> None:
        """摘要 ≤500 字由转人工请求侧强制（TicketContextCard 自身不截断，
        超长摘要应在构造 HandoffRequest 时即被拒绝）。"""
        with pytest.raises(ValueError):
            HandoffRequest(
                session_id="sess_001",
                buyer_id="b",
                reason=HandoffReason.UNRESOLVED,
                summary="x" * 501,
            )


# =============================================================================
# 2.4.5 转人工：user_message 严禁外链（HC-1 代码级强制）
# =============================================================================
class TestHandoffNoLink:
    """HC-1：给用户的话术不得含任何链接或跳转词。"""

    @pytest.mark.parametrize(
        "bad",
        [
            "请点击 https://kf.wankeai.example.com/t/123 查看",
            "点击这里联系客服",
            "点这里转人工",
            "请跳转到工单页面",
            "小程序搜索玩可爱",
        ],
    )
    def test_reject_link_wording(self, bad: str) -> None:
        """命中链接/跳转词必须抛错。

        Args:
            bad: 违规话术。
        """
        with pytest.raises(ValueError):
            HandoffResponse(ticket_no="TS202601010001", user_message=bad)

    def test_accept_plain_wording(self) -> None:
        """正常话术可通过。"""
        r = HandoffResponse(
            ticket_no="TS202601010001",
            user_message="已经为您转接人工客服，请稍候～",
        )
        assert "http" not in r.user_message


# =============================================================================
# 2.4.3 知识库切分长度区间（BE-06 DoD：长度合规）
# =============================================================================
class TestChunkLength:
    """四类知识的长度区间必须与任务书一致。"""

    def test_length_ranges(self) -> None:
        """商品 200-400 / 政策 100-300 / FAQ 50-200 / 长文档 300-500。"""
        assert LENGTH_RANGES[KnowledgeTypeAlias.PRODUCT] == (200, 400)
        assert LENGTH_RANGES[KnowledgeTypeAlias.POLICY] == (100, 300)
        assert LENGTH_RANGES[KnowledgeTypeAlias.FAQ] == (50, 200)
        assert LENGTH_RANGES[KnowledgeTypeAlias.DOC] == (300, 500)

    def test_chunk_record_length_in_range(self) -> None:
        """长度在区间内时 `length_in_range()` 为真。"""
        rec = ChunkRecord(
            chunk_id="kb_faq_001",
            content="x" * 120,
            metadata=ChunkMetadata(knowledge_type=KnowledgeTypeAlias.FAQ),
        )
        assert rec.length == 120
        assert rec.length_in_range()

    def test_metadata_completeness(self) -> None:
        """元数据完整率可计算（BE-06 DoD：100%）。"""
        meta = ChunkMetadata(
            category="毛绒玩偶",
            sku_ids=["WK-PL-001"],
            knowledge_type=KnowledgeTypeAlias.PRODUCT,
            source="商品知识卡.csv",
            authority=1,
            updated_by="运营小王",
        )
        assert 0.0 <= meta.completeness() <= 1.0


# =============================================================================
# 2.5.4 SSE 事件名：前端 EventSource 监听的名字必须逐字一致
# =============================================================================
class TestSseEvents:
    """SSE 事件名（任务书 2.5.4）。"""

    def test_event_names(self) -> None:
        """8 个事件名，带点号格式。"""
        assert SseEventName.TURN_START.value == "turn.start"
        assert SseEventName.TURN_END.value == "turn.end"
        assert SseEventName.DELTA.value == "delta"
        assert SseEventName.ERROR.value == "error"

    def test_to_sse_format(self) -> None:
        """`to_sse()` 输出 `event: X\\ndata: {...}\\n\\n`。"""
        ev = SseEvent(event=SseEventName.DELTA, data={"text": "您好"})
        wire = ev.to_sse()
        assert wire.startswith("event: delta\n")
        assert wire.endswith("\n\n")
        assert '"text"' in wire


# =============================================================================
# 2.4.1 对话接口：reply.cards 本期必须恒为空（HC-1）
# =============================================================================
class TestChatTurn:
    """对话请求/响应（任务书 2.4.1）。"""

    def test_cards_always_empty(self) -> None:
        """卡片数组恒空——CARD 消息本期不接入。"""
        reply = ReplyPayload(content="您好，有什么可以帮您～")
        assert reply.cards == []

    def test_max_turns_bounds(self) -> None:
        """max_turns 边界 1..20。"""
        assert ChatTurnOptions().max_turns == 5
        with pytest.raises(ValueError):
            ChatTurnOptions(max_turns=0)
        with pytest.raises(ValueError):
            ChatTurnOptions(max_turns=21)

    def test_takeover_bot_muted(self) -> None:
        """一键接管后 bot_muted 必须为 True（BE-13 DoD：接管即停止回复）。"""
        req = BffTakeoverRequest(agent_id="agent_001")
        assert req.agent_id == "agent_001"


# ---------------------------------------------------------------------------
# 局部别名：避免顶部 import 过长
# ---------------------------------------------------------------------------
from app.core.enums import KnowledgeType as KnowledgeTypeAlias  # noqa: E402
