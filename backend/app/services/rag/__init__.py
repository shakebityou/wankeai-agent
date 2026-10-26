"""RAG 检索服务（BE-05）。

六步混合检索流程：查询改写 → 双路召回（向量 20 ‖ BM25 20）→
RRF 融合（k=60）→ 元数据过滤 → Cross-Encoder 重排（Top-5）→
阈值分流（0.72 / 0.55）。
"""

from app.services.rag.service import (
    CACHE_TTL_SECONDS,
    FusedCandidate,
    RagService,
    filter_candidates,
    fuse_reciprocal,
    get_rag_service,
    rewrite_query,
    set_rag_service,
)

__all__ = [
    "CACHE_TTL_SECONDS",
    "FusedCandidate",
    "RagService",
    "filter_candidates",
    "fuse_reciprocal",
    "get_rag_service",
    "rewrite_query",
    "set_rag_service",
]
