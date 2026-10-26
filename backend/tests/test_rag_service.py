"""BE-05 RAG 检索服务单测（任务书 3.6 节 DoD 的可自动化部分）。

覆盖要点：
* 六步流程中的**纯函数**：改写 / RRF 融合 / 元数据过滤 / 阈值分流。
* `RagService` 的编排行为：双路降级、缓存命中、NO_EVIDENCE 不返回低分片段。
* 契约硬参数（1024 维、Top20/Top20、Top5、0.72/0.55、k=60）不可被改动。

设计约定：
* **不连真实向量库/ES**——用内存假实现注入，用例毫秒级完成。
* 阈值用例必须覆盖**边界值本身**（0.72、0.55），任务书对这两个数字
  是硬性要求，`0.7199` 与 `0.72` 的分流结果不同属预期行为。
* `filter_candidates` 接收的是 `FusedCandidate` dataclass（不是裸 dict），
  用例必须构造真实类型，否则永远测不到线上走的代码分支。
"""

from __future__ import annotations

import pytest

from app.core.enums import RagDecision

# ---------------------------------------------------------------------------
# 第 1 步：查询改写
# ---------------------------------------------------------------------------


class TestRewriteQuery:
    """查询改写（去口语词 + 同义归一）。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("你好，请问这个小熊怎么洗", "这个小熊怎么洗"),
            ("在吗在吗", "在吗在吗"),
            ("麻烦问一下发货多久", "发货多久"),
            ("你好，我想问一下奶油小熊怎么洗啊", "奶油小熊怎么洗"),
            ("我想问一下怎么退款", "怎么退款"),
        ],
    )
    def test_strips_oral_prefix(self, raw: str, expected: str) -> None:
        """口语前缀应被剥离（含叠用的「你好，我想问一下」）。"""
        from app.services.rag.service import rewrite_query

        assert rewrite_query(raw) == expected

    def test_returns_original_when_all_stripped(self) -> None:
        """全口语的 query 改写后为空——必须回退原文，不能改没。"""
        from app.services.rag.service import rewrite_query

        assert rewrite_query("你好") == "你好"

    def test_idempotent(self) -> None:
        """改写必须幂等（缓存 key 依赖它，不幂等会导致缓存永不命中）。"""
        from app.services.rag.service import rewrite_query

        once = rewrite_query("你好，请问这个怎么洗")
        assert rewrite_query(once) == once

    def test_disabled_returns_original(self) -> None:
        """`use_rewrite=False` 时原样返回（排障对比用）。"""
        from app.services.rag.service import rewrite_query

        raw = "你好，请问这个小熊怎么洗"
        assert rewrite_query(raw, enabled=False) == raw


# ---------------------------------------------------------------------------
# 第 3 步：RRF 融合
# ---------------------------------------------------------------------------


def _raw(chunk_id: str, score: float) -> dict:
    """构造一路召回结果的原始字典。

    Args:
        chunk_id: 块 ID。
        score: 该路分数。

    Returns:
        召回结果字典。
    """
    return {"chunk_id": chunk_id, "content": chunk_id, "metadata": {}, "score": score}


class TestFuseReciprocal:
    """RRF 融合（`score = Σ 1/(k + rank)`，k=60）。"""

    def test_both_lists_hit_ranks_first(self) -> None:
        """两路都命中的候选必须排在最前。"""
        from app.services.rag.service import fuse_reciprocal

        fused = fuse_reciprocal(
            [_raw("a", 0.9), _raw("b", 0.8)],
            [_raw("c", 5.0), _raw("a", 4.0)],
            rrf_k=60,
        )
        assert fused[0].chunk_id == "a"

    def test_sources_recorded(self) -> None:
        """融合结果要记录命中来源，便于排障。"""
        from app.services.rag.service import fuse_reciprocal

        fused = fuse_reciprocal([_raw("a", 0.9)], [_raw("a", 4.0)], rrf_k=60)
        assert set(fused[0].sources) == {"vector", "bm25"}

    def test_formula_exact(self) -> None:
        """公式必须与契约一致：两路各 rank=1 时 score = 2/(60+1)。"""
        from app.services.rag.service import fuse_reciprocal

        fused = fuse_reciprocal([_raw("a", 0.9)], [_raw("a", 4.0)], rrf_k=60)
        assert fused[0].score == pytest.approx(2 / 61, abs=1e-9)

    def test_single_route_formula(self) -> None:
        """单路 rank=1 时 score = 1/(60+1)。"""
        from app.services.rag.service import fuse_reciprocal

        fused = fuse_reciprocal([_raw("a", 0.9)], [], rrf_k=60)
        assert fused[0].score == pytest.approx(1 / 61, abs=1e-9)

    def test_dedupes_by_chunk_id(self) -> None:
        """同一 chunk 在多路出现只保留一条。"""
        from app.services.rag.service import fuse_reciprocal

        fused = fuse_reciprocal([_raw("a", 0.9)], [_raw("a", 4.0)], rrf_k=60)
        assert len(fused) == 1

    def test_both_empty(self) -> None:
        """两路都空时返回空列表（由上层判 NO_EVIDENCE）。"""
        from app.services.rag.service import fuse_reciprocal

        assert fuse_reciprocal([], [], rrf_k=60) == []


# ---------------------------------------------------------------------------
# 第 4 步：元数据过滤
# ---------------------------------------------------------------------------


def _cand(
    chunk_id: str,
    *,
    category: str = "毛绒玩偶",
    sku_ids: list[str] | None = None,
    channel: list[str] | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
    score: float = 0.9,
):
    """构造一条融合候选（真实 `FusedCandidate` 类型）。

    Args:
        chunk_id: 块 ID。
        category: 类目。
        sku_ids: 关联 SKU。
        channel: 可见渠道。
        effective_from: 生效起始日。
        effective_to: 生效截止日。
        score: 融合分。

    Returns:
        `FusedCandidate` 实例。
    """
    from app.services.rag.service import FusedCandidate

    return FusedCandidate(
        chunk_id=chunk_id,
        content=chunk_id,
        metadata={
            "category": category,
            "sku_ids": sku_ids or [],
            "channel": channel or ["all"],
            "effective_from": effective_from,
            "effective_to": effective_to,
        },
        score=score,
    )


class TestFilterCandidates:
    """元数据过滤（类目 / SKU / 渠道 / 生效时间）。"""

    @pytest.fixture
    def candidates(self):
        """三条候选：类目不同、生效期不同。"""
        return [
            _cand("p1", category="毛绒玩偶", sku_ids=["WK-BEAR-20"], effective_from="2026-01-01"),
            _cand(
                "f1",
                category="洗护保养",
                effective_from="2026-01-01",
                effective_to="2026-06-30",
            ),
            _cand(
                "m1",
                category="营销活动",
                channel=["TAOBAO"],
                effective_from="2026-10-01",
                effective_to="2026-11-11",
            ),
        ]

    def test_empty_filter_keeps_all(self, candidates) -> None:
        """空过滤条件不丢任何候选。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, dropped = filter_candidates(candidates, RagFilters())
        assert len(kept) == 3
        assert dropped == 0

    def test_filter_by_category(self, candidates) -> None:
        """按类目过滤。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, dropped = filter_candidates(candidates, RagFilters(category="毛绒玩偶"))
        assert [c.chunk_id for c in kept] == ["p1"]
        assert dropped == 2

    def test_filter_by_sku(self, candidates) -> None:
        """按 SKU 过滤：只有命中的块保留（sku_ids 为空视为通用知识）。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, _ = filter_candidates(candidates, RagFilters(sku_ids=["WK-BEAR-20"]))
        ids = [c.chunk_id for c in kept]
        assert "p1" in ids
        assert "m1" not in ids

    def test_effective_open_ended_kept(self, candidates) -> None:
        """无 effective_to 表示永久有效，任何时间点都保留。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, _ = filter_candidates(
            candidates, RagFilters(effective_at="2030-01-01")
        )
        assert "p1" in [c.chunk_id for c in kept]

    def test_effective_expired_dropped(self, candidates) -> None:
        """超过 effective_to 的候选必须被过滤（避免答已下架政策）。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, _ = filter_candidates(candidates, RagFilters(effective_at="2026-08-01"))
        assert "f1" not in [c.chunk_id for c in kept]

    def test_effective_not_yet_kept_out(self, candidates) -> None:
        """早于 effective_from 的候选不生效（活动未开始不能提前答）。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, _ = filter_candidates(candidates, RagFilters(effective_at="2025-12-01"))
        assert kept == []

    def test_channel_all_matches_any(self, candidates) -> None:
        """`channel=["all"]` 的知识对所有渠道可见。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        kept, _ = filter_candidates(candidates, RagFilters(channel=["PDD"]))
        assert "p1" in [c.chunk_id for c in kept]
        assert "m1" not in [c.chunk_id for c in kept]

    def test_channel_native_list(self) -> None:
        """渠道知识只在声明过的渠道可见（含通道枚举原值）。"""
        from app.schemas.rag import RagFilters
        from app.services.rag.service import filter_candidates

        c = _cand("tb", channel=["TAOBAO"])
        kept, _ = filter_candidates([c], RagFilters(channel=["TAOBAO"]))
        assert len(kept) == 1
        kept, _ = filter_candidates([c], RagFilters(channel=["PDD"]))
        assert kept == []


