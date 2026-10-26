"""契约模型（Pydantic v2）。

**本包是前后端与内部服务之间唯一的结构口径**，与任务书第 2 章逐字段对齐：

| 模块 | 对应契约 | 任务书章节 |
| --- | --- | --- |
| `umo` | 契约 B · 统一消息协议 UMP | 2.2 |
| `chat` | 契约 B · 对话核心 | 2.4.1 |
| `rag` | 契约 B · RAG 检索服务 | 2.4.2 |
| `knowledge` | 契约 B · 知识库服务 | 2.4.3 |
| `tools` | 契约 B · 业务工具服务 | 2.4.4 |
| `handoff` | 契约 B · 转人工服务 | 2.4.5 |
| `bff` | 契约 C · BFF 契约 | 2.5 |
| `common` | 统一响应 / 分页 | 2.5.5 |

变更流程（2.7 节）：任何字段增删改必须先改本包 → 跑契约测试 → 同步
`docs/内部服务契约落地版.md` → 通知前后端。
"""

from app.schemas.bff import (
    BffContextCard,
    BffKbChunk,
    BffKbChunkCreate,
    BffKbSearchRequest,
    BffKbSearchResponse,
    BffSessionDetail,
    BffSessionSummary,
    BffTakeoverRequest,
    BffTakeoverResponse,
    BffTicketDetail,
    BffTicketSummary,
    SseEvent,
    SseEventName,
)
from app.schemas.chat import (
    ChatTurnOptions,
    ChatTurnRequest,
    ChatTurnResponse,
    LatencyBreakdown,
    ReplyPayload,
)
from app.schemas.common import CommonFilters, HealthReport, TimeRange
from app.schemas.handoff import (
    HandoffRequest,
    HandoffResponse,
    TicketContextCard,
    TranscriptTurn,
)
from app.schemas.knowledge import (
    ChunkCreateRequest,
    ChunkHistoryEntry,
    ChunkMetadata,
    ChunkRecord,
    ChunkUpdateRequest,
    ImportPreviewResponse,
    ImportRequest,
    ImportResult,
    ReindexRequest,
    ReindexResponse,
)
from app.schemas.rag import (
    RagHit,
    RagRetrieveRequest,
    RagRetrieveResponse,
    RagThresholds,
)
from app.schemas.tools import (
    InventoryItem,
    InventoryQueryResponse,
    LogisticsQueryResponse,
    LogisticsTrace,
    OrderAmount,
    OrderItem,
    OrderQueryResponse,
    ProductItem,
    ProductQueryResponse,
    ReceiverInfo,
)
from app.schemas.umo import (
    NormalizedText,
    RawMessage,
    SessionState,
    UmoEntities,
    UmoFlags,
    UnifiedMessageObject,
)

__all__ = [
    "BffContextCard",
    "BffKbChunk",
    "BffKbChunkCreate",
    "BffKbSearchRequest",
    "BffKbSearchResponse",
    "BffSessionDetail",
    "BffSessionSummary",
    "BffTakeoverRequest",
    "BffTakeoverResponse",
    "BffTicketDetail",
    "BffTicketSummary",
    "ChatTurnOptions",
    "ChatTurnRequest",
    "ChatTurnResponse",
    "ChunkCreateRequest",
    "ChunkHistoryEntry",
    "ChunkMetadata",
    "ChunkRecord",
    "ChunkUpdateRequest",
    "CommonFilters",
    "HandoffRequest",
    "HandoffResponse",
    "HealthReport",
    "ImportPreviewResponse",
    "ImportRequest",
    "ImportResult",
    "InventoryItem",
    "InventoryQueryResponse",
    "LatencyBreakdown",
    "LogisticsQueryResponse",
    "LogisticsTrace",
    "NormalizedText",
    "OrderAmount",
    "OrderItem",
    "OrderQueryResponse",
    "ProductItem",
    "ProductQueryResponse",
    "RagHit",
    "RagRetrieveRequest",
    "RagRetrieveResponse",
    "RagThresholds",
    "RawMessage",
    "ReceiverInfo",
    "ReindexRequest",
    "ReindexResponse",
    "ReplyPayload",
    "SessionState",
    "SseEvent",
    "SseEventName",
    "TicketContextCard",
    "TimeRange",
    "TranscriptTurn",
    "UmoEntities",
    "UmoFlags",
    "UnifiedMessageObject",
]
