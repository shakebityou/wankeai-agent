"""RAG 检索服务（任务书 BE-05 / 契约 2.4.2 节）。

**六步流程（硬性，顺序不可调）**：

    1. 查询改写        query rewrite（口语化/带指代的 query → 可检索 query）
    2. 双路召回        向量 Top-20 ‖ BM25 Top-20（**并行**）
    3. RRF 融合        k=60，按 Σ 1/(k + rank_i) 融合去重
    4. 元数据过滤      category / sku_ids / channel / effective_at
    5. Cross-Encoder 重排  → Top-5
    6. 阈值分流        ≥0.72 DIRECT_ANSWER / ≥0.55 WEAK / <0.55 NO_EVIDENCE

**为什么不把过滤放在召回前**：过滤条件可能把某一路的 20 条全部筛掉
（如按 SKU 过滤时该 SKU 只有 3 条知识）。先召回后过滤保证
「只要库里有相关内容就一定能捞到」，代价是多读一些候选。

**降级策略**（单路故障不导致整体检索失败）：
    * 向量路挂 → `VECTOR_UNAVAILABLE`，只用 BM25 结果
    * BM25 路挂 → `SPARSE_UNAVAILABLE`，只用向量结果
    * 重排挂   → `RERANK_UNAVAILABLE`，用 RRF 融合序取 Top-N
    * 全挂     → 空结果 + `NO_EVIDENCE`，绝不返回低分片段冒充有依据

**BE-05 DoD 映射**：
    * Recall@5 ≥ 95%（300 条问答集）→ `scripts/check_rag_recall.py`
    * 检索 P95 ≤ 200ms → 同上脚本统计
    * `top_score < 0.55` 必须 `NO_EVIDENCE` 且不得用低分片段生成
      → `decide()` 是唯一阈值实现；`answer_context()` 在
        `NO_EVIDENCE` 时返回空上下文，从源头断掉「无依据生成」
    * 知识更新 5 分钟内可检索 → `_CACHE_TTL` 短于生效延迟，
      且 `reindex` 后主动清缓存
    * 库存/价格字段不得存在于向量库 → 校验点在上游
      `app.services.knowledge.service._vector_metadata()`
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.adapters.embedding import get_embedder, normalize_text
from app.adapters.reranker import get_reranker
from app.adapters.sparse_index import get_sparse_index
from app.adapters.vector_store import get_vector_store
from app.core.config import get_settings
from app.core.enums import RagDecision
from app.core.logging import get_logger, mask_text
from app.schemas.rag import (
    RagFilters,
    RagHit,
    RagRetrieveRequest,
    RagRetrieveResponse,
    RagThresholds,
    decide,
)

logger = get_logger(__name__)

#: 检索结果缓存 TTL（秒）。**必须小于** `knowledge_effective_delay_minutes`
#: 对应的 300 秒，否则知识更新后在缓存期内查不到（违反 5 分钟生效 DoD）。
CACHE_TTL_SECONDS = 120

#: 缓存 key 前缀
_CACHE_PREFIX = "rag:retrieve"


# ----------------------------------------------------------------------
# 查询改写
# ----------------------------------------------------------------------

#: 口语化前缀 → 去掉后更利于检索（这些词在知识库里几乎不出现）
#: 顺序有讲究：**长的放前面**。循环剥离是按顺序逐个试 `startswith`，
#: 「请问一下」若排在「请问」之后，「请问一下这个怎么洗」会先被
#: 「请问」吃掉，留下「一下这个怎么洗」——反而制造了新的噪声词。
_ORAL_PREFIXES = (
    "我想问一下",
    "我想问下",
    "麻烦问一下",
    "麻烦问下",
    "请问一下",
    "请问下",
    "想请问一下",
    "问一下",
    "问下",
    "我想问",
    "想问一下",
    "想问下",
    "想请问",
    "请问",
    "打扰一下",
    "在吗",
    "你好",
    "您好",
    "哈喽",
    "hello",
    "hi",
)

#: 语气词/语气助词（检索噪声）
_ORAL_SUFFIXES = (
    "谢谢",
    "谢了",
    "拜托",
    "拜托了",
    "好吗",
    "好不好",
    "可以吗",
    "行吗",
    "呢",
    "啊",
    "呀",
    "哦",
    "噢",
    "哈",
    "嘛",
    "吧",
    "诶",
    "欸",
)

#: 常见同义/别称映射（key 归一化后的小写形态）
#: **只做「库内更常见的说法」→ 不制造库中没有的词**：
#: 「机洗」在知识卡里常写作「机洗/洗衣机清洗」，而「洗」单字映射到
#: 「清洗」会把「能机洗吗」改成「能机清洗吗」——反而两边都不像。
#: 同义映射的验收标准是「改写后 query 在库内的字面命中率上升」，
#: 不是「看起来更通顺」，所以宁少勿滥。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "咋办": ("怎么办",),
    "咋": ("怎么",),
    "啥": ("什么",),
    "咋样": ("怎么样",),
    "掉毛": ("掉毛", "脱毛"),
    "起球": ("起球", "起毛球"),
    "退货": ("退货", "退款", "退回"),
    "换货": ("换货", "更换"),
    "运费险": ("运费险", "退货运费"),
    "快递": ("快递", "物流", "速递"),
    "娃娃": ("娃娃", "玩偶", "公仔"),
    "小熊": ("小熊",),
}


def rewrite_query(query: str, *, enabled: bool = True) -> str:
    """查询改写（六步第 1 步）。

    做三件事：去口语前缀/语气词、同义归一、压缩空白。
    **刻意不做**指代消解——那需要会话上下文，属于 BE-03 会话服务的职责，
    检索服务只拿到已经消解过的 query（编排层传入）。

    Args:
        query: 原始查询。
        enabled: False 时原样返回（用于对比测试改写收益）。

    Returns:
        改写后的查询。改写后为空串时返回原文（避免把 query 改没）。
    """
    if not enabled:
        return query
    text = normalize_text(query or "")
    if not text:
        return query

    # 1) 去口语前缀。**循环而非单次**——用户会叠加礼貌用语
    #    （「你好，我想问一下…」），只判一次 startswith 会把
    #    「我想问一下」漏在句子里，改写等于没做。
    stripped = True
    while stripped:
        stripped = False
        for prefix in _ORAL_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip("，,。.！!？?、 ")
                stripped = True
                break

    # 2) 去语气词后缀（可反复，如「可以吗谢谢」）
    changed = True
    while changed:
        changed = False
        for suffix in _ORAL_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip("，,。.！!？?、 ")
                changed = True

    # 3) 同义归一：把 query 里的别称替换为库内更常见的说法。
    #    用「包含即替换」而非分词替换，保证「小熊咋办」这类连续口语也能命中。
    for alias, canon_list in _SYNONYMS.items():
        if alias in text:
            for canon in canon_list:
                if canon != alias and canon in text:
                    break
            else:
                text = text.replace(alias, canon_list[0])

    text = " ".join(text.split())
    return text or (query or "")


# ----------------------------------------------------------------------
# RRF 融合
# ----------------------------------------------------------------------


@dataclass
class FusedCandidate:
    """RRF 融合后的候选。

    Attributes:
        chunk_id: 知识块 ID。
        content: 正文。
        title: 标题。
        metadata: 元数据。
        score: RRF 融合分（未重排时作为最终分）。
        vector_rank: 向量路排名（1 起）。
        bm25_rank: BM25 路排名（1 起）。
        sources: 命中来源列表。
    """

    chunk_id: str
    content: str = ""
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    vector_rank: int | None = None
    bm25_rank: int | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为普通字典（适配器/重排的输入格式）。"""
        return {
            "chunk_id": self.chunk_id,
            "content": self.content,
            "title": self.title,
            "metadata": dict(self.metadata),
            "score": self.score,
            "sources": list(self.sources),
            "vector_rank": self.vector_rank,
            "bm25_rank": self.bm25_rank,
        }