# ---------------------------------------------------------------------------
# 第 6 步：阈值分流
# ---------------------------------------------------------------------------


class TestDecide:
    """阈值分流（0.72 直接答 / 0.55 兜底 / 以下 NO_EVIDENCE）。"""

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.96, RagDecision.DIRECT_ANSWER),
            (0.72, RagDecision.DIRECT_ANSWER),  # 边界含等号
            (0.7199, RagDecision.WEAK),
            (0.60, RagDecision.WEAK),
            (0.55, RagDecision.WEAK),  # 边界含等号
            (0.5499, RagDecision.NO_EVIDENCE),
            (0.0, RagDecision.NO_EVIDENCE),
        ],
    )
    def test_thresholds(self, score: float, expected: RagDecision) -> None:
        """两个阈值边界都不能差一位（契约硬性要求）。"""
        from app.services.rag.service import decide

        assert decide(score) is expected

    def test_thresholds_from_settings(self, settings) -> None:
        """阈值必须来自配置，不能在别处硬编码。"""
        assert settings.rag_threshold_direct == 0.72
        assert settings.rag_threshold_fallback == 0.55


# ---------------------------------------------------------------------------
# 硬参数不可被改动（任务书 3.6 节「参数不可改」）
# ---------------------------------------------------------------------------


