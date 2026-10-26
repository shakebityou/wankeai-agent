"""契约 B 知识库管理路由（任务书 2.4.3 节，对应 BE-06）。

| 端点 | 用途 |
| --- | --- |
| `GET /internal/v1/kb/chunks` | 分页列出知识块（FE-05 列表页） |
| `POST /internal/v1/kb/chunks` | 新增知识块 |
| `PUT /internal/v1/kb/chunks/{chunk_id}` | 更新（先快照后改，支持回滚） |
| `DELETE /internal/v1/kb/chunks/{chunk_id}` | 删除（软删 + 摘索引） |
| `GET /internal/v1/kb/chunks/{chunk_id}/history` | 版本历史（FE-07） |
| `POST /internal/v1/kb/import` | 批量导入（`dry_run=true` 为切分预览） |
| `POST /internal/v1/kb/reindex` | 重建索引（生效延迟 ≤ 5 分钟） |
| `GET /internal/v1/kb/stats` | 知识库统计（运营看板） |
| `POST /internal/v1/kb/feedback` | 坐席纠错上报（FE-08 闭环入口） |

设计约定：
* **写操作一律幂等**：`chunk_id` 由调用方给（新增）或由路径定位（更新/删除），
  重复调用不会产生第二份数据；`import` 按 `chunk_id` 判重（跳过/更新/新增）。
* 事务边界在路由层：一个请求一个事务，服务层只 `flush` 不 `commit`，
  这样批量导入中途失败不会留下"写了一半的知识库"。
* 导入的 `source_path` 允许相对商家材料包（运维常用 `data/FAQ知识库.csv`），
  也允许 `__all__` 表示整包导入。
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from app.api.deps import InternalAuthGuard, PageParamsDep
from app.core.config import REPO_ROOT
from app.core.logging import get_logger
from app.core.response import PageResult, ok
from app.schemas.knowledge import (
    ChunkCreateRequest,
    ChunkHistoryEntry,
    ChunkRecord,
    ChunkUpdateRequest,
    ImportRequest,
    ImportResult,
    ReindexRequest,
)
from app.services.knowledge.importer import (
    collect_merchant_package,
    load_faq,
    load_policy_doc,
    load_product_cards,
    preview_response,
    rows_to_metadata,
)
from app.services.knowledge.service import KnowledgeService

logger = get_logger(__name__)

router = APIRouter(prefix="/kb", tags=["kb"], dependencies=[InternalAuthGuard])


def _service(db: Any) -> KnowledgeService:
    """构造知识库服务（依赖全部走全局单例，便于测试替换）。

    Args:
        db: 数据库会话。

    Returns:
        `KnowledgeService` 实例。
    """
    return KnowledgeService(db)


def _package_dir() -> Any:
    """商家材料包目录。

    取 `settings.merchant_package_dir`（可用 `WK_MERCHANT_PACKAGE_DIR` 覆盖），
    而不是硬编码 `REPO_ROOT / 商家材料包`：容器里材料包挂在 `/data/商家材料包`，
    硬编码会指到 `/app/商家材料包`，导入直接报「缺少必需文件」。

    Returns:
        材料包根目录 `Path`。
    """
    from app.core.config import get_settings

    return get_settings().merchant_package_dir


def _load_rows(source_path: str, knowledge_type: Any) -> tuple[list[Any], int]:
    """按 `source_path` 加载导入行。

    Args:
        source_path: 相对材料包的路径，或 `__all__` 表示整包。
        knowledge_type: 知识类型（仅单文件导入时用于兜底判定）。

    Returns:
        `(导入行列表, 源文件数据行数)`。

    Raises:
        NotFoundError: 指定的源文件不存在。
    """
    from app.core.errors import NotFoundError

    package = _package_dir()

    if source_path in ("__all__", "all", ""):
        rows, missing = collect_merchant_package(package)
        if missing and not rows:
            raise NotFoundError(f"商家材料包缺少必需文件：{missing}")
        return rows, len(rows)

    path = package / source_path
    if not path.exists():
        # 兼容调用方直接传绝对路径（脚本/本地联调）
        from pathlib import Path as _P

        alt = _P(source_path)
        if not alt.exists():
            raise NotFoundError(f"源文件不存在：{source_path}")
        path = alt

    name = path.name
    if name.endswith(".csv"):
        if "FAQ" in name or "faq" in name:
            rows = load_faq(path)
            return rows, len(rows)
        if "知识卡" in name or "商品" in name:
            rows = load_product_cards(path)
            return rows, len(rows)
        # 未知 CSV：按 FAQ 口径尝试（一问一答最常见）
        rows = load_faq(path)
        return rows, len(rows)

    if name.endswith(".md"):
        # 政策类文档需要来源元数据（分类/权威等级），从 POLICY_SOURCES 里找
        from app.services.knowledge.importer import POLICY_SOURCES

        meta = POLICY_SOURCES.get(name)
        if meta is None:
            meta = {
                "category": "售后政策",
                "authority": 1,
                "knowledge_type": knowledge_type,
            }
        rows = load_policy_doc(path, meta=meta)
        return rows, len(rows)

    raise NotFoundError(f"不支持的文件类型：{name}")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


@router.get("/chunks", summary="分页列出知识块", response_model=None)
def list_chunks(
    page: PageParamsDep = ...,
    knowledge_type: Annotated[str | None, Query(description="知识类型")] = None,
    category: Annotated[str | None, Query(description="知识分类")] = None,
    keyword: Annotated[str | None, Query(description="标题/正文关键词")] = None,
    enabled: Annotated[bool | None, Query(description="启用状态")] = None,
) -> dict:
    """分页列出知识块（FE-05 列表页数据源）。

    Returns:
        统一响应包装，`data` 为分页结果。
    """
    from app.db.engine import session_scope

    with session_scope() as session:
        total, items = _service(session).list_chunks(
            page=page.page,
            page_size=page.page_size,
            knowledge_type=knowledge_type,
            category=category,
            keyword=keyword,
            enabled=enabled,
        )
    return ok(
        PageResult.create(
            [i.model_dump(mode="json") for i in items], total, page
        )
    )


@router.get("/chunks/{chunk_id}", summary="查询单个知识块", response_model=None)
def get_chunk(chunk_id: Annotated[str, Path(description="知识块 ID")]) -> dict:
    """按 ID 查知识块。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        record = _service(session).get_chunk(chunk_id)
    return ok(record.model_dump(mode="json"))