def fuse_reciprocal(
    vector_hits: Sequence[dict[str, Any]],
    bm25_hits: Sequence[dict[str, Any]],
    *,
    rrf_k: int,
) -> list[FusedCandidate]:
    """RRF（Reciprocal Rank Fusion）融合去重。

    公式（任务书 3.6 节硬性）::

        score(d) = Σ_{i∈{vector, bm25}} 1 / (k + rank_i(d))

    **为什么用 RRF 而不是加权求和**：两路分数量纲不同
    （余弦相似度 ∈ [-1,1]，BM25 分数无上界），加权求和需要调权重，
    换 embedding 模型或换语料就要重新调。RRF 只用**排名**，
    天然无量纲，换任何一路都不影响融合逻辑。

    Args:
        vector_hits: 向量路结果（按 score 降序）。
        bm25_hits: BM25 路结果（按 score 降序）。
        rrf_k: 融合常数，任务书锁定 60。

    Returns:
        按融合分降序的候选列表。
    """
    merged: dict[str, FusedCandidate] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        cid = str(hit.get("chunk_id", ""))
        if not cid:
            continue
        cand = merged.get(cid)
        if cand is None:
            cand = FusedCandidate(
                chunk_id=cid,
                content=str(hit.get("content", "")),
                title=str(hit.get("title", "") or ""),
                metadata=dict(hit.get("metadata", {}) or {}),
            )
            merged[cid] = cand
        cand.vector_rank = rank
        cand.sources.append("vector")
        cand.score += 1.0 / (rrf_k + rank)

    for rank, hit in enumerate(bm25_hits, start=1):
        cid = str(hit.get("chunk_id", ""))
        if not cid:
            continue
        cand = merged.get(cid)
        if cand is None:
            cand = FusedCandidate(
                chunk_id=cid,
                content=str(hit.get("content", "")),
                title=str(hit.get("title", "") or ""),
                metadata=dict(hit.get("metadata", {}) or {}),
            )
            merged[cid] = cand
        cand.bm25_rank = rank
        cand.sources.append("bm25")
        cand.score += 1.0 / (rrf_k + rank)

    out = list(merged.values())
    # 双路命中的排前（RRF 分天然更高），同分时按 chunk_id 稳定排序
    out.sort(key=lambda c: (-c.score, c.chunk_id))
    return out