class TestHardParams:
    """契约锁定的检索参数。"""

    def test_dims_and_tops(self, settings) -> None:
        """向量维度与各路召回数。"""
        assert settings.embedding_dim == 1024
        assert settings.rag_top_k_vector == 20
        assert settings.rag_top_k_bm25 == 20
        assert settings.rag_top_n_rerank == 5

    def test_rrf_k(self, settings) -> None:
        """RRF k 必须是 60。"""
        assert settings.rag_rrf_k == 60

    def test_llm_context_top_n(self, settings) -> None:
        """送模型的上下文只取 Top-3（成本控制硬指标）。"""
        assert settings.llm_context_top_n == 3

    def test_cache_ttl_shorter_than_effective_delay(self, settings) -> None:
        """缓存 TTL 必须短于「知识生效延迟」要求，否则更新后查不到。

        注意：`answer_cache_ttl_seconds` 是 **LLM 答案缓存**（1 小时，
        任务书 3.5 节 BE-16 明确要求 1h），与 RAG 检索缓存 TTL
        （`RAG_CACHE_TTL_SECONDS`=120s）是两套东西。
        真正约束「知识更新 5 分钟内生效」的是后者。
        """
        from app.services.rag.service import CACHE_TTL_SECONDS

        assert CACHE_TTL_SECONDS <= settings.knowledge_effective_delay_minutes * 60


# ---------------------------------------------------------------------------
# 服务编排：降级与 NO_EVIDENCE 守卫
# ---------------------------------------------------------------------------


class _FakeStore:
    """假向量库（可控返回 / 可控故障）。"""

    def __init__(self, *, hits: list[dict] | None = None, fail: bool = False) -> None:
        self._hits = hits or []
        self._fail = fail

    async def search(self, *args, **kwargs) -> list[dict]:
        """返回预置命中或抛错。"""
        if self._fail:
            raise RuntimeError("vector store down")
        return list(self._hits)

    async def count(self) -> int:
        """条目数。"""
        return len(self._hits)


class _FakeSparse:
    """假稀疏索引。"""

    def __init__(self, *, hits: list[dict] | None = None, fail: bool = False) -> None:
        self._hits = hits or []
        self._fail = fail

    async def search(self, *args, **kwargs) -> list[dict]:
        """返回预置命中或抛错。"""
        if self._fail:
            raise RuntimeError("sparse index down")
        return list(self._hits)

    async def count(self) -> int:
        """条目数。"""
        return len(self._hits)