@router.get(
    "/chunks/{chunk_id}/history",
    summary="知识块版本历史",
    response_model=None,
)
def get_history(chunk_id: Annotated[str, Path(description="知识块 ID")]) -> dict:
    """版本历史（FE-07 版本对比与回滚的依据）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        entries: list[ChunkHistoryEntry] = _service(session).get_history(chunk_id)
    return ok([e.model_dump(mode="json") for e in entries])


@router.get("/stats", summary="知识库统计", response_model=None)
def stats() -> dict:
    """知识库统计（总数/类型分布/元数据完整率）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        data = _service(session).stats()
    return ok(data.model_dump(mode="json"))


@router.get("/import/jobs", summary="导入账本列表", response_model=None)
def list_import_jobs(
    limit: Annotated[int, Query(ge=1, le=100, description="返回条数")] = 20,
) -> dict:
    """最近的导入记录（核对"无丢块"用）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        jobs = _service(session).list_import_jobs(limit=limit)
        # 服务层返的是 ORM 行（`KbImportJob`），必须在这里转 dict。
        # 直接在 ORM 上调用 `.model_dump()` 会 AttributeError → 500，
        # 而用户只会看到「系统错误」，排查成本极高。
        data = [_job_to_dict(job) for job in jobs]
    return ok(data)


def _job_to_dict(job: Any) -> dict[str, Any]:
    """把 `KbImportJob` ORM 行转成可 JSON 序列化的 dict。

    Args:
        job: 导入账本行。

    Returns:
        字段与 `ImportResult` 对齐的 dict。
    """
    return {
        "id": job.id,
        "source_path": job.source_path,
        "knowledge_type": job.knowledge_type,
        "total_rows": job.total_rows,
        "total_chunks": job.total_chunks,
        "inserted": job.inserted,
        "updated": job.updated,
        "skipped": job.skipped,
        "failed": job.failed,
        "errors": job.errors,
        "dry_run": job.dry_run,
        "duration_ms": job.duration_ms,
        "created_by": job.created_by,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


@router.post("/chunks", summary="新增知识块", response_model=None)
def create_chunk(payload: ChunkCreateRequest) -> dict:
    """新增知识块（`chunk_id` 由服务端生成）。

    幂等说明：内容完全相同的重复提交会产生新 chunk（因为 ID 随机）；
    需要幂等的场景请走 `import`（按源行号生成稳定 ID）。
    """
    from app.core.trace import new_request_id
    from app.db.engine import session_scope

    chunk_id = f"kb_manual:{new_request_id()}"
    with session_scope() as session:
        record: ChunkRecord = _service(session).create_chunk(payload, chunk_id=chunk_id)
    logger.info("kb_chunk_created chunk_id=%s", record.chunk_id)
    return ok(record.model_dump(mode="json"))


@router.put("/chunks/{chunk_id}", summary="更新知识块", response_model=None)
def update_chunk(
    chunk_id: Annotated[str, Path(description="知识块 ID")],
    payload: ChunkUpdateRequest,
) -> dict:
    """更新知识块（先快照旧值，可回滚）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        record: ChunkRecord = _service(session).update_chunk(chunk_id, payload)
    logger.info("kb_chunk_updated chunk_id=%s version=%s", chunk_id, record.metadata.version)
    return ok(record.model_dump(mode="json"))