# ----------------------------------------------------------------------
# 元数据过滤
# ----------------------------------------------------------------------


def filter_candidates(
    candidates: Sequence[FusedCandidate],
    filters: RagFilters,
) -> tuple[list[FusedCandidate], int]:
    """融合后元数据过滤（六步第 4 步）。

    在 Python 侧做而不再查一次向量库的原因：融合结果只有几十条，
    本地过滤是微秒级；再查一次库则要多一次网络往返，拖慢 P95。

    Args:
        candidates: 融合后的候选。
        filters: 过滤条件。

    Returns:
        `(过滤后候选, 被过滤掉的条数)`。
    """
    if filters.is_empty():
        return list(candidates), 0

    kept: list[FusedCandidate] = []
    dropped = 0
    for cand in candidates:
        if _matches_filters(cand.metadata, filters):
            kept.append(cand)
        else:
            dropped += 1
    return kept, dropped


def _matches_filters(metadata: dict[str, Any], filters: RagFilters) -> bool:
    """判断单条元数据是否满足过滤条件。"""
    if filters.category is not None:
        if str(metadata.get("category", "")) != filters.category:
            return False

    if filters.sku_ids:
        have = {str(s) for s in (metadata.get("sku_ids") or [])}
        if not (set(filters.sku_ids) & have):
            return False

    channels = [c for c in (filters.channel or []) if c != "all"]
    if channels:
        have_ch = {str(c) for c in (metadata.get("channel") or [])}
        # 元数据里带 "all" 的知识对所有渠道可见
        if not (set(channels) & have_ch) and "all" not in have_ch:
            return False

    if filters.knowledge_type is not None:
        if str(metadata.get("knowledge_type", "")) != filters.knowledge_type.value:
            return False

    if filters.authority_min is not None:
        try:
            if int(metadata.get("authority", 99)) > filters.authority_min:
                return False
        except (TypeError, ValueError):
            return False

    if filters.effective_at:
        start = str(metadata.get("effective_from") or "")
        end = str(metadata.get("effective_to") or "")
        # 只有起始日期（无失效日期）也算生效
        if start and start > filters.effective_at:
            return False
        if end and end < filters.effective_at:
            return False

    return True


