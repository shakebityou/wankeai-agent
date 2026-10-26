"""向量库适配器（Milvus 2.4 Standalone + numpy 等价实现）。

| 实现 | 用途 |
| --- | --- |
| `MilvusVectorStore` | 生产：Milvus 2.4 Standalone，1024 维，支持元数据过滤 |
| `NumpyVectorStore` | 本地 / 单测：numpy 余弦相似度 + 磁盘持久化 |

**硬性约束（BE-05 DoD，有自动化校验用例）**：集合内**不得存在库存 / 价格字段**。
本模块在 `upsert` 入口做**主动拦截**——命中禁用字段名直接抛错，
而不是静默丢弃，因为静默丢弃会让「过期价格进向量库」这类缺陷溜到线上。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 禁止进入向量库的字段名（HC-3 硬约束：会变的数字永远走接口）
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "stock",
        "stock_status",
        "stock_qty",
        "available_qty",
        "inventory",
        "inventory_qty",
        "qty",
        "quantity",
        "price",
        "current_price",
        "list_price",
        "promo_price",
        "discount",
        "amount",
        "logistics_no",
        "tracking_no",
        "express_no",
    }
)

#: 允许出现在向量库元数据中的字段（白名单，防止误塞）
ALLOWED_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "chunk_id",
        "title",
        "category",
        "sub_category",
        "sku_ids",
        "knowledge_type",
        "source",
        "authority",
        "channel",
        "effective_from",
        "effective_to",
        "brand",
        "keywords",
        "version",
        "review_status",
        "enabled",
        "updated_by",
        "created_at",
        "updated_at",
    }
)


class ForbiddenFieldError(ValueError):
    """向量库元数据中出现禁用字段（库存/价格等会变的数字）。"""


def assert_no_forbidden_fields(metadata: dict[str, Any]) -> None:
    """校验元数据不含禁用字段。

    Args:
        metadata: 待入库元数据。

    Raises:
        ForbiddenFieldError: 命中禁用字段。
    """
    hits = sorted(k for k in metadata if k.lower() in FORBIDDEN_FIELDS)
    if hits:
        raise ForbiddenFieldError(
            "向量库元数据不得包含会变的数字字段 "
            f"{hits}（HC-3 硬约束：库存/价格/运单号必须走接口实时查询）"
        )


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """裁剪元数据：只保留白名单字段，并做类型规整。

    Milvus 的标量字段需要固定 schema，因此这里统一为可入库类型：
    列表 → `list[str]`，日期 → `str`（`yyyy-MM-dd`），布尔 → `bool`。
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in FORBIDDEN_FIELDS:
            # 已在 assert_no_forbidden_fields 拦截；此处兜底再丢一次
            continue
        if key not in ALLOWED_METADATA_FIELDS:
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out[key] = [str(v) for v in value]
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value)
    return out


