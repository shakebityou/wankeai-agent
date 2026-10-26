"""BFF · 知识库补充端点（契约 C 2.5.1 里 FE-06/FE-07/FE-08 的剩余 5 条）。

补齐 `frontend/mock` 有、后端原先没有的 5 条：

* `POST /bff/v1/kb/import/preview` —— 上传文件，返回切分预览（导入前必看）
* `GET  /bff/v1/kb/import/{import_id}/progress` —— 导入进度
* `GET  /bff/v1/kb/feedback` —— 纠错待办列表（按错误类型 / 状态 / 处理人筛选）
* `PUT  /bff/v1/kb/feedback/{feedback_id}` —— 指派 / 关闭 / 关联修复后的 chunk
* `POST /bff/v1/kb/chunks/{chunk_id}/rollback` —— 回滚到指定版本

`POST /bff/v1/kb/feedback`（纠错上报）留在 `knowledge.py` 里，与知识块增删改查同文件；
提交导入 `POST /bff/v1/kb/import` 也在 `knowledge.py`，本模块只提供预览与进度。

**为什么单开文件**：`knowledge.py` 已 330 行且都是「知识块 CRUD」，把导入任务与
纠错工单塞进去会让一个文件混三件事；这里独立成 `kb-ops` 路由组，两个 router
共用 `/kb` 前缀，FastAPI 按完整路径匹配，互不影响。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, select

from app.api.bff.deps import AgentPrincipalDep, BffAuthGuard, require_role
from app.core.errors import NotFoundError, ValidationError
from app.core.response import ok
from app.db.engine import session_scope
from app.db.models.knowledge import KbChunk, KbFeedback
from app.schemas.knowledge import ChunkMetadata, ChunkUpdateRequest
from app.services.knowledge import import_tasks
from app.services.knowledge.service import KnowledgeService

router = APIRouter(prefix="/kb", tags=["bff-kb-ops"], dependencies=[BffAuthGuard])

__all__ = ["router"]

#: 前端 `FeedbackItem['status']` 的合法取值
FEEDBACK_STATUSES = frozenset({"OPEN", "ASSIGNED", "RESOLVED", "CLOSED"})

#: 后端历史状态 → 前端契约状态（老数据兼容）
_STATUS_TO_FE = {
    "PENDING": "OPEN",
    "ACCEPTED": "RESOLVED",
    "REJECTED": "CLOSED",
}


def _now_iso(value: datetime | None) -> str:
    """时间统一序列化成带时区的 ISO 8601。"""
    if value is None:
        return datetime.now(UTC).isoformat()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.isoformat()


def _feedback_item(row: KbFeedback) -> dict[str, Any]:
    """`KbFeedback` → 前端 `FeedbackItem`（字段名逐个对齐，不做二次猜测）。"""
    chunk_ids = list(row.chunk_ids or [])
    if not chunk_ids and row.chunk_id:
        chunk_ids = [row.chunk_id]
    return {
        "feedback_id": f"FB-{row.id:04d}",
        "message_id": row.message_id or "",
        "session_id": row.session_id or "",
        "chunk_ids": chunk_ids,
        "error_type": row.error_type or "OTHER",
        "comment": row.comment or "",
        "submitted_by": row.reported_by or "",
        "submitted_at": _now_iso(row.created_at),
        "status": _STATUS_TO_FE.get(row.status, row.status),
        "assignee": row.assignee or None,
        "resolved_chunk_id": row.resolved_chunk_id or None,
        "resolution": row.resolution or None,
    }


# ---------------------------------------------------------------------------
# FE-06 批量导入：预览 / 进度
# ---------------------------------------------------------------------------


@router.post("/import/preview", summary="导入前切分预览")
async def preview_import(
    file: Annotated[UploadFile, File(description="待导入的 .csv/.xlsx/.md/.txt 文件")],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """上传文件并返回切分预览。

    **为什么不直接入库**：1000 条材料一次导入，切错了要逐条删。预览让运营先看
    每个切分单元的字数与超长/过短标记，剔除或补写后再提交（契约要求「导入前必看」）。

    解析与切分在 `app.services.knowledge.import_tasks.build_preview`，
    本函数只负责读字节与包 `ok()`。
    """
    data = await file.read()
    preview = import_tasks.build_preview(data, file.filename or "upload")
    return ok(preview, f"已切分为 {preview['total_chunks']} 个单元，请确认后导入")


@router.get("/import/{import_id}/progress", summary="导入进度")
def import_progress(
    import_id: str,
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """查询导入任务进度（任务不存在返回 404/10004）。"""
    view = import_tasks.progress_view(import_id)
    if view is None:
        raise NotFoundError("导入任务不存在")
    return ok(view)


# ---------------------------------------------------------------------------
# FE-08 纠错闭环：待办列表 / 处理
# ---------------------------------------------------------------------------


@router.get("/feedback", summary="纠错待办列表")
def list_feedback(
    principal: AgentPrincipalDep,
    error_type: Annotated[str | None, Query(description="按错误类型筛选")] = None,
    status: Annotated[str | None, Query(description="按处理状态筛选")] = None,
    assignee: Annotated[str | None, Query(description="按处理人筛选")] = None,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=50, description="每页条数")] = 20,
) -> dict[str, Any]:
    """纠错待办列表（新→旧，支持错误类型 / 状态 / 处理人筛选）。"""
    filters = []
    if error_type:
        filters.append(KbFeedback.error_type == error_type)
    if status:
        # 前端传的是 OPEN/ASSIGNED/...；历史数据里可能存着 PENDING，
        # 查 OPEN 时把 PENDING 一起算进来，否则运营会看到「列表比统计少」。
        wanted = {status}
        if status == "OPEN":
            wanted.add("PENDING")
        filters.append(KbFeedback.status.in_(tuple(wanted)))
    if assignee:
        filters.append(KbFeedback.assignee == assignee)

    with session_scope() as db:
        total = int(
            db.execute(
                select(func.count()).select_from(KbFeedback).where(*filters)
            ).scalar_one()
        )
        rows = (
            db.execute(
                select(KbFeedback)
                .where(*filters)
                .order_by(KbFeedback.created_at.desc(), KbFeedback.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        items = [_feedback_item(row) for row in rows]
    return ok(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }
    )


@router.put("/feedback/{feedback_id}", summary="处理纠错（指派/关闭/关联 chunk）")
def update_feedback(
    feedback_id: str,
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """更新纠错记录的处理状态。

    只允许改 `status` / `assignee` / `resolved_chunk_id` / `resolution` 四项：
    错误类型与原始评论是坐席上报时的证据，运营不能改（改了就没法追溯）。
    """
    allowed = {"status", "assignee", "resolved_chunk_id", "resolution"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValidationError(
            f"不支持修改字段：{'、'.join(sorted(unknown))}（仅支持 status/assignee/resolved_chunk_id/resolution）"
        )

    row_id = _parse_feedback_id(feedback_id)
    with session_scope() as db:
        row = db.get(KbFeedback, row_id)
        if row is None:
            raise NotFoundError("纠错记录不存在")

        if "status" in payload and payload["status"] is not None:
            status = str(payload["status"]).upper()
            if status not in FEEDBACK_STATUSES:
                raise ValidationError(
                    f"status 只能是 {'/'.join(sorted(FEEDBACK_STATUSES))}"
                )
            row.status = status
            if status in ("RESOLVED", "CLOSED"):
                row.handled_by = principal.display_name or principal.agent_id
                row.handled_at = datetime.now(UTC)
        if "assignee" in payload:
            row.assignee = str(payload["assignee"] or "")
            if row.status == "OPEN" and row.assignee:
                row.status = "ASSIGNED"
        if "resolved_chunk_id" in payload:
            row.resolved_chunk_id = payload["resolved_chunk_id"] or None
        if "resolution" in payload:
            row.resolution = str(payload["resolution"] or "")

        db.flush()
        item = _feedback_item(row)
    return ok(item, "纠错记录已更新")


def _parse_feedback_id(feedback_id: str) -> int:
    """`FB-0007` / `7` 两种写法都能定位到主键。"""
    raw = str(feedback_id).strip()
    if raw.upper().startswith("FB-"):
        raw = raw[3:]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise NotFoundError("纠错记录不存在") from exc


# ---------------------------------------------------------------------------
# FE-07 版本回滚
# ---------------------------------------------------------------------------


@router.post("/chunks/{chunk_id}/rollback", summary="回滚到指定版本")
def rollback_chunk(
    chunk_id: str,
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """把知识块恢复到历史版本 `version`。

    **实现方式**：不是「删掉新版本」，而是把目标版本的正文/元数据**当作一次新的修改**
    写回主表——`KnowledgeService.update_chunk` 会先把当前值快照进版本表，
    所以回滚本身也可再回滚（审计链不断）。

    Returns:
        `{chunk_id, version: 新版本号, effective_at}`，`version` 是回滚后产生的新版本号。
    """
    try:
        target_version = int(payload.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("version 必须是整数") from exc
    if target_version <= 0:
        raise ValidationError("version 必须是正整数")

    with session_scope() as db:
        service = KnowledgeService(db)
        chunk = service.get_chunk(chunk_id)  # 不存在 → NotFoundError「知识 chunk ... 不存在」
        history = service.get_history(chunk_id)
        target = next((item for item in history if item.version == target_version), None)
        if target is None:
            raise NotFoundError(f"未找到版本 v{target_version}")

        metadata = target.metadata or ChunkMetadata()
        updated = service.update_chunk(
            chunk_id,
            ChunkUpdateRequest(
                content=target.content,
                title=chunk.title,
                metadata=metadata,
                updated_by=principal.display_name or principal.agent_id,
                change_reason=f"回滚到 v{target_version} 前的快照",
            ),
        )
        row = db.get(KbChunk, chunk_id)
        new_version = int(getattr(row, "version", target_version + 1) or 0)
        effective_at = _now_iso(getattr(updated, "updated_at", None))
    return ok(
        {
            "chunk_id": chunk_id,
            "version": new_version,
            "effective_at": effective_at,
        },
        f"已回滚到 v{target_version}，当前版本 v{new_version}",
    )
