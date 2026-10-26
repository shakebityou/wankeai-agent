"""契约 B · RAG 检索服务（任务书 2.4.2 节）。

    请求：POST /internal/v1/rag/retrieve
    响应：{ "hits": [...], "top_score": 0.83, "decision": "DIRECT_ANSWER",
            "thresholds": { "direct": 0.72, "fallback": 0.55 } }

**阈值硬性不可改**：
    DIRECT_ANSWER  top_score >= 0.72
    WEAK           0.55 <= top_score < 0.72
    NO_EVIDENCE    top_score < 0.55  →  **不得生成**，必须走兜底或转人工
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.enums import KnowledgeType, RagDecision

#: 向量路召回条数（硬性）
DEFAULT_TOP_K_VECTOR = 20
#: BM25 路召回条数（硬性）
DEFAULT_TOP_K_BM25 = 20
#: 重排后保留条数（硬性）
DEFAULT_TOP_N_RERANK = 5
#: RRF 融合常数（硬性）
DEFAULT_RRF_K = 60


class RagFilters(BaseModel):
    """检索元数据过滤条件（对应六步流程第 4 步）。"""

    category: str | None = Field(default=None, description="知识分类")
    sku_ids: list[str] = Field(default_factory=list, description="关联 SKU")
    channel: list[str] = Field(default_factory=lambda: ["all"], description="渠道")
    knowledge_type: KnowledgeType | None = Field(
        default=None, description="知识类型"
    )
    authority_min: int | None = Field(
        default=None, description="权威等级下限（1 最高）"
    )
    effective_at: str | None = Field(
        default=None, description="按生效时间过滤，格式 yyyy-MM-dd"
    )

    def is_empty(self) -> bool:
        """是否无有效过滤条件（渠道全量不算条件）。"""
        channels = set(self.channel or [])
        no_channel = not channels or channels == {"all"}
        return (
            self.category is None
            and not self.sku_ids
            and no_channel
            and self.knowledge_type is None
            and self.authority_min is None
            and self.effective_at is None
        )

    def to_milvus_expr(self) -> str | None:
        """转换为 Milvus 布尔表达式；无条件时返回 None。

        注意：**库存 / 价格字段不在向量库中**，因此本表达式不会出现这类字段。
        """
        clauses: list[str] = []
        if self.category:
            clauses.append(f'category == "{self.category}"')
        if self.sku_ids:
            joined = ", ".join(f'"{s}"' for s in self.sku_ids)
            clauses.append(f"ARRAY_CONTAINS_ANY(sku_ids, [{joined}])")
        channels = [c for c in (self.channel or []) if c != "all"]
        if channels:
            joined = ", ".join(f'"{c}"' for c in channels)
            clauses.append(f"ARRAY_CONTAINS_ANY(channel, [{joined}])")
        if self.knowledge_type:
            clauses.append(f'knowledge_type == "{self.knowledge_type.value}"')
        if self.authority_min is not None:
            clauses.append(f"authority >= {self.authority_min}")
        if self.effective_at:
            clauses.append(f'effective_from <= "{self.effective_at}"')
        return " and ".join(clauses) if clauses else None


class RagRetrieveRequest(BaseModel):
    """RAG 检索请求（2.4.2 节）。

    Attributes:
        query: 用户查询原文（检索前会做查询改写）。
        top_k_vector: 向量路召回条数，默认 20。
        top_k_bm25: BM25 路召回条数，默认 20。
        top_n_rerank: 重排后保留条数，默认 5。
        rrf_k: RRF 融合常数，默认 60。
        filters: 元数据过滤。
        use_rewrite: 是否启用查询改写（六步流程第 1 步）。
        use_rerank: 是否启用 Cross-Encoder 重排（第 5 步）。
    """

    query: str = Field(min_length=1, description="查询原文")
    top_k_vector: int = Field(default=DEFAULT_TOP_K_VECTOR, ge=1, le=100)
    top_k_bm25: int = Field(default=DEFAULT_TOP_K_BM25, ge=1, le=100)
    top_n_rerank: int = Field(default=DEFAULT_TOP_N_RERANK, ge=1, le=20)
    rrf_k: int = Field(default=DEFAULT_RRF_K, ge=1, le=1000)
    filters: RagFilters = Field(default_factory=RagFilters)
    use_rewrite: bool = Field(default=True, description="是否查询改写")
    use_rerank: bool = Field(default=True, description="是否重排")

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class RagHit(BaseModel):
    """检索命中。

    Attributes:
        chunk_id: 知识块 ID。
        content: 知识块全文（送入 LLM 的上下文）。
        score: 最终得分（重排后为 Cross-Encoder 分，未重排为 RRF 归一化分）。
        metadata: 元数据。
        sources: 该块来自哪几路召回（`vector` / `bm25`），排障用。
        vector_rank / bm25_rank: 各路原始排名，排障用。
    """

    chunk_id: str = Field(description="知识块 ID")
    content: str = Field(default="", description="知识块全文")
    score: float = Field(ge=0.0, description="最终得分")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    sources: list[str] = Field(default_factory=list, description="召回来源")
    vector_rank: int | None = Field(default=None, description="向量路排名")
    bm25_rank: int | None = Field(default=None, description="BM25 路排名")

    @property
    def category(self) -> str | None:
        """知识分类快捷访问。"""
        value = self.metadata.get("category")
        return str(value) if value is not None else None


class RagThresholds(BaseModel):
    """阈值快照（随响应返回，便于前端与排障核对）。"""

    direct: float = Field(default=0.72, description="直接答阈值")
    fallback: float = Field(default=0.55, description="兜底阈值")

    @classmethod
    def from_settings(cls) -> "RagThresholds":
        """从配置读取阈值。"""
        s = get_settings()
        return cls(direct=s.rag_threshold_direct, fallback=s.rag_threshold_fallback)


class RagRetrieveResponse(BaseModel):
    """RAG 检索响应（2.4.2 节）。

    Attributes:
        hits: 重排后的 Top-N 命中。
        top_score: 最高分。**无命中时为 0.0**，此时 decision 必为 NO_EVIDENCE。
        decision: 阈值分流决策。
        thresholds: 阈值快照。
        rewritten_query: 改写后的查询（便于排障对比）。
        candidates_count: 融合去重后的候选数。
        vector_hits / bm25_hits: 各路召回条数。
        degraded: 降级标识（如 `VECTOR_UNAVAILABLE`）。
        latency_ms: 检索耗时。
        cache_hit: 是否命中检索缓存（监控缓存命中率用）。
    """

    hits: list[RagHit] = Field(default_factory=list, description="Top-N 命中")
    top_score: float = Field(default=0.0, ge=0.0, description="最高分")
    decision: RagDecision = Field(
        default=RagDecision.NO_EVIDENCE, description="阈值分流决策"
    )
    thresholds: RagThresholds = Field(default_factory=RagThresholds.from_settings)
    rewritten_query: str | None = Field(default=None, description="改写后查询")
    candidates_count: int = Field(default=0, ge=0, description="候选数")
    vector_hits: int = Field(default=0, ge=0, description="向量路召回数")
    bm25_hits: int = Field(default=0, ge=0, description="BM25 路召回数")
    degraded: list[str] = Field(default_factory=list, description="降级标识")
    latency_ms: int = Field(default=0, ge=0, description="检索耗时 ms")
    cache_hit: bool = Field(default=False, description="是否命中检索缓存")


def decide(top_score: float, thresholds: RagThresholds | None = None) -> RagDecision:
    """按阈值判定分流决策（**唯一实现，不要在别处重复阈值判断**）。

    Args:
        top_score: 最高相关性得分。
        thresholds: 阈值；None 时从配置读取。

    Returns:
        `DIRECT_ANSWER` / `WEAK` / `NO_EVIDENCE`。

    Note:
        `top_score < 0.55` **必须**返回 `NO_EVIDENCE`，且调用方
        **不得**使用任何低分片段生成回复（BE-05 DoD 自动化断言项）。
    """
    t = thresholds or RagThresholds.from_settings()
    if top_score >= t.direct:
        return RagDecision.DIRECT_ANSWER
    if top_score >= t.fallback:
        return RagDecision.WEAK
    return RagDecision.NO_EVIDENCE


__all__ = [
    "DEFAULT_RRF_K",
    "DEFAULT_TOP_K_BM25",
    "DEFAULT_TOP_K_VECTOR",
    "DEFAULT_TOP_N_RERANK",
    "RagFilters",
    "RagHit",
    "RagRetrieveRequest",
    "RagRetrieveResponse",
    "RagThresholds",
    "decide",
]
