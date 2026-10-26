"""稀疏检索适配器（Elasticsearch 8 BM25 + 本地 jieba BM25 等价实现）。

RAG 六步流程的第 2 路召回（BM25）。**为什么必须有这一路**：向量检索对
「精确专有名词」不敏感（如 SKU 编码 `WK-BEAR-20`、政策条款号），
BM25 能补上字面精确匹配，两路 RRF 融合后 Recall@5 才可能达到 95%。

| 实现 | 用途 |
| --- | --- |
| `EsSparseIndex` | 生产：Elasticsearch 8，中文分词用 `ik_max_word`（或 `smartcn`） |
| `Bm25SparseIndex` | 本地 / 单测：jieba 分词 + 纯 Python BM25（Okapi），磁盘持久化 |

本地实现**必须与 ES 语义对齐**：`upsert` 覆盖、`search` 返回按 score 降序、
`filter_expr` 支持 RAG 用到的同一子集（与向量库共用 `_match_filter` 语义）。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.adapters.embedding import tokenize
from app.adapters.vector_store import assert_no_forbidden_fields
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Okapi BM25 参数（ES 默认值，保持一致）
BM25_K1 = 1.2
BM25_B = 0.75


class Bm25SparseIndex:
    """jieba + Okapi BM25 本地稀疏索引。

    持久化格式：`bm25.json`，包含 docs（chunk_id → {content, metadata, tf, len}）
    与统计量（avgdl、df）。

    实现要点：
        * 词典在内存中重建（条目数在万级，重建成本可接受）。
        * BM25 打分公式与 ES/Lucene 一致（`k1=1.2, b=0.75`），
          因此本地调参与线上表现不会因打分口径不同而漂移。
        * 过滤表达式复用向量库的 `_match_filter` 语义，避免两路过滤行为不一致。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._file = self._path / "bm25.json"
        self._docs: dict[str, dict[str, Any]] = {}
        self._df: Counter[str] = Counter()
        self._avgdl: float = 0.0
        self._loaded = False

    @property
    def name(self) -> str:
        """实现名。"""
        return "jieba-bm25-local"

    # -- 持久化 -------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """惰性加载索引。"""
        if self._loaded:
            return
        self._path.mkdir(parents=True, exist_ok=True)
        if self._file.exists():
            try:
                payload = json.loads(self._file.read_text(encoding="utf-8"))
                self._docs = payload.get("docs", {})
                self._recompute_stats()
            except Exception as exc:  # noqa: BLE001 - 索引损坏时重建
                logger.warning("BM25 索引加载失败（%s），已重建", type(exc).__name__)
                self._docs = {}
                self._df = Counter()
                self._avgdl = 0.0
        self._loaded = True

    def _recompute_stats(self) -> None:
        """重建 df 与 avgdl。"""
        self._df = Counter()
        total_len = 0
        for doc in self._docs.values():
            total_len += int(doc.get("len", 0))
            for term in doc.get("tf", {}):
                self._df[term] += 1
        self._avgdl = (total_len / len(self._docs)) if self._docs else 0.0

    def _persist(self) -> None:
        """落盘。"""
        self._path.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(
                {"docs": self._docs, "avgdl": self._avgdl, "n": len(self._docs)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # -- SparseIndexPort ----------------------------------------------------
    async def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """批量写入 / 覆盖。"""
        self._ensure_loaded()
        for chunk in chunks:
            metadata = dict(chunk.get("metadata", {}) or {})
            assert_no_forbidden_fields(metadata)
            chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
            if not chunk_id:
                raise ValueError("chunk 必须携带 chunk_id")
            content = str(chunk.get("content", ""))
            # 标题与关键词一起进索引：标题命中应显著提升排序
            title = str(chunk.get("title") or metadata.get("title") or "")
            keywords = " ".join(str(k) for k in (metadata.get("keywords") or []))
            terms = tokenize(f"{title} {keywords} {content}")
            self._docs[chunk_id] = {
                "chunk_id": chunk_id,
                "content": content,
                "title": title,
                "metadata": metadata,
                "tf": dict(Counter(terms)),
                "len": len(terms),
            }
        self._recompute_stats()
        self._persist()
        return len(chunks)

    def _score(self, doc: dict[str, Any], query_terms: Counter[str]) -> float:
        """计算单个文档的 BM25 分数。"""
        tf_map: dict[str, int] = doc.get("tf", {})
        doc_len = int(doc.get("len", 0)) or 1
        total_docs = len(self._docs) or 1
        score = 0.0
        for term, q_weight in query_terms.items():
            freq = tf_map.get(term, 0)
            if not freq:
                continue
            df = self._df.get(term, 0) or 0.5
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            denom = freq + BM25_K1 * (
                1 - BM25_B + BM25_B * doc_len / (self._avgdl or 1.0)
            )
            score += q_weight * idf * (freq * (BM25_K1 + 1)) / denom
        return score

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索，返回按 score 降序的结果。"""
        from app.adapters.vector_store import NumpyVectorStore

        self._ensure_loaded()
        if not self._docs:
            return []
        query_terms = Counter(tokenize(query))
        if not query_terms:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in self._docs.values():
            if not NumpyVectorStore._match_filter(
                doc.get("metadata", {}), filter_expr
            ):
                continue
            score = self._score(doc, query_terms)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda item: -item[0])
        return [
            {
                "chunk_id": doc["chunk_id"],
                "content": doc.get("content", ""),
                "score": float(score),
                "metadata": doc.get("metadata", {}),
            }
            for score, doc in scored[:top_k]
        ]

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        self._ensure_loaded()
        removed = 0
        for chunk_id in chunk_ids:
            if str(chunk_id) in self._docs:
                del self._docs[str(chunk_id)]
                removed += 1
        if removed:
            self._recompute_stats()
            self._persist()
        return removed

    async def count(self) -> int:
        """索引内 chunk 总数。"""
        self._ensure_loaded()
        return len(self._docs)

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        try:
            self._ensure_loaded()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def all_records(self) -> list[dict[str, Any]]:
        """返回全部记录（重建索引/统计用）。"""
        self._ensure_loaded()
        return [dict(d) for d in self._docs.values()]


class EsSparseIndex:
    """Elasticsearch 8 BM25 实现。

    索引 mapping（与本地实现字段一致）：

        * `chunk_id` keyword（主键）
        * `content` text（`ik_max_word` 分词）
        * `title` text（权重 ×3，标题命中优先）
        * `category` / `knowledge_type` keyword
        * `sku_ids` / `channel` keyword 数组
        * `authority` integer、`effective_from` keyword

    ⚠️ **mapping 中刻意不存在 stock / price 字段**（HC-3）。
    """

    #: 标题字段的权重（ES boost）
    TITLE_BOOST = 3.0

    def __init__(
        self,
        *,
        url: str,
        index: str,
        user: str = "",
        password: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._index = index
        self._user = user
        self._password = password
        self._timeout = timeout
        #: **按事件循环缓存客户端**（同 `RedisCache`）：`AsyncElasticsearch`
        #: 底层 aiohttp 连接池绑在创建它的 loop 上，跨 loop 复用会抛
        #: `RuntimeError: Event loop is closed`。
        self._clients: dict[int, Any] = {}

    @property
    def name(self) -> str:
        """实现名。"""
        return "elasticsearch-8"

    def _get_client(self) -> Any:
        """惰性建立连接（每个事件循环一个客户端）。"""
        import asyncio

        loop = asyncio.get_running_loop()
        key = id(loop)
        client = self._clients.get(key)
        if client is None:
            from elasticsearch import AsyncElasticsearch

            kwargs: dict[str, Any] = {
                "hosts": [self._url],
                "request_timeout": self._timeout,
            }
            if self._user:
                kwargs["basic_auth"] = (self._user, self._password)
            client = AsyncElasticsearch(**kwargs)
            self._clients[key] = client
        return client

    async def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """批量写入 / 覆盖（`_bulk` 带 `index` 动作）。"""
        client = self._get_client()
        operations: list[dict[str, Any]] = []
        for chunk in chunks:
            metadata = dict(chunk.get("metadata", {}) or {})
            assert_no_forbidden_fields(metadata)
            chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
            if not chunk_id:
                raise ValueError("chunk 必须携带 chunk_id")
            operations.append(
                {"index": {"_index": self._index, "_id": chunk_id}}
            )
            operations.append(
                {
                    "chunk_id": chunk_id,
                    "content": str(chunk.get("content", "")),
                    "title": str(chunk.get("title") or metadata.get("title") or ""),
                    "category": str(metadata.get("category", "")),
                    "knowledge_type": str(metadata.get("knowledge_type", "DOC")),
                    "sku_ids": list(metadata.get("sku_ids", [])),
                    "channel": list(metadata.get("channel", ["all"])),
                    "authority": int(metadata.get("authority", 2)),
                    "effective_from": str(metadata.get("effective_from", "") or ""),
                }
            )
        if not operations:
            return 0
        await client.bulk(operations=operations, refresh=True)
        return len(chunks)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索。`filter_expr` 由 Milvus 语法翻译为 ES 过滤子句。"""
        client = self._get_client()
        body: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": [
                                    f"title^{self.TITLE_BOOST}",
                                    "content",
                                ],
                            }
                        }
                    ],
                    "filter": self._translate_filter(filter_expr),
                }
            },
            "size": top_k,
        }
        resp = await client.search(index=self._index, body=body)
        hits = resp.get("hits", {}).get("hits", [])
        return [
            {
                "chunk_id": hit["_source"].get("chunk_id", hit["_id"]),
                "content": hit["_source"].get("content", ""),
                "score": float(hit.get("_score") or 0.0),
                "metadata": {
                    k: v
                    for k, v in hit["_source"].items()
                    if k not in {"chunk_id", "content", "title"}
                },
            }
            for hit in hits
        ]

    @staticmethod
    def _translate_filter(filter_expr: str | None) -> list[dict[str, Any]]:
        """把 Milvus 布尔表达式翻译为 ES filter 子句列表。"""
        if not filter_expr:
            return []
        clauses: list[dict[str, Any]] = []
        for clause in filter_expr.split(" and "):
            clause = clause.strip()
            if not clause:
                continue
            if clause.startswith("ARRAY_CONTAINS_ANY("):
                inner = clause[len("ARRAY_CONTAINS_ANY(") :].rstrip(")")
                field, _, rest = inner.partition(",")
                values = [
                    part.strip().strip('"').strip("'")
                    for part in rest.strip().strip("[]").split(",")
                    if part.strip()
                ]
                clauses.append({"terms": {field.strip(): values}})
            elif ">=" in clause:
                field, _, raw = clause.partition(">=")
                clauses.append(
                    {"range": {field.strip(): {"gte": float(raw.strip())}}}
                )
            elif "<=" in clause:
                field, _, raw = clause.partition("<=")
                clauses.append(
                    {"range": {field.strip(): {"lte": raw.strip().strip('"')}}}
                )
            elif "==" in clause:
                field, _, raw = clause.partition("==")
                clauses.append(
                    {"term": {field.strip(): raw.strip().strip('"').strip("'")}}
                )
        return clauses

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        client = self._get_client()
        operations: list[dict[str, Any]] = []
        for chunk_id in chunk_ids:
            operations.append(
                {"delete": {"_index": self._index, "_id": str(chunk_id)}}
            )
        if operations:
            await client.bulk(operations=operations, refresh=True)
        return len(chunk_ids)

    async def count(self) -> int:
        """索引内文档数。"""
        client = self._get_client()
        resp = await client.count(index=self._index)
        return int(resp.get("count", 0))

    async def health(self) -> bool:
        """连接探测。"""
        try:
            client = self._get_client()
            return bool(await client.ping())
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("Elasticsearch 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_INDEX: Any = None


def get_sparse_index() -> Any:
    """获取稀疏索引实现（单例）。

    策略：`WK_ENV=local` 用 jieba BM25；其余环境用 Elasticsearch 8。
    `WK_SPARSE_INDEX=local|es` 可强制覆盖。
    """
    global _INDEX  # noqa: PLW0603
    if _INDEX is None:
        import os

        settings = get_settings()
        forced = os.getenv("WK_SPARSE_INDEX", "").strip().lower()
        use_local = forced == "local" or (not forced and settings.env == "local")
        if use_local:
            logger.info("稀疏索引实现：Bm25SparseIndex（jieba + Okapi BM25）")
            _INDEX = Bm25SparseIndex(settings.local_index_dir / "bm25")
        else:
            logger.info("稀疏索引实现：EsSparseIndex")
            _INDEX = EsSparseIndex(
                url=settings.es_url,
                index=settings.es_index,
                user=settings.es_user,
                password=settings.es_password.get_secret_value(),
            )
    return _INDEX


def set_sparse_index(index: Any) -> None:
    """替换稀疏索引实现（测试用）。"""
    global _INDEX  # noqa: PLW0603
    _INDEX = index


__all__ = [
    "BM25_B",
    "BM25_K1",
    "Bm25SparseIndex",
    "EsSparseIndex",
    "get_sparse_index",
    "set_sparse_index",
]
