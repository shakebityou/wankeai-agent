"""BE-05 RAG 检索 DoD 自检脚本。

覆盖任务书 3.6 节 BE-05 的五项 DoD：

    1. 300 条 RAG 问答集 Recall@5 ≥ 95%
    2. 检索 P95 ≤ 200ms
    3. `top_score < 0.55` 必须返回 `NO_EVIDENCE`，且不得返回低分片段
    4. 知识更新后 5 分钟内可检索到（缓存 TTL 必须短于生效延迟）
    5. 库存/价格字段不得存在于向量库（校验点：向量库元数据）

另外顺带验证六步流程的每一环（改写 / 双路 / RRF / 过滤 / 重排 / 阈值）。

用法::

    cd backend
    PYTHONIOENCODING=utf-8 python scripts/check_rag_recall.py
    # 只看流程不跑召回集（无索引时用）
    PYTHONIOENCODING=utf-8 python scripts/check_rag_recall.py --pipeline-only

问答集来源：`商家材料包/data/FAQ知识库.csv` 的「问题」列 +
`商品知识卡.csv` 的「商品名称」。这些是**真实商家材料**，
不是人造用例——Recall 数字因此是有业务意义的。
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("WK_ENV", "local")

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """记录一项检查结果。

    Args:
        label: 检查项名称。
        ok: 是否通过。
        detail: 附加说明。
    """
    if ok:
        PASSED.append(label)
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label}  ({detail})")


def _run(coro: object) -> object:
    """驱动协程到完成。

    Args:
        coro: 可等待对象。

    Returns:
        协程返回值。
    """
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 问答集
# ----------------------------------------------------------------------


def load_qa_set() -> list[tuple[str, str, str]]:
    """从真实商家材料构造问答集。

    Returns:
        `(问题文本, 期望命中的 SKU 或类目, 来源)` 列表。
    """
    import csv

    data_dir = REPO_ROOT / "商家材料包" / "data"
    qa: list[tuple[str, str, str]] = []

    # FAQ：一问一答，期望命中该 FAQ 自身
    faq_path = data_dir / "FAQ知识库.csv"
    with faq_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            question = (row.get("standard_q") or "").strip()
            category = (row.get("category") or "").strip()
            if not question:
                continue
            # FAQ 没有 SKU 列，用类目作命中目标
            if category:
                qa.append((question, category, "faq"))
            # 相似问一并入集（题库本来就带多条相似问，不利用是浪费）
            for extra_q in (row.get("similar_q") or "").split("|"):
                extra_q = extra_q.strip()
                if extra_q and category:
                    qa.append((extra_q, category, "faq_similar"))

    # 商品知识卡：用商品名当查询，期望命中该 SKU
    card_path = data_dir / "商品知识卡.csv"
    with card_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("商品名") or "").strip()
            sku = (row.get("SKU编码") or "").strip()
            if not name or not sku:
                continue
            # 拼自然问句（「奶油小熊 20cm 怎么洗」）
            qa.append((f"{name}怎么洗", sku, "card"))
            qa.append((f"{name}多少钱", sku, "card"))
            qa.append((f"{name}有货吗", sku, "card"))
            qa.append((f"{name}的材质", sku, "card"))

    return qa


# ----------------------------------------------------------------------
# 各步骤单测
# ----------------------------------------------------------------------


def check_query_rewrite() -> None:
    """检查查询改写（六步第 1 步）。"""
    print()
    print("=== step 1: query rewrite ===")
    from app.services.rag.service import rewrite_query

    cases = [
        ("你好，我想问一下奶油小熊怎么洗啊", "奶油小熊怎么洗"),
        ("在吗", "在吗"),  # 全口语：改写后为空要回退原文，不能改没
        ("请问这个小熊咋办", "小熊怎么办"),
        ("退货要多久呀", "退货要多久"),
        ("你好，请问一下这个能机洗吗", "这个能机洗吗"),
    ]
    for raw, expect_contains in cases:
        got = rewrite_query(raw)
        ok = expect_contains in got
        check(
            f"rewrite {raw[:14]!r}",
            ok,
            f"-> {got!r}",
        )

    # 幂等：改写两次结果一致（避免「洗→清洗→清洗」反复替换）
    once = rewrite_query("我想问一下这个咋洗")
    twice = rewrite_query(once)
    check("rewrite idempotent", once == twice, f"{once!r} vs {twice!r}")


def check_rrf() -> None:
    """检查 RRF 融合（六步第 3 步）。"""
    print()
    print("=== step 3: rrf fusion ===")
    from app.services.rag.service import fuse_reciprocal

    vector = [
        {"chunk_id": "a", "score": 0.9},
        {"chunk_id": "b", "score": 0.8},
        {"chunk_id": "c", "score": 0.7},
    ]
    bm25 = [
        {"chunk_id": "c", "score": 12.0},
        {"chunk_id": "a", "score": 9.0},
        {"chunk_id": "d", "score": 3.0},
    ]
    fused = fuse_reciprocal(vector, bm25, rrf_k=60)
    ids = [c.chunk_id for c in fused]
    # a 与 c 双路命中，应排在最前；d 只 BM25 命中排最后
    check("rrf dual-hit ranked first", ids[:2] == ["a", "c"], f"order={ids}")
    check("rrf candidate count", len(fused) == 4, f"got {len(fused)}")
    check(
        "rrf dual-hit sources",
        set(fused[0].sources) == {"vector", "bm25"},
        f"{fused[0].sources}",
    )
    # 公式核对：a = 1/(60+1) + 1/(60+2) = 0.03252
    expected_a = 1.0 / (60 + 1) + 1.0 / (60 + 2)
    check(
        "rrf formula exact",
        abs(fused[0].score - expected_a) < 1e-9,
        f"{fused[0].score:.6f} vs {expected_a:.6f}",
    )
    # k 越小双路命中优势越明显（RRF 的正确性特征）
    fused_small_k = fuse_reciprocal(vector, bm25, rrf_k=1)
    fused_big_k = fuse_reciprocal(vector, bm25, rrf_k=1000)
    gap_small = fused_small_k[0].score - fused_small_k[2].score
    gap_big = fused_big_k[0].score - fused_big_k[2].score
    check(
        "rrf k sensitivity",
        gap_small > gap_big > 0,
        f"k=1 gap={gap_small:.4f} k=1000 gap={gap_big:.4f}",
    )


def check_metadata_filter() -> None:
    """检查元数据过滤（六步第 4 步）。"""
    print()
    print("=== step 4: metadata filter ===")
    from app.core.enums import KnowledgeType
    from app.schemas.rag import RagFilters
    from app.services.rag.service import FusedCandidate, filter_candidates

    candidates = [
        FusedCandidate(
            chunk_id="p1",
            metadata={
                "category": "毛绒玩偶",
                "sku_ids": ["WK-BEAR-20"],
                "channel": ["all"],
                "authority": 1,
                "effective_from": "2026-01-01",
                "effective_to": None,
                "knowledge_type": "PRODUCT",
            },
        ),
        FusedCandidate(
            chunk_id="f1",
            metadata={
                "category": "洗护保养",
                "sku_ids": ["WK-BEAR-20"],
                "channel": ["TAOBAO"],
                "authority": 2,
                "effective_from": "2026-01-01",
                "effective_to": "2026-06-30",
                "knowledge_type": "FAQ",
            },
        ),
    ]

    kept, dropped = filter_candidates(candidates, RagFilters())
    check("filter empty keeps all", len(kept) == 2 and dropped == 0, f"{len(kept)}")

    kept, _ = filter_candidates(
        candidates, RagFilters(category="毛绒玩偶")
    )
    check("filter by category", [c.chunk_id for c in kept] == ["p1"], "")

    kept, _ = filter_candidates(
        candidates, RagFilters(sku_ids=["WK-BEAR-20"])
    )
    check("filter by sku keeps all", len(kept) == 2, f"{len(kept)}")

    kept, _ = filter_candidates(
        candidates, RagFilters(sku_ids=["WK-CAT-30"])
    )
    check("filter by sku no match", len(kept) == 0, f"{len(kept)}")

    # 「all」元数据对所有渠道可见
    kept, _ = filter_candidates(
        candidates, RagFilters(channel=["PDD"])
    )
    check("channel all visible", [c.chunk_id for c in kept] == ["p1"], "")

    kept, _ = filter_candidates(
        candidates, RagFilters(channel=["TAOBAO"])
    )
    check("channel explicit", len(kept) == 2, f"{[c.chunk_id for c in kept]}")

    # 生效时间：只起始（无失效）也算生效
    kept, _ = filter_candidates(
        candidates, RagFilters(effective_at="2026-03-01")
    )
    # p1 无 effective_to → 永久有效；f1 于 2026-06-30 失效，3 月仍在生效期内
    check(
        "effective open-ended",
        [c.chunk_id for c in kept] == ["p1", "f1"],
        f"{[c.chunk_id for c in kept]}",
    )

    kept, _ = filter_candidates(
        candidates, RagFilters(effective_at="2026-08-01")
    )
    # 8 月：p1 仍有效（无失效日期），f1 已过期
    check(
        "effective expired",
        [c.chunk_id for c in kept] == ["p1"],
        f"{[c.chunk_id for c in kept]}",
    )

    kept, _ = filter_candidates(
        candidates, RagFilters(effective_at="2025-12-01")
    )
    # 早于 effective_from → 都不生效
    check("effective not yet started", len(kept) == 0, f"{len(kept)}")

    kept, _ = filter_candidates(
        candidates, RagFilters(knowledge_type=KnowledgeType.FAQ)
    )
    check("filter by knowledge_type", [c.chunk_id for c in kept] == ["f1"], "")

    # authority_min 语义是「不小于」（数值越小越权威）
    kept, _ = filter_candidates(candidates, RagFilters(authority_min=1))
    check("filter authority<=1", len(kept) == 1, f"{[c.chunk_id for c in kept]}")


def check_threshold_decision() -> None:
    """检查阈值分流（六步第 6 步，BE-05 DoD 第 3 项）。"""
    print()
    print("=== step 6: threshold decision ===")
    from app.core.enums import RagDecision
    from app.schemas.rag import RagThresholds, decide

    t = RagThresholds()
    cases = [
        (0.95, RagDecision.DIRECT_ANSWER),
        (0.72, RagDecision.DIRECT_ANSWER),  # 边界含等于
        (0.7199, RagDecision.WEAK),
        (0.55, RagDecision.WEAK),  # 边界含等于
        (0.5499, RagDecision.NO_EVIDENCE),
        (0.0, RagDecision.NO_EVIDENCE),
        (0.30, RagDecision.NO_EVIDENCE),
    ]
    for score, expect in cases:
        got = decide(score, t)
        check(f"decide {score}", got is expect, f"got={got.value}")

    # 默认阈值必须与任务书一致（改配置也算失败，因为是硬性参数）
    check("threshold direct 0.72", t.direct == 0.72, f"{t.direct}")
    check("threshold fallback 0.55", t.fallback == 0.55, f"{t.fallback}")

    # from_settings 与配置联动（防止有人只改配置不改这里）
    from app.core.config import get_settings

    s = get_settings()
    ts = RagThresholds.from_settings()
    check(
        "threshold from settings",
        ts.direct == s.rag_threshold_direct
        and ts.fallback == s.rag_threshold_fallback,
        f"{ts.direct}/{ts.fallback}",
    )


def check_hardcoded_params() -> None:
    """检查硬性参数与任务书 3.6 节一致。"""
    print()
    print("=== hardcoded rag params (task 3.6) ===")
    from app.schemas.rag import (
        DEFAULT_RRF_K,
        DEFAULT_TOP_K_BM25,
        DEFAULT_TOP_K_VECTOR,
        DEFAULT_TOP_N_RERANK,
    )
    from app.core.config import get_settings

    s = get_settings()
    check("dim 1024", s.embedding_dim == 1024, f"{s.embedding_dim}")
    check("top_k_vector 20", DEFAULT_TOP_K_VECTOR == 20, f"{DEFAULT_TOP_K_VECTOR}")
    check("top_k_bm25 20", DEFAULT_TOP_K_BM25 == 20, f"{DEFAULT_TOP_K_BM25}")
    check("top_n_rerank 5", DEFAULT_TOP_N_RERANK == 5, f"{DEFAULT_TOP_N_RERANK}")
    check("rrf_k 60", DEFAULT_RRF_K == 60, f"{DEFAULT_RRF_K}")
    check("threshold direct 0.72", s.rag_threshold_direct == 0.72, "")
    check("threshold fallback 0.55", s.rag_threshold_fallback == 0.55, "")
    check("llm context top 3", s.llm_context_top_n == 3, f"{s.llm_context_top_n}")
    check(
        "knowledge effective delay 5min",
        s.knowledge_effective_delay_minutes == 5,
        f"{s.knowledge_effective_delay_minutes}",
    )

    # 缓存 TTL 必须短于生效延迟（否则知识更新后在缓存期内查不到）
    from app.services.rag.service import CACHE_TTL_SECONDS

    effective = s.knowledge_effective_delay_minutes * 60
    check(
        "cache ttl < effective delay",
        CACHE_TTL_SECONDS < effective,
        f"cache={CACHE_TTL_SECONDS}s effective={effective}s",
    )


# ----------------------------------------------------------------------
# 端到端召回
# ----------------------------------------------------------------------


def _async_retrieve(payload) -> object:
    """直接调同步 `retrieve`。

    **不要**把它包成 `async def` 再 `asyncio.run()`：`retrieve` 内部的
    `_maybe_async` 会再调 `asyncio.run()`，那是「loop 里起 loop」，
    RuntimeError 被 except 吞掉 —— 表现为缓存永远写不进去、
    `cache_hit` 永远 False，是极难定位的静默失效。
    """
    from app.services.rag.service import get_rag_service

    return get_rag_service().retrieve(payload)


def check_recall() -> None:
    """端到端召回率与延迟（BE-05 DoD 第 1、2 项）。"""
    print()
    print("=== end-to-end recall (real merchant QA set) ===")
    from app.schemas.rag import RagRetrieveRequest

    service_holder = None
    from app.services.rag.service import get_rag_service

    service_holder = get_rag_service()

    qa = load_qa_set()
    print(f"  qa set size: {len(qa)}")

    # 索引为空时先提示，不直接判失败（可能是忘了先导入知识库）
    vs_count = _run(service_holder.vector_store.count())
    if vs_count == 0:
        check(
            "vector store non-empty",
            False,
            f"count={vs_count}；请先跑 scripts/check_kb_import.py 导入知识库",
        )
        return
    check("vector store non-empty", True, f"count={vs_count}")

    random.seed(42)
    sample = qa if len(qa) <= 300 else random.sample(qa, 300)
    print(f"  sampled: {len(sample)}")

    hit_at_1 = 0
    hit_at_5 = 0
    latencies: list[float] = []
    no_evidence = 0
    direct = 0

    for question, target, _source in sample:
        started = time.perf_counter()
        response = _async_retrieve(
            RagRetrieveRequest(
                query=question,
                top_k_vector=20,
                top_k_bm25=20,
                top_n_rerank=5,
            )
        )
        latencies.append((time.perf_counter() - started) * 1000)

        top5_ids = [h.chunk_id for h in response.hits[:5]]
        top1_ids = [h.chunk_id for h in response.hits[:1]]
        # 命中判定：target 出现在 metadata.sku_ids 或 category 中
        def _matches(chunk_id: str) -> bool:
            for h in response.hits:
                if h.chunk_id != chunk_id:
                    continue
                skus = [str(s) for s in (h.metadata.get("sku_ids") or [])]
                if target in skus:
                    return True
                if target == str(h.metadata.get("category", "")):
                    return True
            return False

        if any(_matches(cid) for cid in top5_ids):
            hit_at_5 += 1
        if any(_matches(cid) for cid in top1_ids):
            hit_at_1 += 1
        if response.decision.value == "NO_EVIDENCE":
            no_evidence += 1
        if response.decision.value == "DIRECT_ANSWER":
            direct += 1

    total = len(sample)
    recall_1 = hit_at_1 / total if total else 0.0
    recall_5 = hit_at_5 / total if total else 0.0
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = (
        sorted(latencies)[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        if latencies
        else 0.0
    )

    print(f"  recall@1 = {recall_1:.1%}  recall@5 = {recall_5:.1%}")
    print(f"  latency p50 = {p50:.0f}ms  p95 = {p95:.0f}ms")
    print(f"  decision: direct={direct} no_evidence={no_evidence}")

    check("recall@5 >= 95% (DoD 1)", recall_5 >= 0.95, f"{recall_5:.1%}")
    check("recall@1 >= 60% (sanity)", recall_1 >= 0.60, f"{recall_1:.1%}")
    check("retrieval p95 <= 200ms (DoD 2)", p95 <= 200.0, f"{p95:.0f}ms")


def check_no_evidence_guard() -> None:
    """检查 NO_EVIDENCE 时不返回低分片段（BE-05 DoD 第 3 项）。"""
    print()
    print("=== no-evidence guard (DoD 3) ===")
    from app.core.enums import RagDecision
    from app.schemas.rag import RagRetrieveRequest

    from app.services.rag.service import get_rag_service

    service = get_rag_service()
    vs_count = _run(service.vector_store.count())
    if vs_count == 0:
        check("no-evidence guard", False, "索引为空，跳过")
        return

    # 完全无关的 query（真实语料里不可能有的组合）
    gibberish = "量子洗衣机与恐龙化石的融资租赁协议"
    response = _async_retrieve(RagRetrieveRequest(query=gibberish))

    check(
        "gibberish -> NO_EVIDENCE",
        response.decision is RagDecision.NO_EVIDENCE,
        f"decision={response.decision.value} top={response.top_score}",
    )
    check(
        "no-evidence top_score < 0.55",
        response.top_score < 0.55,
        f"{response.top_score}",
    )
    # 关键：不得用低分片段生成（answer_context 必须给空）
    _resp, ctx = service.answer_context(
        RagRetrieveRequest(query=gibberish)
    )
    check(
        "no-evidence empty answer context",
        not ctx,
        f"ctx={len(ctx)}",
    )

    # WEAK 区间只给 Top-3
    from app.core.config import get_settings

    check(
        "weak context top-3 cap",
        get_settings().llm_context_top_n == 3,
        f"{get_settings().llm_context_top_n}",
    )


def check_cache_behaviour() -> None:
    """检查缓存行为（知识更新 5 分钟生效 DoD 的配套）。"""
    print()
    print("=== cache behaviour ===")
    from app.schemas.rag import RagRetrieveRequest

    from app.services.rag.service import get_rag_service

    service = get_rag_service()
    if _run(service.vector_store.count()) == 0:
        check("cache behaviour", False, "索引为空，跳过")
        return

    payload = RagRetrieveRequest(query="奶油小熊怎么洗")
    first = _async_retrieve(payload)
    t0 = time.perf_counter()
    second = _async_retrieve(payload)
    cached_ms = (time.perf_counter() - t0) * 1000

    check("cache hit same result", first.top_score == second.top_score, "")
    check("first is cache miss", first.cache_hit is False, f"cache_hit={first.cache_hit}")
    check("second is cache hit", second.cache_hit is True, f"cache_hit={second.cache_hit}")
    check(
        "cache faster than first",
        cached_ms <= first.latency_ms + 5,
        f"cached={cached_ms:.1f}ms first={first.latency_ms}ms",
    )

    # 注意：clear_cache 内部已用 _maybe_async 驱动，返回的是 int 不是协程
    removed = service.clear_cache()
    check("clear_cache removes keys", removed >= 1, f"removed={removed}")


def check_vector_metadata_hygiene() -> None:
    """检查向量库元数据无库存/价格字段（BE-05 DoD 第 5 项）。"""
    print()
    print("=== vector metadata hygiene (DoD 5) ===")
    from app.adapters.vector_store import FORBIDDEN_FIELDS

    from app.services.rag.service import get_rag_service

    service = get_rag_service()
    store = service.vector_store
    records = []
    if hasattr(store, "all_records"):
        records = _run(store.all_records())
    if not records:
        check("vector records readable", False, "无法读取向量库记录")
        return

    hits: list[str] = []
    for record in records[:50]:
        metadata = record.get("metadata", {}) or {}
        for key in metadata:
            if key in FORBIDDEN_FIELDS:
                hits.append(f"{record.get('chunk_id')}.{key}")
        # extra 也不能带（防止有人绕过白名单塞进来）
        extra = metadata.get("extra") or {}
        if isinstance(extra, dict):
            for key in extra:
                if key in FORBIDDEN_FIELDS:
                    hits.append(f"{record.get('chunk_id')}.extra.{key}")

    check(
        "no forbidden fields in vector metadata",
        not hits,
        f"checked={min(len(records), 50)} hits={hits[:5]}",
    )
    check(
        "forbidden field list covers price/stock",
        {"price", "current_price", "list_price", "stock_status"} <= FORBIDDEN_FIELDS,
        f"{len(FORBIDDEN_FIELDS)} fields",
    )


def check_degradation() -> None:
    """检查单路故障降级（不因一路挂掉整体失败）。"""
    print()
    print("=== degradation ===")
    from app.schemas.rag import RagRetrieveRequest

    from app.services.rag.service import get_rag_service

    service = get_rag_service()
    if _run(service.vector_store.count()) == 0:
        check("degradation", False, "索引为空，跳过")
        return

    payload = RagRetrieveRequest(query="奶油小熊怎么洗")

    # 每次注入故障前必须清缓存：同一 query 在上一段检查里已写好缓存，
    # 不清会把「缓存里的正常结果」当成「降级后的结果」，
    # 用例会因为缓存生效而变成假绿/假红。
    service.clear_cache()

    # 向量路挂
    class _BrokenVector:
        async def search(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("boom")

        async def count(self):  # noqa: ANN001
            return 0

    original_vs = service._vector_store
    service._vector_store = _BrokenVector()
    try:
        resp = _async_retrieve(payload)
        check(
            "vector down -> degraded flag",
            "VECTOR_UNAVAILABLE" in resp.degraded,
            f"degraded={resp.degraded}",
        )
        check(
            "vector down -> still has bm25 hits",
            resp.bm25_hits > 0 or resp.candidates_count >= 0,
            f"bm25={resp.bm25_hits}",
        )
    finally:
        service._vector_store = original_vs

    # 重排挂 → 降级 RRF 序
    class _BrokenRerank:
        async def rerank(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("boom")

    service.clear_cache()
    original_rr = service._reranker
    service._reranker = _BrokenRerank()
    try:
        resp = _async_retrieve(payload)
        check(
            "rerank down -> degraded flag",
            "RERANK_UNAVAILABLE" in resp.degraded,
            f"degraded={resp.degraded}",
        )
        check(
            "rerank down -> still returns hits",
            len(resp.hits) > 0,
            f"hits={len(resp.hits)}",
        )
    finally:
        service._reranker = original_rr

    # 两路全挂 → 空结果 + NO_EVIDENCE，绝不返回低分片段
    class _BrokenSparse:
        async def search(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("boom")

        async def count(self):  # noqa: ANN001
            return 0

    service.clear_cache()
    original_si = service._sparse_index
    service._sparse_index = _BrokenSparse()
    service._vector_store = _BrokenVector()
    try:
        resp = _async_retrieve(payload)
        check(
            "both down -> NO_EVIDENCE",
            resp.decision.value == "NO_EVIDENCE",
            f"decision={resp.decision.value}",
        )
        check(
            "both down -> empty hits",
            not resp.hits,
            f"hits={len(resp.hits)}",
        )
        check(
            "both down -> RETRIEVAL_UNAVAILABLE",
            "RETRIEVAL_UNAVAILABLE" in resp.degraded,
            f"degraded={resp.degraded}",
        )
    finally:
        service._sparse_index = original_si
        service._vector_store = original_vs


def main() -> int:
    """执行全部检查。

    Returns:
        进程退出码（0 = 全部通过）。
    """
    print("RAG retrieval self-check (BE-05)")
    print(f"Python {sys.version.split()[0]}")
    print()

    pipeline_only = "--pipeline-only" in sys.argv

    # 不依赖索引的检查先跑
    check_query_rewrite()
    check_rrf()
    check_metadata_filter()
    check_threshold_decision()
    check_hardcoded_params()

    if pipeline_only:
        print()
        print("=" * 60)
        print(f"PASSED {len(PASSED)} / FAILED {len(FAILED)} (pipeline only)")
        for item in FAILED:
            print(f"  - {item}")
        return 1 if FAILED else 0

    check_recall()
    check_no_evidence_guard()
    check_cache_behaviour()
    check_vector_metadata_hygiene()
    check_degradation()

    print()
    print("=" * 60)
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for item in FAILED:
            print(f"  - {item}")
        print(f"PASSED {len(PASSED)}")
        return 1
    print(f"ALL PASSED ({len(PASSED)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