class NumpyVectorStore:
    """numpy 余弦相似度向量库（本地等价实现）。

    持久化格式：索引目录下 `vectors.npy`（float32 矩阵）+ `records.jsonl`
    （每行一条元数据 JSON，顺序与矩阵行一致）。

    语义与 Milvus 一致：`upsert` 覆盖、`search` 按余弦降序、`delete` 按 chunk_id。
    元数据过滤表达式只实现 RAG 实际用到的子集（`category ==`、
    `ARRAY_CONTAINS_ANY(...)`、`knowledge_type ==`、`authority >=`、
    `effective_from <=`），保证本地与生产的过滤行为对齐。
    """

    def __init__(self, path: Path, *, dim: int = 1024) -> None:
        self._path = Path(path)
        self._dim = dim
        self._vectors_path = self._path / "vectors.npy"
        self._records_path = self._path / "records.jsonl"
        self._vectors: Any = None  # numpy.ndarray | None
        self._records: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}
        self._loaded = False

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    @property
    def name(self) -> str:
        """实现名。"""
        return "numpy-local"

    # -- 持久化 -------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """惰性加载索引文件。"""
        if self._loaded:
            return
        import numpy as np

        self._path.mkdir(parents=True, exist_ok=True)
        if self._vectors_path.exists() and self._records_path.exists():
            try:
                self._vectors = np.load(self._vectors_path)
                with self._records_path.open("r", encoding="utf-8") as fh:
                    self._records = [json.loads(line) for line in fh if line.strip()]
                if len(self._records) != len(self._vectors):
                    logger.warning(
                        "向量索引与记录数不一致（%d vs %d），已重建索引",
                        len(self._vectors),
                        len(self._records),
                    )
                    self._vectors = None
                    self._records = []
            except Exception as exc:  # noqa: BLE001 - 索引损坏时重建
                logger.warning("向量索引加载失败（%s），已重建", type(exc).__name__)
                self._vectors = None
                self._records = []
        if self._vectors is None:
            self._vectors = np.zeros((0, self._dim), dtype="float32")
        self._index = {
            str(r.get("chunk_id")): i for i, r in enumerate(self._records)
        }
        self._loaded = True

    def _persist(self) -> None:
        """落盘。"""
        import numpy as np

        self._path.mkdir(parents=True, exist_ok=True)
        np.save(self._vectors_path, self._vectors)
        with self._records_path.open("w", encoding="utf-8") as fh:
            for record in self._records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # -- VectorStorePort ----------------------------------------------------
    async def upsert(
        self, chunks: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]
    ) -> int:
        """批量写入 / 覆盖。"""
        import numpy as np

        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量必须一致")
        self._ensure_loaded()
        written = 0
        for chunk, vector in zip(chunks, vectors, strict=True):
            if len(vector) != self._dim:
                raise ValueError(
                    f"向量维度 {len(vector)} 与索引维度 {self._dim} 不一致"
                )
            # 必须先校验原始元数据再裁剪：_sanitize_metadata 会按白名单丢弃
            # 非白名单键（含 price/stock 等），若先裁剪则校验永远看不到禁用字段，
            # 会退化成静默丢弃——而 BE-05 DoD 要求的是「显式报错」。
            raw_metadata = dict(chunk.get("metadata", {}) or {})
            assert_no_forbidden_fields(raw_metadata)
            metadata = _sanitize_metadata(raw_metadata)
            chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
            if not chunk_id:
                raise ValueError("chunk 必须携带 chunk_id")
            record = {
                "chunk_id": chunk_id,
                "content": str(chunk.get("content", "")),
                "metadata": metadata,
            }
            row = np.asarray(vector, dtype="float32")
            existing = self._index.get(chunk_id)
            if existing is None:
                self._vectors = np.vstack([self._vectors, row.reshape(1, -1)])
                self._records.append(record)
                self._index[chunk_id] = len(self._records) - 1
            else:
                self._vectors[existing] = row
                self._records[existing] = record
            written += 1
        self._persist()
        return written

    @staticmethod
    def _match_filter(metadata: dict[str, Any], filter_expr: str | None) -> bool:
        """在本地解释 Milvus 过滤表达式（支持 RAG 用到的子集）。"""
        if not filter_expr:
            return True
        expr = filter_expr.strip()
        ok = True
        for clause in expr.split(" and "):
            clause = clause.strip()
            if not clause:
                continue
            if clause.startswith("ARRAY_CONTAINS_ANY("):
                inner = clause[len("ARRAY_CONTAINS_ANY(") :].rstrip(")")
                field, _, rest = inner.partition(",")
                field = field.strip()
                wanted = {
                    part.strip().strip('"').strip("'")
                    for part in rest.strip().strip("[]").split(",")
                    if part.strip()
                }
                have = {str(v) for v in (metadata.get(field) or [])}
                if not (wanted & have):
                    ok = False
            elif ">=" in clause:
                field, _, raw = clause.partition(">=")
                field, raw = field.strip(), raw.strip().strip('"')
                try:
                    if float(metadata.get(field, 0)) < float(raw):
                        ok = False
                except (TypeError, ValueError):
                    ok = False
            elif "<=" in clause:
                field, _, raw = clause.partition("<=")
                field, raw = field.strip(), raw.strip().strip('"')
                value = metadata.get(field)
                if value is None or str(value) > raw:
                    ok = False
            elif "==" in clause:
                field, _, raw = clause.partition("==")
                field, raw = field.strip(), raw.strip().strip('"').strip("'")
                value = metadata.get(field)
                if isinstance(value, (list, tuple)):
                    if raw not in {str(v) for v in value}:
                        ok = False
                elif str(value) != raw:
                    ok = False
            if not ok:
                return False
        return ok

    async def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """余弦相似度检索，返回按 score 降序的结果。"""
        import numpy as np

        self._ensure_loaded()
        if len(self._vectors) == 0:
            return []
        query = np.asarray(vector, dtype="float32")
        norm = float(np.linalg.norm(query))
        if norm == 0:
            return []
        query = query / norm
        matrix_norm = np.linalg.norm(self._vectors, axis=1)
        matrix_norm[matrix_norm == 0] = 1.0
        scores = (self._vectors / matrix_norm[:, None]) @ query

        results: list[dict[str, Any]] = []
        order = np.argsort(-scores)
        for idx in order:
            record = self._records[int(idx)]
            if not self._match_filter(record.get("metadata", {}), filter_expr):
                continue
            results.append(
                {
                    "chunk_id": record["chunk_id"],
                    "content": record.get("content", ""),
                    "score": float(scores[int(idx)]),
                    "metadata": record.get("metadata", {}),
                }
            )
            if len(results) >= top_k:
                break
        return results

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        import numpy as np

        self._ensure_loaded()
        targets = {str(c) for c in chunk_ids}
        keep_idx = [
            i
            for i, record in enumerate(self._records)
            if record["chunk_id"] not in targets
        ]
        removed = len(self._records) - len(keep_idx)
        if removed:
            self._records = [self._records[i] for i in keep_idx]
            if keep_idx:
                self._vectors = self._vectors[keep_idx]
            else:
                self._vectors = np.zeros((0, self._dim), dtype="float32")
            self._index = {
                r["chunk_id"]: i for i, r in enumerate(self._records)
            }
            self._persist()
        return removed

    async def count(self) -> int:
        """集合内 chunk 总数。"""
        self._ensure_loaded()
        return len(self._records)

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        try:
            self._ensure_loaded()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def get(self, chunk_id: str) -> dict[str, Any] | None:
        """按 ID 取单条（本地扩展方法，生产 Milvus 亦支持 query）。"""
        self._ensure_loaded()
        idx = self._index.get(chunk_id)
        return dict(self._records[idx]) if idx is not None else None

    async def all_records(self) -> list[dict[str, Any]]:
        """返回全部记录（重建稀疏索引用）。"""
        self._ensure_loaded()
        return [dict(r) for r in self._records]