# ----------------------------------------------------------------------
# 检索服务
# ----------------------------------------------------------------------


def _maybe_async(result: Any) -> Any:
    """同步/异步通用驱动（适配器端口全部按 `async def` 设计）。

    分三种情况：

    * 不是 awaitable → 原样返回（同步适配器）；
    * 当前线程**没有**运行中的事件循环 → `asyncio.run` 直接跑完；
    * 当前线程**已有**运行中的事件循环（async 端点、Celery 的 async 任务）
      → 不能 `asyncio.run`（会抛
      `RuntimeError: asyncio.run() cannot be called from a running event loop`），
      改在**独立线程**里跑一个新 loop。

    **为什么第三种情况必须处理**：`clear_cache()` 被
    `POST /internal/v1/kb/import`（async 端点）调用。早期实现在这种场景下
    抛 RuntimeError，又被 `clear_cache` 里的 `except Exception` 吞掉，
    结果是**缓存静默不清 + 协程泄漏**（只在 GC 时打一条 RuntimeWarning），
    表现为「知识改了，5 分钟内还是查到旧答案」，直接违反 BE-05 的
    生效延迟 DoD。这类吞异常的位置必须保证驱动本身不会失败。
    """
    if not inspect.isawaitable(result):
        return result

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的 loop：常规路径
        return asyncio.run(result)

    # 已有运行中的 loop：换到独立线程新开一个 loop。
    # 用列表承接结果是线程间传值的常规做法（不做 result 变量闭包，
    # 避免 `nonlocal` 在异常路径下漏赋值）。
    box: list[Any] = []
    err: list[BaseException] = []

    def _runner(coro: Any) -> None:
        try:
            box.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            err.append(exc)

    thread = threading.Thread(target=_runner, args=(result,), daemon=True)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return box[0] if box else None


