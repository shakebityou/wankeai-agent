"""BFF · 知识库（契约 C 2.5.1 的 `/bff/v1/kb/*`，FE-05 ~ FE-08）。

坐席侧知识管理：列表、详情、新增、修改、软删、审核、导入、检索、纠错、统计。

**字段裁剪**：`BffKbChunk` 只下发坐席需要的字段；`ChunkMetadata` 里的
`brand`/`keywords`/`source_row` 等内部字段不外发（见 `_to_bff_chunk`）。

**复用**：底层就是 `KnowledgeService`（与 `/internal/v1/kb/*` 同一套逻辑），
BFF 只是换了一层契约模型 + 坐席鉴权，避免两套写库路径漂移。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select, update

from app.api.bff.deps import AgentPrincipalDep, BffAuthGuard, require_role
from app.api.deps import PageParamsDep
from app.core.errors import NotFoundError, ValidationError
from app.core.response import ok, PageResult
from app.db.engine import session_scope
from app.schemas.bff import (
    BffKbChunk,
    BffKbChunkCreate,
    BffKbFeedbackRequest,
    BffKbSearchRequest,
    BffKbSearchResponse,
)
from app.schemas.knowledge import ChunkCreateRequest, ChunkMetadata, ChunkUpdateRequest
from app.services.knowledge.service import KnowledgeService

router = APIRouter(prefix="/kb", tags=["bff-kb"], dependencies=[BffAuthGuard])

__all__ = ["router"]

#: 审核状态可选值
REVIEW_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "DISABLED"})


def _date_str(value: date | None) -> str | None:
    """日期 → `yyyy-MM-dd` 字符串（BFF 契约里生效/失效日期是字符串）。"""
    return value.isoformat() if isinstance(value, date) else None


def _to_bff_chunk(record: Any) -> BffKbChunk:
    """`ChunkRecord` → `BffKbChunk`（字段裁剪）。"""
    meta = record.metadata
    return BffKbChunk(
        chunk_id=record.chunk_id,
        title=record.title,
        content=record.content,
        category=meta.category or "未分类",
        knowledge_type=str(meta.knowledge_type),
        sku_ids=list(meta.sku_ids or []),
        authority=int(meta.authority),
        version=int(meta.version),
        review_status=meta.review_status,
        enabled=bool(record.enabled),
        effective_from=_date_str(meta.effective_from),
        effective_to=_date_str(meta.effective_to),
        updated_by=meta.updated_by or "system",
        updated_at=record.updated_at,
        source=meta.source or "",
    )


@router.get("/chunks", summary="知识块列表")
def list_chunks(
    page: PageParamsDep,
    principal: AgentPrincipalDep,
    knowledge_type: Annotated[str | None, Query(max_length=16)] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    keyword: Annotated[str | None, Query(max_length=64)] = None,
    enabled: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    """分页列出知识块。"""
    with session_scope() as db:
        total, records = KnowledgeService(db).list_chunks(
            page=page.page,
            page_size=page.page_size,
            knowledge_type=knowledge_type.upper() if knowledge_type else None,
            category=category,
            keyword=keyword,
            enabled=enabled,
        )
        items = [_to_bff_chunk(r).model_dump(mode="json") for r in records]
    return ok(PageResult.create(items, total, page))


@router.get("/chunks/{chunk_id}", summary="知识块详情")
def get_chunk(
    chunk_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """按 ID 取知识块。"""
    with session_scope() as db:
        record = KnowledgeService(db).get_chunk(chunk_id)
        return ok(_to_bff_chunk(record).model_dump(mode="json"))


@router.post("/chunks", summary="新增知识块")
def create_chunk(
    payload: BffKbChunkCreate,
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """新增知识块（写库 + 同步索引）。"""
    meta = ChunkMetadata(
        category=payload.category,
        knowledge_type=payload.knowledge_type.upper(),
        sku_ids=list(payload.sku_ids),
        authority=payload.authority,
        effective_from=date.fromisoformat(payload.effective_from)
        if payload.effective_from
        else None,
        effective_to=date.fromisoformat(payload.effective_to) if payload.effective_to else None,
        source=f"BFF 手工录入（{payload.created_by}）",
        updated_by=payload.created_by,
        review_status="PENDING",
    )
    request = ChunkCreateRequest(
        content=payload.content,
        title=payload.title,
        metadata=meta,
        auto_split=payload.auto_split,
    )
    chunk_id = f"kb_{payload.knowledge_type.lower()}_{uuid4().hex[:8]}"
    with session_scope() as db:
        record = KnowledgeService(db).create_chunk(request, chunk_id=chunk_id)
        return ok(_to_bff_chunk(record).model_dump(mode="json"))


@router.put("/chunks/{chunk_id}", summary="修改知识块")
def update_chunk(
    chunk_id: Annotated[str, Path(min_length=1, max_length=64)],
    payload: dict[str, Any],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """修改知识块（`version` 自增，旧版本进历史）。"""
    content = payload.get("content")
    if content is not None and not str(content).strip():
        raise ValidationError("content 不能为空")
    meta_payload = payload.get("metadata")
    request = ChunkUpdateRequest(
        content=content,
        title=payload.get("title"),
        metadata=ChunkMetadata.model_validate(meta_payload) if meta_payload else None,
        updated_by=str(payload.get("updated_by") or principal.agent_id),
        change_reason=payload.get("change_reason"),
    )
    with session_scope() as db:
        record = KnowledgeService(db).update_chunk(chunk_id, request)
        return ok(_to_bff_chunk(record).model_dump(mode="json"))


@router.delete("/chunks/{chunk_id}", summary="删除知识块（软删）")
def delete_chunk(
    chunk_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """软删知识块（保留版本历史，只置 `enabled=False` 并摘索引）。"""
    with session_scope() as db:
        KnowledgeService(db).delete_chunk(chunk_id, deleted_by=principal.agent_id)
    return ok({"chunk_id": chunk_id, "deleted": True})


@router.get("/chunks/{chunk_id}/history", summary="知识块版本历史")
def chunk_history(
    chunk_id: Annotated[str, Path(min_length=1, max_length=64)],
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """知识块版本历史（新→旧）。"""
    with session_scope() as db:
        entries = KnowledgeService(db).get_history(chunk_id)
        return ok({"items": [e.model_dump(mode="json") for e in entries]})


@router.post("/chunks/{chunk_id}/review", summary="知识块审核")
def review_chunk(
    chunk_id: Annotated[str, Path(min_length=1, max_length=64)],
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """审核知识块（仅主管/管理员）。

    请求体：`{review_status: PENDING|APPROVED|REJECTED|DISABLED}`。
    """
    status = str(payload.get("review_status") or "").upper()
    if status not in REVIEW_STATUSES:
        raise ValidationError(f"review_status 只能是 {sorted(REVIEW_STATUSES)}")
    with session_scope() as db:
        service = KnowledgeService(db)
        record = service.get_chunk(chunk_id, include_disabled=True)
        from app.core.enums import ReviewStatus

        meta = record.metadata.model_copy(update={"review_status": ReviewStatus(status)})
        updated = service.update_chunk(
            chunk_id,
            ChunkUpdateRequest(
                metadata=meta,
                updated_by=principal.agent_id,
                change_reason=f"审核：{status}",
            ),
        )
        return ok(_to_bff_chunk(updated).model_dump(mode="json"))


@router.post("/search", summary="坐席侧知识检索")
def search_knowledge(
    payload: BffKbSearchRequest,
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """检索知识库（与机器人同一条 RAG 链路，便于坐席复现「为什么这么答」）。"""
    from app.schemas.rag import RagFilters, RagRetrieveRequest
    from app.services.rag.service import get_rag_service

    filters = RagFilters(
        category=payload.category,
        knowledge_type=payload.knowledge_type.upper() if payload.knowledge_type else None,
    )
    response = get_rag_service().retrieve(
        RagRetrieveRequest(query=payload.query, top_n_rerank=payload.top_k, filters=filters)
    )
    items = [
        {
            "chunk_id": hit.chunk_id,
            "score": hit.score,
            "category": (hit.metadata or {}).get("category"),
            "knowledge_type": (hit.metadata or {}).get("knowledge_type"),
            "title": (hit.metadata or {}).get("title"),
            "content": hit.content[:500],
            "sources": list(hit.sources),
        }
        for hit in response.hits
    ]
    result = BffKbSearchResponse(
        total=len(items),
        top_score=float(response.top_score or 0.0),
        decision=str(response.decision),
        items=items,
    )
    return ok(result.model_dump(mode="json"))


@router.post("/feedback", summary="坐席纠错上报")
def submit_feedback(
    payload: BffKbFeedbackRequest,
    principal: AgentPrincipalDep,
) -> dict[str, Any]:
    """上报「这条答错了」，进入知识运营闭环。

    **幂等**：同一 `message_id` 重复上报返回已有记录（前端连点两次不会产生两条待办）。
    校验按前端契约：`message_id` 与 `session_id` 必填、`error_type` 必填，
    缺失一律 400/10001——这是运营统计的分组维度，不能空着进来。
    """
    message_id = (payload.message_id or "").strip()
    session_id = (payload.session_id or "").strip()
    if not message_id and not session_id:
        raise ValidationError("message_id 与 session_id 不能同时为空")
    if not payload.error_type:
        raise ValidationError("error_type 不能为空")

    chunk_ids = [c for c in (payload.chunk_ids or []) if c]
    if not chunk_ids and payload.chunk_id:
        chunk_ids = [payload.chunk_id]

    from app.api.bff.kb_ops import _feedback_item
    from app.db.models.knowledge import KbFeedback

    with session_scope() as db:
        if message_id:
            existing = (
                db.execute(
                    select(KbFeedback).where(KbFeedback.message_id == message_id)
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return ok(_feedback_item(existing), "该消息已上报过纠错")

        row = KbFeedback(
            session_id=session_id,
            chunk_id=chunk_ids[0] if chunk_ids else None,
            chunk_ids=chunk_ids,
            message_id=message_id,
            error_type=payload.error_type,
            comment=payload.comment or payload.answer_text or "",
            answer_text=payload.answer_text or payload.comment or "",
            reason=payload.reason,
            expected_answer=payload.expected_answer or "",
            status="OPEN",
            reported_by=payload.reported_by or principal.display_name or principal.agent_id,
        )
        db.add(row)
        db.flush()
        item = _feedback_item(row)
    return ok(item, "纠错已上报")


@router.get("/stats", summary="知识库统计")
def kb_stats(principal: AgentPrincipalDep) -> dict[str, Any]:
    """知识库统计（块数 / 元数据完整率 / 分类分布）。"""
    with session_scope() as db:
        stats = KnowledgeService(db).stats()
        return ok(stats.model_dump(mode="json"))


@router.post("/import", summary="批量导入知识")
def import_knowledge(
    payload: dict[str, Any],
    principal: Annotated[Any, Depends(require_role("supervisor", "admin"))],
) -> dict[str, Any]:
    """批量导入（仅主管/管理员）。

    **两种请求体**：

    1. 走预览流程（前端 `commitKbImport`）：
       `{import_id, kb_type, category, authority, channel, excluded_indexes}` ——
       取 `import/preview` 登记的任务，剔除人工勾掉的单元后入库，返回 `ImportProgress`。
    2. 直接给行（内部/脚本）：
       `{rows: [{content, title, metadata?}], source_path?}` —— 老形状，保持不变。

    **幂等**：同 `chunk_id` 且正文一致则跳过。
    """
    import_id = str(payload.get("import_id") or "").strip()
    if import_id:
        return _commit_preview_import(payload, import_id, principal)

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValidationError("rows 不能为空")
    source_path = str(payload.get("source_path") or "bff-manual-import")

    from app.services.knowledge.importer import ImportRow

    import_rows: list[Any] = []
    for idx, item in enumerate(rows, start=1):
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            raise ValidationError(f"第 {idx} 行缺少 content")
        meta_payload = item.get("metadata") or {}
        try:
            meta = ChunkMetadata.model_validate(meta_payload)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"第 {idx} 行 metadata 不合法：{exc}") from exc
        import_rows.append(
            ImportRow(
                title=item.get("title") or "",
                content=str(item["content"]),
                knowledge_type=meta.knowledge_type,
                category=meta.category,
                sku_ids=list(meta.sku_ids or []),
                keywords=list(meta.keywords or []),
                source=meta.source or source_path,
                source_row=idx,
                authority=int(meta.authority),
            )
        )
    with session_scope() as db:
        total, inserted, updated, skipped, errors = KnowledgeService(db).import_rows(
            import_rows, source_path=source_path, created_by=principal.agent_id
        )
        return ok(
            {
                "total": total,
                "inserted": inserted,
                "updated": updated,
                "skipped": skipped,
                "errors": errors[:10],
            }
        )


def _commit_preview_import(
    payload: dict[str, Any],
    import_id: str,
    principal: Any,
) -> dict[str, Any]:
    """提交预览导入：剔除勾掉的单元，逐条入库，回写任务进度。"""
    from app.services.knowledge import import_tasks
    from app.services.knowledge.importer import ImportRow
    from app.services.knowledge.splitter import extract_keywords

    task = import_tasks.get_task(import_id)
    if task is None:
        raise NotFoundError("导入任务不存在")

    excluded_raw = payload.get("excluded_indexes") or []
    if not isinstance(excluded_raw, list):
        raise ValidationError("excluded_indexes 必须是数组")
    try:
        excluded = {int(item) for item in excluded_raw}
    except (TypeError, ValueError) as exc:
        raise ValidationError("excluded_indexes 只能包含整数下标") from exc

    kb_type = str(payload.get("kb_type") or task["detected_type"]).upper()
    if kb_type not in import_tasks.TYPE_RANGES:
        raise ValidationError(
            f"kb_type 只能是 {'/'.join(import_tasks.TYPE_RANGES)}"
        )
    backend_type = import_tasks.BACKEND_TYPE[kb_type]

    category = str(payload.get("category") or "").strip() or "未分类"
    authority = _parse_authority(payload.get("authority"))
    channel = str(payload.get("channel") or "ALL").strip() or "ALL"
    channels = ["all"] if channel.upper() == "ALL" else [channel.upper()]

    kept = [c for c in task["chunks"] if int(c["index"]) not in excluded]
    failures = [
        {
            "row": int(c["index"]),
            "reason": "人工在预览中剔除",
            "raw": str(c["title"]),
        }
        for c in task["chunks"]
        if int(c["index"]) in excluded
    ]
    if not kept:
        import_tasks.finish_task(
            import_id,
            succeeded=0,
            failed=len(failures),
            failures=failures,
            status="FAILED",
        )
        raise ValidationError("所有切分单元都被剔除，没有可导入的内容")

    import_rows = [
        ImportRow(
            title=chunk["title"],
            content=chunk["content"],
            knowledge_type=backend_type,
            category=category,
            keywords=extract_keywords(chunk["content"]),
            source=task["file_name"],
            source_row=int(chunk["index"]),
            authority=authority,
            extra={"channel": channels},
        )
        for chunk in kept
    ]

    with session_scope() as db:
        total, inserted, updated, skipped, errors = KnowledgeService(db).import_rows(
            import_rows,
            source_path=task["file_name"],
            created_by=principal.display_name or principal.agent_id,
        )
        # `KnowledgeService._build_chunk` 把 channel 写死成 ["all"]（导入材料的
        # 默认口径）；坐席在导入弹窗里选了具体渠道时，这里按来源文件回填一次。
        if channels != ["all"]:
            from app.db.models.knowledge import KbChunk

            db.execute(
                update(KbChunk)
                .where(KbChunk.source == task["file_name"])
                .values(channel=list(channels))
            )

    for error in errors:
        failures.append(
            {"row": 0, "reason": str(error)[:200], "raw": "导入失败"}
        )
    status = "PARTIAL_FAILED" if failures else "DONE"
    progress = import_tasks.finish_task(
        import_id,
        succeeded=int(inserted) + int(updated),
        failed=len(failures),
        failures=failures,
        status=status,
    )
    return ok(progress or {}, f"已导入 {inserted} 条、更新 {updated} 条、跳过 {skipped} 条")


def _parse_authority(raw: Any) -> int:
    """把 `"1"` / `"L1"` / `1` 都归一成 1–5 的权威度。"""
    text = str(raw or "3").strip().upper().lstrip("L")
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, value))