class MilvusVectorStore:
    """Milvus 2.4 Standalone 实现。

    集合 schema（与本地实现字段一致，便于切换）：

        | 字段 | 类型 | 说明 |
        | --- | --- | --- |
        | chunk_id | VARCHAR(64) 主键 | 知识块 ID |
        | vector | FLOAT_VECTOR(1024) | 向量 |
        | content | VARCHAR(4096) | 正文 |
        | category | VARCHAR(64) | 分类 |
        | sku_ids | ARRAY(VARCHAR) | 关联 SKU |
        | knowledge_type | VARCHAR(32) | 知识类型 |
        | authority | INT8 | 权威等级 |
        | channel | ARRAY(VARCHAR) | 适用渠道 |
        | effective_from | VARCHAR(16) | 生效日期 |
        | version | INT16 | 版本 |
        | enabled | BOOL | 是否启用 |

    ⚠️ **schema 中刻意不存在 stock / price 字段**——从结构上杜绝库存价格进向量库。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        collection: str,
        dim: int = 1024,
        user: str = "",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._collection = collection
        self._dim = dim
        self._user = user
        self._password = password
        self._client: Any | None = None

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    @property
    def name(self) -> str:
        """实现名。"""
        return "milvus-2.4"

    def _get_client(self) -> Any:
        """惰性建立连接，并确保集合已存在。"""
        if self._client is None:
            from pymilvus import MilvusClient

            uri = f"http://{self._host}:{self._port}"
            kwargs: dict[str, Any] = {"uri": uri}
            if self._user:
                kwargs["user"] = self._user
                kwargs["password"] = self._password
            client = MilvusClient(**kwargs)
            self._ensure_collection(client)
            self._client = client
        return self._client

    def _ensure_collection(self, client: Any) -> None:
        """集合不存在则按上面的 schema 建好。

        早先版本没有这一步：单测与本地开发都用 `NumpyVectorStore`，Milvus 分支
        在真实部署里第一次 upsert 才会连上去，而全新 Milvus 上集合并不存在，
        于是 336 条知识全部报
        `DescribeCollectionException: can't find collection[...]`（见交接文档踩坑）。
        建集合必须是幂等的，所以先 `has_collection` 再建。
        """
        if client.has_collection(collection_name=self._collection):
            return

        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field("content", DataType.VARCHAR, max_length=4096)
        schema.add_field("category", DataType.VARCHAR, max_length=64)
        schema.add_field(
            "sku_ids",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=64,
            max_length=64,
        )
        schema.add_field("knowledge_type", DataType.VARCHAR, max_length=32)
        schema.add_field("authority", DataType.INT8)
        schema.add_field(
            "channel",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=16,
            max_length=32,
        )
        schema.add_field("effective_from", DataType.VARCHAR, max_length=16)
        schema.add_field("version", DataType.INT16)
        schema.add_field("enabled", DataType.BOOL)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )
        logger.info("Milvus 集合已创建：%s（dim=%s）", self._collection, self._dim)

    async def upsert(
        self, chunks: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]
    ) -> int:
        """批量写入 / 覆盖。"""
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量必须一致")
        rows: list[dict[str, Any]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            # 必须先校验原始元数据再裁剪：_sanitize_metadata 会按白名单丢弃
            # 非白名单键（含 price/stock 等），若先裁剪则校验永远看不到禁用字段，
            # 会退化成静默丢弃——而 BE-05 DoD 要求的是「显式报错」。
            raw_metadata = dict(chunk.get("metadata", {}) or {})
            assert_no_forbidden_fields(raw_metadata)
            metadata = _sanitize_metadata(raw_metadata)
            chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
            if not chunk_id:
                raise ValueError("chunk 必须携带 chunk_id")
            rows.append(
                {
                    "chunk_id": chunk_id,
                    "vector": list(vector),
                    "content": str(chunk.get("content", ""))[:4096],
                    "category": str(metadata.get("category", "")),
                    "sku_ids": list(metadata.get("sku_ids", [])),
                    "knowledge_type": str(metadata.get("knowledge_type", "DOC")),
                    "authority": int(metadata.get("authority", 2)),
                    "channel": list(metadata.get("channel", ["all"])),
                    "effective_from": str(metadata.get("effective_from", "") or ""),
                    "version": int(metadata.get("version", 1)),
                    "enabled": True,
                }
            )
        client = self._get_client()
        client.upsert(collection_name=self._collection, data=rows)
        return len(rows)

    async def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索。"""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "collection_name": self._collection,
            "data": [list(vector)],
            "limit": top_k,
            "output_fields": [
                "chunk_id",
                "content",
                "category",
                "sku_ids",
                "knowledge_type",
                "authority",
                "channel",
                "effective_from",
                "version",
            ],
        }
        if filter_expr:
            kwargs["filter"] = filter_expr
        raw = client.search(**kwargs)
        out: list[dict[str, Any]] = []
        for hits in raw:
            for hit in hits:
                entity = hit.get("entity", {})
                out.append(
                    {
                        "chunk_id": entity.get("chunk_id") or hit.get("id"),
                        "content": entity.get("content", ""),
                        "score": float(hit.get("distance", 0.0)),
                        "metadata": {
                            k: v
                            for k, v in entity.items()
                            if k not in {"chunk_id", "content"}
                        },
                    }
                )
        return out

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        client = self._get_client()
        client.delete(
            collection_name=self._collection, ids=[str(c) for c in chunk_ids]
        )
        return len(chunk_ids)

    async def count(self) -> int:
        """集合内实体数。"""
        client = self._get_client()
        stats = client.get_collection_stats(collection_name=self._collection)
        return int(stats.get("row_count", 0))

    async def health(self) -> bool:
        """连接探测。"""
        try:
            client = self._get_client()
            client.list_collections()
            return True
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("Milvus 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_STORE: Any = None


def get_vector_store() -> Any:
    """获取向量库实现（单例）。

    策略：`WK_ENV=local` 用 numpy 实现；其余环境用 Milvus。
    `WK_VECTOR_STORE=local|milvus` 可强制覆盖（便于生产环境临时降级演练）。
    """
    global _STORE  # noqa: PLW0603
    if _STORE is None:
        import os

        settings = get_settings()
        forced = os.getenv("WK_VECTOR_STORE", "").strip().lower()
        use_local = forced == "local" or (not forced and settings.env == "local")
        if use_local:
            logger.info("向量库实现：NumpyVectorStore（本地等价实现）")
            _STORE = NumpyVectorStore(
                settings.local_index_dir / "vectors", dim=settings.embedding_dim
            )
        else:
            logger.info("向量库实现：MilvusVectorStore")
            _STORE = MilvusVectorStore(
                host=settings.milvus_host,
                port=settings.milvus_port,
                collection=settings.milvus_collection,
                dim=settings.embedding_dim,
                user=settings.milvus_user,
                password=settings.milvus_password.get_secret_value(),
            )
    return _STORE


def set_vector_store(store: Any) -> None:
    """替换向量库实现（测试用）。"""
    global _STORE  # noqa: PLW0603
    _STORE = store


__all__ = [
    "ALLOWED_METADATA_FIELDS",
    "FORBIDDEN_FIELDS",
    "ForbiddenFieldError",
    "MilvusVectorStore",
    "NumpyVectorStore",
    "assert_no_forbidden_fields",
    "get_vector_store",
    "set_vector_store",
]