class RagService:
    """RAG 检索服务（六步流程的唯一实现）。

    设计约定：
        * 依赖全部走端口协议（`get_vector_store()` 等），不 import 具体驱动。
        * 单路故障降级不抛异常（检索失败不该让整个对话挂掉），
          但**两路全挂时返回空结果 + NO_EVIDENCE**，
          绝不退化成「随便返回几条低分片段」。
        * 结果进缓存（TTL 120s < 300s 生效延迟要求）。
    """

    def __init__(
        self,
        *,
        cache: Any = None,
        vector_store: Any = None,
        sparse_index: Any = None,
        embedder: Any = None,
        reranker: Any = None,
    ) -> None:
        self._cache = cache
        self._vector_store = vector_store
        self._sparse_index = sparse_index
        self._embedder = embedder
        self._reranker = reranker

    # -- 依赖惰性获取（允许测试注入） --------------------------------

    @property
    def cache(self) -> Any:
        """缓存端口。"""
        if self._cache is None:
            from app.adapters.cache import get_cache

            self._cache = get_cache()
        return self._cache

    @property
    def vector_store(self) -> Any:
        """向量库端口。"""
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    @property
    def sparse_index(self) -> Any:
        """稀疏索引端口。"""
        if self._sparse_index is None:
            self._sparse_index = get_sparse_index()
        return self._sparse_index

    @property
    def embedder(self) -> Any:
        """嵌入器端口。"""
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    @property
    def reranker(self) -> Any:
        """重排端口。"""
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    # -- 主入口 ------------------------------------------------------

    def retrieve(self, payload: RagRetrieveRequest) -> RagRetrieveResponse:
        """执行六步检索（契约 `POST /internal/v1/rag/retrieve`）。

        Args:
            payload: 检索请求。

        Returns:
            检索响应，含 Top-N 命中、最高分与分流决策。
        """
        started = time.perf_counter()
        settings = get_settings()
        thresholds = RagThresholds.from_settings()

        cached = self._cache_get(payload)
        if cached is not None:
            # 命中缓存时必须是**本次**的耗时：直接返回缓存对象会把首次
            # 冷启动的 latency_ms 原样带出去，监控曲线上看起来缓存从未生效。
            # `model_copy` 而非原地改，避免污染缓存里的那份。
            return cached.model_copy(
                update={
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "cache_hit": True,
                }
            )

        degraded: list[str] = []

        # 第 1 步：查询改写
        rewritten = rewrite_query(payload.query, enabled=payload.use_rewrite)

        # 第 2 步：双路并行召回
        vector_hits, bm25_hits = self._dual_recall(
            rewritten,
            top_k_vector=payload.top_k_vector,
            top_k_bm25=payload.top_k_bm25,
            filter_expr=payload.filters.to_milvus_expr(),
            degraded=degraded,
        )

        # 第 3 步：RRF 融合
        fused = fuse_reciprocal(
            vector_hits, bm25_hits, rrf_k=payload.rrf_k
        )

        # 第 4 步：元数据过滤
        filtered, dropped = filter_candidates(fused, payload.filters)

        # 第 5 步：Cross-Encoder 重排 → Top-N
        ranked = self._rerank(
            rewritten,
            filtered,
            top_n=payload.top_n_rerank,
            enabled=payload.use_rerank,
            degraded=degraded,
        )

        # 第 6 步：阈值分流
        top_score = float(ranked[0]["score"]) if ranked else 0.0
        decision = decide(top_score, thresholds)

        response = RagRetrieveResponse(
            hits=[self._to_hit(c) for c in ranked],
            top_score=round(top_score, 6),
            decision=decision,
            thresholds=thresholds,
            rewritten_query=rewritten,
            candidates_count=len(filtered),
            vector_hits=len(vector_hits),
            bm25_hits=len(bm25_hits),
            degraded=degraded,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

        self._cache_put(payload, response)
        self._log(payload, response, dropped=dropped, rewritten=rewritten)
        return response

    def answer_context(
        self, payload: RagRetrieveRequest, *, top_n: int | None = None
    ) -> tuple[RagRetrieveResponse, list[RagHit]]:
        """检索并返回「可用于生成」的上下文。

        **BE-05 DoD 的代码层保障**：`NO_EVIDENCE` 时返回**空**上下文。
        调用方（BE-16 生成阶段）拿不到任何片段，从源头杜绝
        「无依据硬答」。`WEAK` 时也只返回 Top-3（BE-16 成本控制要求），
        且由三道闸的闸 1 判定是否敢用。

        Args:
            payload: 检索请求。
            top_n: 覆盖请求的 `top_n_rerank`（BE-16 传 `llm_context_top_n`）。

        Returns:
            `(完整检索响应, 可用于生成的命中列表)`。
        """
        if top_n is not None:
            payload = payload.model_copy(
                update={"top_n_rerank": max(1, min(top_n, payload.top_n_rerank or top_n))}
            )
        response = self.retrieve(payload)
        return response, self.answer_context_from(response)

    def answer_context_from(self, response: RagRetrieveResponse) -> list[RagHit]:
        """从**已有**检索响应里挑出可用于生成的上下文（不重复检索）。

        抽出来的原因：编排层（BE-04）会先 `retrieve` 拿到 decision 做动作
        决策，再要上下文交给生成——如果调 `answer_context` 会再跑一遍
        六步流程（多一次向量+BM25+重排，P95 直接翻倍）。
        这里保证「同一份响应 → 同一个挑选口径」，避免两处阈值判断分叉。

        Args:
            response: 已完成的检索响应。

        Returns:
            可用于生成的命中列表；`NO_EVIDENCE` 时为空。
        """
        if response.decision is RagDecision.NO_EVIDENCE:
            return []
        if response.decision is RagDecision.WEAK:
            return response.hits[: get_settings().llm_context_top_n]
        return list(response.hits)

    # -- 内部步骤 ----------------------------------------------------

    def _dual_recall(
        self,
        query: str,
        *,
        top_k_vector: int,
        top_k_bm25: int,
        filter_expr: str | None,
        degraded: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """双路召回（并行执行，单路故障降级）。

        Returns:
            `(向量路结果, BM25 路结果)`。故障方返回空列表并在
            `degraded` 里记标识。
        """
        import concurrent.futures

        def _run_vector() -> list[dict[str, Any]]:
            vector = _maybe_async(self.embedder.embed_query(query))
            return _maybe_async(
                self.vector_store.search(
                    vector, top_k=top_k_vector, filter_expr=filter_expr
                )
            )

        def _run_bm25() -> list[dict[str, Any]]:
            return _maybe_async(
                self.sparse_index.search(
                    query, top_k=top_k_bm25, filter_expr=filter_expr
                )
            )

        vector_hits: list[dict[str, Any]] = []
        bm25_hits: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_vec = pool.submit(_run_vector)
            fut_bm25 = pool.submit(_run_bm25)
            try:
                vector_hits = list(fut_vec.result(timeout=5.0))
            except Exception as exc:  # noqa: BLE001
                degraded.append("VECTOR_UNAVAILABLE")
                logger.warning(
                    "向量路召回失败，降级为单路：%s(%s)",
                    type(exc).__name__,
                    mask_text(str(exc))[:120],
                )
            try:
                bm25_hits = list(fut_bm25.result(timeout=5.0))
            except Exception as exc:  # noqa: BLE001
                degraded.append("SPARSE_UNAVAILABLE")
                logger.warning(
                    "BM25 路召回失败，降级为单路：%s(%s)",
                    type(exc).__name__,
                    mask_text(str(exc))[:120],
                )

        if not vector_hits and not bm25_hits:
            # 两路全空：区分「库里没内容」与「两路都挂了」
            if degraded:
                degraded.append("RETRIEVAL_UNAVAILABLE")
                logger.error("双路召回全失败，返回空结果（NO_EVIDENCE）")

        return vector_hits, bm25_hits

    def _rerank(
        self,
        query: str,
        candidates: Sequence[FusedCandidate],
        *,
        top_n: int,
        enabled: bool,
        degraded: list[str],
    ) -> list[dict[str, Any]]:
        """重排 → Top-N。失败时降级为 RRF 融合序。

        Returns:
            重排后的候选字典列表（含 `score`）。
        """
        if not candidates:
            return []
        if not enabled:
            return [c.to_dict() for c in candidates[:top_n]]

        try:
            ranked = _maybe_async(
                self.reranker.rerank(
                    query, [c.to_dict() for c in candidates], top_n=top_n
                )
            )
            return list(ranked)
        except Exception as exc:  # noqa: BLE001
            degraded.append("RERANK_UNAVAILABLE")
            logger.warning(
                "重排失败，降级为 RRF 融合序：%s(%s)",
                type(exc).__name__,
                mask_text(str(exc))[:120],
            )
            return [c.to_dict() for c in candidates[:top_n]]

    @staticmethod
    def _to_hit(candidate: dict[str, Any]) -> RagHit:
        """融合/重排候选 → 契约 `RagHit`。"""
        score = float(candidate.get("score", 0.0) or 0.0)
        vector_rank = candidate.get("vector_rank")
        bm25_rank = candidate.get("bm25_rank")
        sources = list(candidate.get("sources") or [])
        if not sources:
            # 重排后的候选可能丢了 sources 字段，用 rank 反推
            if vector_rank is not None:
                sources.append("vector")
            if bm25_rank is not None:
                sources.append("bm25")
        return RagHit(
            chunk_id=str(candidate.get("chunk_id", "")),
            content=str(candidate.get("content", "")),
            score=max(0.0, round(score, 6)),
            metadata=dict(candidate.get("metadata", {}) or {}),
            sources=sources,
            vector_rank=int(vector_rank) if vector_rank is not None else None,
            bm25_rank=int(bm25_rank) if bm25_rank is not None else None,
        )

    # -- 缓存 --------------------------------------------------------

    @staticmethod
    def _cache_key(payload: RagRetrieveRequest) -> str:
        """缓存 key（含全部影响结果的参数）。"""
        import hashlib
        import json

        raw = json.dumps(
            {
                "q": normalize_text(payload.query),
                "kv": payload.top_k_vector,
                "kb": payload.top_k_bm25,
                "kn": payload.top_n_rerank,
                "k": payload.rrf_k,
                "rw": payload.use_rewrite,
                "rr": payload.use_rerank,
                "f": payload.filters.model_dump(mode="json"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
        return f"{_CACHE_PREFIX}:{digest}"

    def _cache_get(self, payload: RagRetrieveRequest) -> RagRetrieveResponse | None:
        """读缓存；任何异常都视为未命中（缓存故障不影响检索）。

        用 `self.cache`（property，惰性初始化）而非 `self._cache`：
        未经构造注入时 `_cache` 恒为 None，直接判它会**永远跳过缓存**。
        """
        try:
            raw = _maybe_async(self.cache.get(self._cache_key(payload)))
            if not raw:
                return None
            import json

            return RagRetrieveResponse.model_validate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            logger.debug("RAG 缓存读取失败：%s", type(exc).__name__)
            return None

    def _cache_put(self, payload: RagRetrieveRequest, response: RagRetrieveResponse) -> None:
        """写缓存；失败只记日志。

        两个必须记住的坑：
            1. 端口是 `async def`，必须用 `_maybe_async` 驱动，否则
               `set()` 返回的协程从未 await —— 不报错、缓存永远写不进去；
            2. 必须走 `self.cache` property，否则未注入时恒为 None 直接跳过。
        """
        try:
            import json

            _maybe_async(
                self.cache.set(
                    self._cache_key(payload),
                    json.dumps(response.model_dump(mode="json"), ensure_ascii=False),
                    ttl=CACHE_TTL_SECONDS,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("RAG 缓存写入失败：%s", type(exc).__name__)

    def clear_cache(self) -> int:
        """清空检索缓存（知识更新后调用）。

        Returns:
            删除的键数（无法统计时返回 0）。
        """
        try:
            return int(_maybe_async(self.cache.delete_prefix(f"{_CACHE_PREFIX}:")) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("RAG 缓存清理失败：%s", type(exc).__name__)
            return 0

    # -- 日志 --------------------------------------------------------

    @staticmethod
    def _log(
        payload: RagRetrieveRequest,
        response: RagRetrieveResponse,
        *,
        dropped: int,
        rewritten: str,
    ) -> None:
        """结构化检索日志（trace_id 由中间件注入）。"""
        logger.info(
            "rag_retrieve decision=%s top_score=%.4f candidates=%d "
            "vector=%d bm25=%d dropped=%d degraded=%s latency=%dms "
            "rewritten=%r",
            response.decision.value,
            response.top_score,
            response.candidates_count,
            response.vector_hits,
            response.bm25_hits,
            dropped,
            response.degraded,
            response.latency_ms,
            mask_text(rewritten)[:60],
        )


# ----------------------------------------------------------------------
# 单例
# ----------------------------------------------------------------------

_RAG_SERVICE: RagService | None = None


def get_rag_service() -> RagService:
    """获取 RAG 检索服务单例。"""
    global _RAG_SERVICE  # noqa: PLW0603
    if _RAG_SERVICE is None:
        _RAG_SERVICE = RagService()
    return _RAG_SERVICE


def set_rag_service(service: RagService | None) -> None:
    """替换 RAG 检索服务（测试用）。"""
    global _RAG_SERVICE  # noqa: PLW0603
    _RAG_SERVICE = service


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