@router.delete("/chunks/{chunk_id}", summary="删除知识块", response_model=None)
def delete_chunk(
    chunk_id: Annotated[str, Path(description="知识块 ID")],
    deleted_by: Annotated[str, Query(description="操作人")] = "system",
) -> dict:
    """删除知识块（软删 + 摘除索引，重启后可恢复）。

    **为什么是软删**：合规要求"可删除可追溯"（HC-2），物理删除无法
    证明删了什么、谁删的、什么时候删的。
    """
    from app.db.engine import session_scope

    with session_scope() as session:
        _service(session).delete_chunk(chunk_id, deleted_by=deleted_by)
    logger.info("kb_chunk_deleted chunk_id=%s by=%s", chunk_id, deleted_by)
    return ok({"chunk_id": chunk_id, "deleted": True})


# ---------------------------------------------------------------------------
# 导入 / 重建
# ---------------------------------------------------------------------------


@router.post("/import", summary="批量导入（dry_run 为切分预览）", response_model=None)
def import_chunks(payload: ImportRequest) -> dict:
    """批量导入 CSV/XLSX/MD，按知识类型切分后入库。

    `dry_run=True` 只返回切分预览（FE-06 的"导入前必看"），不写库。
    """
    from app.db.engine import session_scope

    started = time.perf_counter()
    rows, total_rows = _load_rows(payload.source_path, payload.knowledge_type)

    if payload.dry_run:
        preview = preview_response(
            payload.source_path,
            rows,
            total_rows=total_rows,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        logger.info("kb_import_preview source=%s chunks=%d", payload.source_path, len(rows))
        # 预览与实导走的是**不同的响应模型**（`ImportPreviewResponse` vs `ImportResult`），
        # 前者本来没有 dry_run 字段。但 FE-06 要靠这个字段区分「这是预览、没写库」，
        # 否则运营看到 126 条成功会以为已经入库。所以这里显式标注。
        data = preview.model_dump(mode="json")
        data["dry_run"] = True
        return ok(data)

    with session_scope() as session:
        service = _service(session)
        total_chunks, inserted, updated, skipped, errors = service.import_rows(
            rows,
            source_path=payload.source_path,
            dry_run=False,
            created_by=payload.created_by,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        service.record_import_job(
            source_path=payload.source_path,
            knowledge_type=str(payload.knowledge_type),
            total_rows=total_rows,
            total_chunks=total_chunks,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            failed=len(errors),
            errors=errors,
            duration_ms=duration_ms,
            dry_run=False,
            created_by=payload.created_by,
        )

    result = ImportResult(
        source_path=payload.source_path,
        total_rows=total_rows,
        total_chunks=total_chunks,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        failed=len(errors),
        errors=errors[:50],
        duration_ms=duration_ms,
    )
    # 导入会改知识 → 检索缓存必须立即失效（否则最长 120s 查到旧内容）
    from app.services.rag.service import get_rag_service

    get_rag_service().clear_cache()

    logger.info(
        "kb_import_done source=%s chunks=%d ins=%d upd=%d skip=%d fail=%d %dms",
        payload.source_path,
        total_chunks,
        inserted,
        updated,
        skipped,
        len(errors),
        duration_ms,
    )
    return ok(result.model_dump(mode="json"))


@router.post("/reindex", summary="重建索引", response_model=None)
async def reindex(payload: ReindexRequest) -> dict:
    """重建向量/稀疏索引（生效延迟 ≤ 5 分钟）。"""
    from app.db.engine import session_scope

    rows: list[dict[str, Any]] = []
    with session_scope() as session:
        service = _service(session)
        response = await service.reindex(payload)
        rows = response.model_dump(mode="json")

    from app.services.rag.service import get_rag_service

    get_rag_service().clear_cache()
    logger.info("kb_reindex total=%s failed=%s", rows.get("total"), rows.get("failed"))
    return ok(rows)


@router.post("/feedback", summary="坐席纠错上报", response_model=None)
def submit_feedback(payload: dict) -> dict:
    """坐席对机器人回答纠错（FE-08 闭环入口）。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        feedback = _service(session).submit_feedback(
            session_id=str(payload.get("session_id") or ""),
            chunk_id=payload.get("chunk_id"),
            answer_text=str(payload.get("answer_text") or ""),
            reason=str(payload.get("reason") or ""),
            expected_answer=str(payload.get("expected_answer") or ""),
            reported_by=str(payload.get("reported_by") or "agent"),
        )
        data = {
            "id": feedback.id,
            "session_id": feedback.session_id,
            "chunk_id": feedback.chunk_id,
            "status": feedback.status,
        }
    logger.info("kb_feedback_submitted id=%s", data["id"])
    return ok(data)


__all__ = ["router"]