class _FakeReranker:
    """假重排器：原序截断（按 RRF 序，便于断言）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def rerank(self, query: str, candidates, top_n: int = 5):
        """原序取前 N，不做真实打分。"""
        if self._fail:
            raise RuntimeError("reranker down")
        return list(candidates)[:top_n]


class _SeqEmbedder:
    """假嵌入器（返回定长零向量，维度取配置值）。"""

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    async def embed(self, texts):
        """返回 N×dim 的零向量。"""
        return [[0.0] * self._dim for _ in texts]


def _hit(chunk_id: str, score: float, *, category: str = "毛绒玩偶") -> dict:
    """构造一条召回命中。

    Args:
        chunk_id: 块 ID。
        score: 分数。
        category: 类目。

    Returns:
        命中字典。
    """
    return {
        "chunk_id": chunk_id,
        "content": f"内容 {chunk_id}",
        "metadata": {"category": category, "sku_ids": [], "channel": ["all"]},
        "score": score,
    }


class TestRagServiceBehaviour:
    """服务编排行为（降级 / 缓存 / NO_EVIDENCE 守卫）。"""

    def _service(self, *, vector=None, sparse=None, reranker=None, cache=None):
        """构造注入了假依赖的服务实例。

        Args:
            vector: 向量库假实现。
            sparse: 稀疏索引假实现。
            reranker: 重排器假实现。
            cache: 缓存假实现。

        Returns:
            `RagService` 实例。
        """
        from app.services.rag.service import RagService

        return RagService(
            vector_store=vector,
            sparse_index=sparse,
            reranker=reranker,
            embedder=_SeqEmbedder(),
            cache=cache,
        )

    def test_both_paths_down_returns_no_evidence(self) -> None:
        """两路全挂必须 NO_EVIDENCE —— 绝不能返回低分片段。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(fail=True),
            sparse=_FakeSparse(fail=True),
            reranker=_FakeReranker(),
        )
        resp = svc.retrieve(RagRetrieveRequest(query="奶油小熊怎么洗"))
        assert resp.decision is RagDecision.NO_EVIDENCE
        assert resp.hits == []
        assert "RETRIEVAL_UNAVAILABLE" in resp.degraded

    def test_vector_down_still_uses_bm25(self) -> None:
        """向量路挂掉时稀疏路仍能出结果（单路降级）。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(fail=True),
            sparse=_FakeSparse(hits=[_hit("b1", 0.9)]),
            reranker=_FakeReranker(),
        )
        resp = svc.retrieve(RagRetrieveRequest(query="奶油小熊怎么洗"))
        assert "VECTOR_UNAVAILABLE" in resp.degraded
        assert resp.hits

    def test_rerank_down_falls_back_to_rrf_order(self) -> None:
        """重排器挂掉时用 RRF 顺序截断（不整体失败）。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(hits=[_hit("v1", 0.9)]),
            sparse=_FakeSparse(hits=[_hit("b1", 0.9)]),
            reranker=_FakeReranker(fail=True),
        )
        resp = svc.retrieve(RagRetrieveRequest(query="奶油小熊怎么洗"))
        assert "RERANK_UNAVAILABLE" in resp.degraded
        assert len(resp.hits) <= 5

    def test_low_score_produces_no_evidence(self) -> None:
        """最高分低于兜底阈值时不得返回片段（防幻觉第一道闸）。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(hits=[_hit("v1", 0.3)]),
            sparse=_FakeSparse(hits=[]),
            reranker=_FakeReranker(),
        )
        resp = svc.retrieve(RagRetrieveRequest(query="无关问题"))
        assert resp.decision is RagDecision.NO_EVIDENCE

    def test_no_evidence_yields_empty_answer_context(self) -> None:
        """NO_EVIDENCE 时交给生成层的上下文必须为空。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(hits=[_hit("v1", 0.2)]),
            sparse=_FakeSparse(hits=[]),
            reranker=_FakeReranker(),
        )
        _resp, ctx = svc.answer_context(RagRetrieveRequest(query="无关问题"))
        assert ctx == []

    def test_answer_context_capped_at_top3(self) -> None:
        """送模型的上下文最多 3 条（成本控制硬指标）。"""
        from app.schemas.rag import RagRetrieveRequest

        hits = [_hit(f"v{i}", 0.9 - i * 0.01) for i in range(5)]
        svc = self._service(
            vector=_FakeStore(hits=hits),
            sparse=_FakeSparse(hits=[]),
            reranker=_FakeReranker(),
        )
        _resp, ctx = svc.answer_context(RagRetrieveRequest(query="奶油小熊怎么洗"))
        assert len(ctx) <= 3

    def test_cache_hit_marks_flag(self, cache) -> None:
        """第二次相同检索必须命中缓存并标记 `cache_hit`。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(hits=[_hit("v1", 0.9)]),
            sparse=_FakeSparse(hits=[_hit("b1", 0.9)]),
            reranker=_FakeReranker(),
            cache=cache,
        )
        payload = RagRetrieveRequest(query="奶油小熊怎么洗")
        first = svc.retrieve(payload)
        second = svc.retrieve(payload)
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert first.top_score == second.top_score

    def test_clear_cache(self, cache) -> None:
        """清缓存后必须回落到真实检索。"""
        from app.schemas.rag import RagRetrieveRequest

        svc = self._service(
            vector=_FakeStore(hits=[_hit("v1", 0.9)]),
            sparse=_FakeSparse(hits=[_hit("b1", 0.9)]),
            reranker=_FakeReranker(),
            cache=cache,
        )
        payload = RagRetrieveRequest(query="奶油小熊怎么洗")
        svc.retrieve(payload)
        assert svc.clear_cache() >= 1
        assert svc.retrieve(payload).cache_hit is False
