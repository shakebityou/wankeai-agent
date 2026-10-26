"""知识库服务（`BE-06`）。

职责边界（契约 `2.4.3` 的 6 个内部接口）：

| 接口 | 方法 |
|---|---|
| `POST /internal/v1/kb/chunks` | `create_chunk` |
| `PUT /internal/v1/kb/chunks/{chunk_id}` | `update_chunk` |
| `DELETE /internal/v1/kb/chunks/{chunk_id}` | `delete_chunk` |
| `POST /internal/v1/kb/import` | `import_source` / `import_merchant_package` |
| `POST /internal/v1/kb/reindex` | `reindex` |
| `GET /internal/v1/kb/chunks/{chunk_id}/history` | `get_history` |

**写路径必须同时更新三处**：关系库（真相源）+ 向量库 + 稀疏索引。
任一处漏更，检索就会出现「库里改了、索引还是旧的」——
这类不一致极难排查（因为 RAG 查询照常返回结果，只是内容是旧的）。
所以 `KnowledgeService` 把这三步收在一个方法里，路由层不许绕过它直接改库。

**`FORBIDDEN_FIELDS` 是 HC-3 的最后一道闸**：价格/库存字段在
`assert_no_forbidden_fields` 被拦，导入侧也不写 content，双重保险。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.embedding import get_embedder
from app.adapters.ports import EmbedderPort, SparseIndexPort, VectorStorePort
from app.adapters.sparse_index import get_sparse_index
from app.adapters.vector_store import assert_no_forbidden_fields, get_vector_store
from app.core.enums import KnowledgeType, ReviewStatus
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models.knowledge import KbChunk, KbChunkVersion, KbFeedback, KbImportJob
from app.schemas.knowledge import (
    ChunkCreateRequest,
    ChunkHistoryEntry,
    ChunkMetadata,
    ChunkRecord,
    ChunkUpdateRequest,
    KbStats,
    ReindexRequest,
    ReindexResponse,
)

logger = get_logger(__name__)

#: 单次向量化批量大小（阿里 text-embedding-v3 上限 10，本地实现无限制；
#: 取小值兼顾 API 提供商的批限制与内存占用）
EMBED_BATCH_SIZE = 10


class KnowledgeService:
    """知识库管理服务。

    Attributes:
        db: SQLAlchemy 会话（由调用方管理事务）。
        vector_store: 向量库端口。
        sparse_index: 稀疏索引端口。
        embedder: 向量化端口。
    """

    def __init__(
        self,
        db: Session,
        *,
        vector_store: VectorStorePort | None = None,
        sparse_index: SparseIndexPort | None = None,
        embedder: EmbedderPort | None = None,
    ) -> None:
        """构造服务。

        Args:
            db: 数据库会话。
            vector_store: 向量库端口；None 时取全局单例。
            sparse_index: 稀疏索引端口；None 时取全局单例。
            embedder: 向量化端口；None 时取全局单例。
        """
        self.db = db
        self.vector_store = vector_store if vector_store is not None else get_vector_store()
        self.sparse_index = sparse_index if sparse_index is not None else get_sparse_index()
        self.embedder = embedder if embedder is not None else get_embedder()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_chunk(self, chunk_id: str, *, include_disabled: bool = False) -> ChunkRecord:
        """按 ID 取 chunk。

        Args:
            chunk_id: chunk 业务主键。
            include_disabled: 是否返回已软删（`enabled=False`）的块。默认 False。

        Returns:
            `ChunkRecord`。

        Raises:
            NotFoundError: chunk 不存在，或已被软删且未要求包含。

        Note:
            `delete_chunk` 是**软删**（保留版本历史、只置 `enabled=False` 并摘索引），
            所以这里必须显式过滤，否则会出现「检索侧已经查不到、管理侧还能查到」
            的状态分裂，运营会以为删除失败而反复删除。
        """
        chunk = self.db.get(KbChunk, chunk_id)
        if chunk is None or (not chunk.enabled and not include_disabled):
            raise NotFoundError(f"知识 chunk {chunk_id} 不存在")
        return _to_record(chunk)

    def list_chunks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        knowledge_type: str | None = None,
        category: str | None = None,
        keyword: str | None = None,
        enabled: bool | None = None,
    ) -> tuple[int, list[ChunkRecord]]:
        """分页列出 chunk。

        Args:
            page: 页码（1-based）。
            page_size: 每页条数（≤50）。
            knowledge_type: 按知识类型过滤。
            category: 按分类精确过滤。
            keyword: 标题/正文模糊搜索。
            enabled: 按启用状态过滤。

        Returns:
            `(总数, 当前页记录列表)`。
        """
        stmt = select(KbChunk)
        count_stmt = select(func.count()).select_from(KbChunk)

        if knowledge_type:
            stmt = stmt.where(KbChunk.knowledge_type == knowledge_type)
            count_stmt = count_stmt.where(KbChunk.knowledge_type == knowledge_type)
        if category:
            stmt = stmt.where(KbChunk.category == category)
            count_stmt = count_stmt.where(KbChunk.category == category)
        if enabled is not None:
            stmt = stmt.where(KbChunk.enabled == enabled)
            count_stmt = count_stmt.where(KbChunk.enabled == enabled)
        if keyword:
            pattern = f"%{keyword}%"
            stmt = stmt.where(KbChunk.title.like(pattern) | KbChunk.content.like(pattern))
            count_stmt = count_stmt.where(
                KbChunk.title.like(pattern) | KbChunk.content.like(pattern)
            )

        total = int(self.db.execute(count_stmt).scalar_one())
        stmt = stmt.order_by(KbChunk.updated_at.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size)
        rows = list(self.db.execute(stmt).scalars())
        return total, [_to_record(row) for row in rows]

    def get_history(self, chunk_id: str) -> list[ChunkHistoryEntry]:
        """取 chunk 版本历史（新→旧）。

        Args:
            chunk_id: chunk 主键。

        Returns:
            版本历史列表。

        Raises:
            NotFoundError: chunk 不存在。
        """
        chunk = self.db.get(KbChunk, chunk_id)
        if chunk is None:
            raise NotFoundError(f"知识 chunk {chunk_id} 不存在")
        return [
            ChunkHistoryEntry(
                chunk_id=version.chunk_id,
                version=version.version,
                content=version.content,
                metadata=version.metadata_snapshot,
                updated_by=version.updated_by,
                # 取 created_at 而非 updated_at：版本快照行是**不可变**的，
                # 写入后不再更新，表里也确实只有 created_at。契约字段名叫
                # updated_at 是站在「这条记录的变更时间」角度命名的。
                updated_at=version.created_at,
                change_reason=version.change_reason,
                action=version.action,
            )
            for version in chunk.versions
        ]

    def stats(self) -> KbStats:
        """汇总统计（运营看板用）。

        Returns:
            `KbStats`。
        """
        total = int(
            self.db.execute(select(func.count()).select_from(KbChunk)).scalar_one()
        )
        enabled = int(
            self.db.execute(
                select(func.count()).select_from(KbChunk).where(KbChunk.enabled.is_(True))
            ).scalar_one()
        )
        by_type_rows = self.db.execute(
            select(KbChunk.knowledge_type, func.count()).group_by(KbChunk.knowledge_type)
        ).all()
        by_category_rows = self.db.execute(
            select(KbChunk.category, func.count()).group_by(KbChunk.category)
        ).all()
        avg_length = self.db.execute(select(func.avg(func.length(KbChunk.content)))).scalar()
        last_updated = self.db.execute(select(func.max(KbChunk.updated_at))).scalar()

        return KbStats(
            total_chunks=total,
            by_type={str(k): int(v) for k, v in by_type_rows},
            by_category={str(k): int(v) for k, v in by_category_rows},
            enabled_chunks=enabled,
            avg_length=float(avg_length) if avg_length is not None else 0.0,
            metadata_completeness=self._metadata_completeness(),
            last_updated_at=last_updated,
        )

    def _metadata_completeness(self) -> float:
        """计算元数据完整率（DoD 要求 100%）。

        完整 = category / knowledge_type / sku_ids 非空（PRODUCT 类型必须有 SKU）、
        keywords 非空、authority 在 1–5、review_status 已审核。

        Returns:
            0–1 之间的完整率。
        """
        chunks = list(self.db.execute(select(KbChunk)).scalars())
        if not chunks:
            return 1.0
        complete = 0
        for chunk in chunks:
            ok = (
                bool(chunk.category)
                and bool(chunk.knowledge_type)
                and bool(chunk.keywords)
                and 1 <= chunk.authority <= 5
                and chunk.review_status in {str(ReviewStatus.APPROVED)}
            )
            if chunk.knowledge_type == str(KnowledgeType.PRODUCT):
                ok = ok and bool(chunk.sku_ids)
            if ok:
                complete += 1
        return complete / len(chunks)

    # ------------------------------------------------------------------
    # 写路径
    # ------------------------------------------------------------------

    def create_chunk(self, payload: ChunkCreateRequest, *, chunk_id: str) -> ChunkRecord:
        """新建 chunk（契约 `POST /internal/v1/kb/chunks`）。

        Args:
            payload: 创建请求。
            chunk_id: 调用方生成的业务主键。

        Returns:
            新建的 `ChunkRecord`。

        Raises:
            ConflictError: chunk_id 已存在。
        """
        assert_no_forbidden_fields(payload.metadata.model_dump())
        existing = self.db.get(KbChunk, chunk_id)
        if existing is not None:
            raise ConflictError(f"知识 chunk {chunk_id} 已存在")

        chunk = KbChunk(
            chunk_id=chunk_id,
            content=payload.content,
            title=payload.title,
            knowledge_type=str(payload.metadata.knowledge_type),
            category=payload.metadata.category,
            sub_category=payload.metadata.sub_category,
            sku_ids=list(payload.metadata.sku_ids),
            channel=list(payload.metadata.channel),
            authority=payload.metadata.authority,
            brand=payload.metadata.brand,
            keywords=list(payload.metadata.keywords),
            source=payload.metadata.source or payload.source,
            effective_from=payload.metadata.effective_from,
            effective_to=payload.metadata.effective_to,
            review_status=str(payload.metadata.review_status),
            version=1,
            enabled=True,
            updated_by=payload.metadata.updated_by,
        )
        self.db.add(chunk)
        self.db.flush()
        self._index_chunk(chunk)
        logger.info(
            "知识 chunk 已创建 chunk_id=%s type=%s",
            chunk_id,
            chunk.knowledge_type,
        )
        return _to_record(chunk)

    def update_chunk(
        self,
        chunk_id: str,
        payload: ChunkUpdateRequest,
    ) -> ChunkRecord:
        """更新 chunk（契约 `PUT /internal/v1/kb/chunks/{chunk_id}`）。

        **先快照旧值到版本表**，再改主表——回滚只需把快照拷回来。
        快照失败则整体回滚，不会出现「改了主表没留历史」。

        Args:
            chunk_id: chunk 主键。
            payload: 更新请求。

        Returns:
            更新后的 `ChunkRecord`。

        Raises:
            NotFoundError: chunk 不存在。
        """
        chunk = self.db.get(KbChunk, chunk_id)
        if chunk is None:
            raise NotFoundError(f"知识 chunk {chunk_id} 不存在")

        metadata_payload = payload.metadata or ChunkMetadata()
        if payload.content or payload.metadata:
            assert_no_forbidden_fields(
                {
                    "content": payload.content or "",
                    **metadata_payload.model_dump(),
                }
            )

        self._snapshot(
            chunk,
            action="UPDATE",
            updated_by=payload.updated_by,
            change_reason=payload.change_reason,
        )

        if payload.content is not None:
            chunk.content = payload.content
        if payload.title is not None:
            chunk.title = payload.title
        if payload.metadata is not None:
            chunk.category = metadata_payload.category
            chunk.sub_category = metadata_payload.sub_category
            chunk.sku_ids = list(metadata_payload.sku_ids)
            chunk.channel = list(metadata_payload.channel)
            chunk.authority = metadata_payload.authority
            chunk.keywords = list(metadata_payload.keywords)
            chunk.review_status = str(metadata_payload.review_status)
            if metadata_payload.effective_from is not None:
                chunk.effective_from = metadata_payload.effective_from
            if metadata_payload.effective_to is not None:
                chunk.effective_to = metadata_payload.effective_to
        chunk.version = chunk.version + 1
        chunk.updated_by = payload.updated_by
        self.db.flush()
        self._index_chunk(chunk)
        logger.info(
            "知识 chunk 已更新 chunk_id=%s version=%s", chunk_id, chunk.version
        )
        return _to_record(chunk)

    def delete_chunk(self, chunk_id: str, *, deleted_by: str = "system") -> None:
        """删除 chunk（契约 `DELETE /internal/v1/kb/chunks/{chunk_id}`）。

        **软删不禁用**：DoD 要求「版本回滚演练通过」与「可删除可追溯」，
        所以这里保留版本快照、把主表行置为 `enabled=False`，
        再从向量库与稀疏索引摘掉——检索不再命中，但历史可查。

        Args:
            chunk_id: chunk 主键。
            deleted_by: 操作人。

        Raises:
            NotFoundError: chunk 不存在。
        """
        chunk = self.db.get(KbChunk, chunk_id)
        if chunk is None:
            raise NotFoundError(f"知识 chunk {chunk_id} 不存在")

        self._snapshot(chunk, action="DELETE", updated_by=deleted_by, change_reason="删除")
        chunk.enabled = False
        chunk.version = chunk.version + 1
        chunk.updated_by = deleted_by
        # 留痕：谁在什么时候删的（合规「可删除可追溯」）
        chunk.deleted_at = utcnow()
        chunk.deleted_by = deleted_by
        self.db.flush()

        self._deindex_chunk(chunk_id)
        logger.info("知识 chunk 已删除（软删）chunk_id=%s by=%s", chunk_id, deleted_by)

    def _snapshot(
        self,
        chunk: KbChunk,
        *,
        action: str,
        updated_by: str,
        change_reason: str,
    ) -> None:
        """把 chunk 当前值快照进版本表。

        Args:
            chunk: 目标 chunk（改之前调用）。
            action: CREATE / UPDATE / DELETE / ROLLBACK。
            updated_by: 操作人。
            change_reason: 变更原因。
        """
        self.db.add(
            KbChunkVersion(
                chunk_id=chunk.chunk_id,
                version=chunk.version,
                action=action,
                content=chunk.content,
                metadata_snapshot={
                    "title": chunk.title,
                    "knowledge_type": chunk.knowledge_type,
                    "category": chunk.category,
                    "sub_category": chunk.sub_category,
                    "sku_ids": list(chunk.sku_ids),
                    "channel": list(chunk.channel),
                    "authority": chunk.authority,
                    "brand": chunk.brand,
                    "keywords": list(chunk.keywords),
                    "source": chunk.source,
                    "review_status": chunk.review_status,
                    "effective_from": chunk.effective_from.isoformat()
                    if chunk.effective_from
                    else None,
                    "effective_to": chunk.effective_to.isoformat()
                    if chunk.effective_to
                    else None,
                },
                change_reason=change_reason,
                updated_by=updated_by,
            )
        )
        self.db.flush()

    # ------------------------------------------------------------------
    # 索引同步
    # ------------------------------------------------------------------

    def _index_chunk(self, chunk: KbChunk) -> None:
        """把单个 chunk 写入向量库与稀疏索引。

        **HC-3 在这里落地**：先 `assert_no_forbidden_fields` 再入库，
        价格/库存字段写向量库会直接抛 `ForbiddenFieldError`。

        Args:
            chunk: 目标 chunk。
        """
        metadata = _vector_metadata(chunk)
        assert_no_forbidden_fields(metadata)
        try:
            vectors = _run_embed(self.embedder, [chunk.content])
            _run_maybe_async(
                self.vector_store.upsert(
                [
                    {
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "title": chunk.title,
                        "metadata": metadata,
                    }
                ],
                vectors,
                )
            )
            _run_maybe_async(
                self.sparse_index.upsert(
                [
                    {
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "title": chunk.title,
                        "metadata": metadata,
                    }
                ]
                )
            )
        except Exception:
            logger.exception("chunk 入索引失败 chunk_id=%s", chunk.chunk_id)
            raise

    def _deindex_chunk(self, chunk_id: str) -> None:
        """从向量库与稀疏索引摘掉 chunk。

        Args:
            chunk_id: chunk 主键。
        """
        try:
            _run_maybe_async(self.vector_store.delete([chunk_id]))
            _run_maybe_async(self.sparse_index.delete([chunk_id]))
        except Exception:
            logger.exception("chunk 出索引失败 chunk_id=%s", chunk_id)
            raise

    async def reindex(self, payload: ReindexRequest) -> ReindexResponse:
        """重建索引（契约 `POST /internal/v1/kb/reindex`）。

        全量重建时**先清空再写入**：增量重建遇到「chunk 已改但 ID 没变」会
        漏掉旧向量的清理，留下幽灵数据（检索能命中、点进去内容对不上）。

        Args:
            payload: 重建请求。

        Returns:
            `ReindexResponse`。
        """
        started = time.perf_counter()
        stmt = select(KbChunk).where(KbChunk.enabled.is_(True))
        if payload.chunk_ids:
            stmt = stmt.where(KbChunk.chunk_id.in_(payload.chunk_ids))
        chunks = list(self.db.execute(stmt).scalars())

        indexed = 0
        failed = 0
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            try:
                vectors = _run_embed(self.embedder, [c.content for c in batch])
                if payload.rebuild_vectors:
                    _run_maybe_async(
                        self.vector_store.upsert(
                        [
                            {
                                "chunk_id": c.chunk_id,
                                "content": c.content,
                                "title": c.title,
                                "metadata": _vector_metadata(c),
                            }
                            for c in batch
                        ],
                        vectors,
                        )
                    )
                if payload.rebuild_sparse:
                    _run_maybe_async(
                        self.sparse_index.upsert(
                        [
                            {
                                "chunk_id": c.chunk_id,
                                "content": c.content,
                                "title": c.title,
                                "metadata": _vector_metadata(c),
                            }
                            for c in batch
                        ]
                        )
                    )
                indexed += len(batch)
            except Exception:
                failed += len(batch)
                logger.exception("重建索引批次失败 start=%s", start)

        return ReindexResponse(
            total=len(chunks),
            vectors_indexed=indexed if payload.rebuild_vectors else 0,
            sparse_indexed=indexed if payload.rebuild_sparse else 0,
            failed=failed,
            duration_ms=int((time.perf_counter() - started) * 1000),
            effective_at=date.today(),
        )

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_rows(
        self,
        rows: list[Any],
        *,
        source_path: str,
        dry_run: bool = False,
        created_by: str = "import",
    ) -> tuple[int, int, int, int, list[str]]:
        """把导入行批量写入（含索引同步）。

        **幂等策略**：按 `chunk_id` 判断——已存在且正文一致则跳过（`skipped`），
        已存在但内容不同则更新版本（`updated`），不存在则新增（`inserted`）。
        这样同一份材料重复导入不会产生重复 chunk。

        Args:
            rows: `ImportRow` 列表。
            source_path: 源文件标识。
            dry_run: True 时只统计不落库。
            created_by: 操作人。

        Returns:
            `(total_chunks, inserted, updated, skipped, errors)`。
        """
        inserted = updated = skipped = 0
        errors: list[str] = []

        for row in rows:
            try:
                if dry_run:
                    continue
                chunk_id = self._chunk_id_for(row)
                existing = self.db.get(KbChunk, chunk_id)
                if existing is None:
                    self.db.add(self._build_chunk(chunk_id, row))
                    inserted += 1
                elif existing.content == row.content:
                    skipped += 1
                else:
                    self._snapshot(
                        existing,
                        action="UPDATE",
                        updated_by=created_by,
                        change_reason=f"导入更新：{source_path}",
                    )
                    self._apply_row(existing, row)
                    updated += 1
                self.db.flush()
            except IntegrityError as exc:
                self.db.rollback()
                errors.append(f"行 {row.source_row}（{row.title}）：{exc.__class__.__name__}")
            except Exception as exc:  # noqa: BLE001
                self.db.rollback()
                errors.append(f"行 {row.source_row}（{row.title}）：{exc}")

        if not dry_run:
            # 落索引（批量，避免逐条 embed）。
            # `__all__` 是聚合标识（多份材料一次导入），此时必须全量刷，
            # 按单份来源文件过滤会一条都匹配不到。
            self._index_all(
                source_path,
                created_by,
                reindex_all=source_path == "__all__",
            )

        return len(rows), inserted, updated, skipped, errors

    def _index_all(
        self,
        source_path: str | None = None,
        created_by: str = "import",
        *,
        reindex_all: bool = False,
    ) -> int:
        """把 chunk 刷入索引。

        Args:
            source_path: 来源文件名。为 `None` 时刷全部启用 chunk。
            created_by: 操作人。
            reindex_all: True 时忽略 `source_path`，刷全部启用 chunk
                （全量重建语义）。调用方传「`__all__`」这类聚合标识时
                必须置 True，否则按来源文件过滤会一条都匹配不到。

        Returns:
            实际刷入索引的 chunk 数。
        """
        if reindex_all or source_path is None or source_path == "__all__":
            stmt = select(KbChunk).where(KbChunk.enabled.is_(True))
        else:
            stmt = select(KbChunk).where(
                KbChunk.enabled.is_(True), KbChunk.source == source_path
            )
        chunks = list(self.db.execute(stmt).scalars())
        indexed = 0
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            try:
                vectors = _run_embed(self.embedder, [c.content for c in batch])
                payload = [
                    {
                        "chunk_id": c.chunk_id,
                        "content": c.content,
                        "title": c.title,
                        "metadata": _vector_metadata(c),
                    }
                    for c in batch
                ]
                _run_maybe_async(self.vector_store.upsert(payload, vectors))
                _run_maybe_async(self.sparse_index.upsert(payload))
                indexed += len(batch)
            except Exception:
                logger.exception("导入后刷索引失败 source=%s start=%s", source_path, start)
        return indexed

    @staticmethod
    def _chunk_id_for(row: Any) -> str:
        """生成稳定的 chunk_id（同一行重复导入得到同一 ID）。

        格式：`kb_{type}_{序号}`，其中序号用源文件 + 源行号哈希，
        保证同一份材料的同一行永远映射到同一 chunk。

        Args:
            row: `ImportRow`。

        Returns:
            chunk_id。
        """
        from app.core.idempotency import build_key

        type_name = str(row.knowledge_type).lower()
        return build_key(f"kb_{type_name}", row.source, str(row.source_row or 0), row.title)

    @staticmethod
    def _build_chunk(chunk_id: str, row: Any) -> KbChunk:
        """把导入行构造成 `KbChunk`。

        Args:
            chunk_id: 业务主键。
            row: `ImportRow`。

        Returns:
            未加入会话的 `KbChunk`。
        """
        return KbChunk(
            chunk_id=chunk_id,
            content=row.content,
            title=row.title,
            knowledge_type=str(row.knowledge_type),
            category=row.category,
            sub_category="",
            sku_ids=list(row.sku_ids),
            channel=["all"],
            authority=row.authority,
            brand="玩可爱",
            keywords=list(row.keywords),
            source=row.source,
            source_row=row.source_row,
            effective_from=row.effective_from,
            review_status=str(ReviewStatus.APPROVED),
            version=1,
            enabled=True,
            updated_by="import",
            extra=dict(row.extra),
        )

    @staticmethod
    def _apply_row(chunk: KbChunk, row: Any) -> None:
        """把导入行的值应用到已存在的 chunk（更新路径）。

        Args:
            chunk: 已存在的 chunk。
            row: `ImportRow`。
        """
        chunk.content = row.content
        chunk.title = row.title
        chunk.category = row.category
        chunk.sku_ids = list(row.sku_ids)
        chunk.keywords = list(row.keywords)
        chunk.authority = row.authority
        chunk.source_row = row.source_row
        chunk.extra = dict(row.extra)
        chunk.version = chunk.version + 1

    def record_import_job(
        self,
        *,
        source_path: str,
        knowledge_type: str,
        total_rows: int,
        total_chunks: int,
        inserted: int,
        updated: int,
        skipped: int,
        failed: int,
        errors: list[str],
        duration_ms: int,
        dry_run: bool,
        created_by: str,
    ) -> KbImportJob:
        """写导入账本（DoD「无丢块」的可核对依据）。

        Args:
            source_path: 源文件路径。
            knowledge_type: 知识类型。
            total_rows: 源数据行数。
            total_chunks: 切分后 chunk 数。
            inserted: 新增数。
            updated: 更新数。
            skipped: 跳过数。
            failed: 失败数。
            errors: 错误明细。
            duration_ms: 耗时毫秒。
            dry_run: 是否仅预览。
            created_by: 操作人。

        Returns:
            落库后的 `KbImportJob`。
        """
        job = KbImportJob(
            source_path=source_path,
            knowledge_type=knowledge_type,
            total_rows=total_rows,
            total_chunks=total_chunks,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            failed=failed,
            errors=errors,
            dry_run=dry_run,
            duration_ms=duration_ms,
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def list_import_jobs(self, *, limit: int = 20) -> list[KbImportJob]:
        """最近的导入账本。

        Args:
            limit: 最多返回几条。

        Returns:
            `KbImportJob` 列表（新→旧）。
        """
        stmt = select(KbImportJob).order_by(KbImportJob.created_at.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars())

    # ------------------------------------------------------------------
    # 反馈
    # ------------------------------------------------------------------

    def submit_feedback(
        self,
        *,
        session_id: str,
        chunk_id: str | None,
        answer_text: str,
        reason: str,
        expected_answer: str,
        reported_by: str,
    ) -> KbFeedback:
        """提交坐席纠错（`FE-08` 闭环入口）。

        Args:
            session_id: 会话 ID。
            chunk_id: 被质疑的 chunk（可空）。
            answer_text: 机器人当时的回答。
            reason: 纠错原因。
            expected_answer: 期望答案。
            reported_by: 上报人。

        Returns:
            落库后的 `KbFeedback`。
        """
        feedback = KbFeedback(
            session_id=session_id,
            chunk_id=chunk_id,
            answer_text=answer_text,
            reason=reason,
            expected_answer=expected_answer,
            reported_by=reported_by,
        )
        self.db.add(feedback)
        self.db.flush()
        return feedback

    def purge_all(self) -> int:
        """清空知识库（仅测试与重置用）。

        Returns:
            删除的行数。
        """
        result = self.db.execute(sa_delete(KbChunk))
        return int(result.rowcount or 0)


def _vector_metadata(chunk: KbChunk) -> dict[str, Any]:
    """构造写入向量库的元数据（**刻意不含价格/库存**）。

    HC-3 的代码层保障：本函数是向量库元数据的**唯一来源**，
    任何价格/库存/物流单号都不允许出现在这里。
    生产实现 `MilvusVectorStore` 的 collection schema 同样不含这些字段，
    双重保险。

    Args:
        chunk: 目标 chunk。

    Returns:
        元数据字典。
    """
    return {
        "category": chunk.category,
        "knowledge_type": chunk.knowledge_type,
        "sku_ids": list(chunk.sku_ids),
        "authority": chunk.authority,
        "channel": list(chunk.channel),
        "brand": chunk.brand,
        "effective_from": chunk.effective_from.isoformat()
        if chunk.effective_from
        else None,
        "effective_to": chunk.effective_to.isoformat() if chunk.effective_to else None,
        "version": chunk.version,
    }


def _run_embed(embedder: Any, texts: Sequence[str]) -> list[list[float]]:
    """同步/异步 embedder 通用调用。

    `EmbedderPort.embed` 是 `async def`，但 `KnowledgeService` 的方法
    目前是同步的（供 Celery 任务与测试直接调用）。这里统一走
    `_run_maybe_async`，**不要自己调 `asyncio.run`**——
    `reindex` 本身是 `async def`，在它内部调 `asyncio.run` 会抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`，
    且被 except 吞成「每一批都失败」，整次重建静默 0 成功。

    Args:
        embedder: 嵌入器实例。
        texts: 待编码文本列表。

    Returns:
        向量列表。
    """
    return list(_run_maybe_async(embedder.embed(list(texts))))


def _run_maybe_async(result: Any) -> Any:
    """同步/异步通用驱动（**已迁到 `app.core.asyncbridge`**）。

    保留本名是为了兼容既有调用点。新代码请直接用
    `from app.core.asyncbridge import run_maybe_async`——
    这个函数是跨模块基础设施，不该让调用方为了它加载整个知识库服务。

    实现细节见 `app.core.asyncbridge.run_maybe_async` 的 docstring：
    三种情形（非 awaitable / 无运行 loop / 有运行 loop）缺一种都会
    静默失败。
    """
    from app.core.asyncbridge import run_maybe_async

    return run_maybe_async(result)


def _to_record(chunk: KbChunk) -> ChunkRecord:
    """`KbChunk` → 契约层 `ChunkRecord`。

    Args:
        chunk: ORM 对象。

    Returns:
        `ChunkRecord`。
    """
    from app.schemas.knowledge import ChunkMetadata

    return ChunkRecord(
        chunk_id=chunk.chunk_id,
        content=chunk.content,
        title=chunk.title,
        metadata=ChunkMetadata(
            category=chunk.category,
            sub_category=chunk.sub_category,
            sku_ids=list(chunk.sku_ids),
            knowledge_type=KnowledgeType(chunk.knowledge_type),
            source=chunk.source,
            authority=chunk.authority,
            channel=list(chunk.channel),
            brand=chunk.brand,
            keywords=list(chunk.keywords),
            updated_by=chunk.updated_by,
            version=chunk.version,
            review_status=ReviewStatus(chunk.review_status),
            source_row=chunk.source_row,
        ),
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
        enabled=chunk.enabled,
    )


__all__ = ["EMBED_BATCH_SIZE", "KnowledgeService"]
