"""契约 B RAG 检索路由（任务书 2.4.2 节，对应 BE-05）。

| 端点 | 用途 |
| --- | --- |
| `POST /internal/v1/rag/retrieve` | 六步混合检索（向量 Top20 + BM25 Top20 → RRF → 过滤 → 重排 Top5 → 阈值分流） |

设计约定：
* 路由层**只做参数校验与响应包装**，六步流程全部在
  `app.services.rag.service.RagService` 里，避免同一套阈值判断散落两处。
* 检索是热路径（契约要求 P95 ≤ 200ms），这里不做任何额外 IO——
  连 trace_id 也由中间件注入的 contextvar 提供，不额外查库。
* `answer_context` 的变体（`?with_context=true`）会额外返回送模型的
  上下文片段，**仅供内部服务（BE-04 编排）使用**，不对外。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import InternalAuthGuard
from app.core.logging import get_logger
from app.core.response import ok
from app.schemas.rag import RagRetrieveRequest, RagRetrieveResponse
from app.services.rag.service import get_rag_service

logger = get_logger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"], dependencies=[InternalAuthGuard])


@router.post(
    "/retrieve",
    summary="混合检索（六步流程）",
    response_model=None,
)
def retrieve(
    payload: RagRetrieveRequest,
    with_context: Annotated[
        bool,
        Query(description="是否同时返回送模型的上下文片段（内部编排用）"),
    ] = False,
) -> dict:
    """执行六步检索。

    **阈值分流保证**：`top_score < 0.55` 时 `decision` 必为
    `NO_EVIDENCE` 且 `hits` 为空 —— 调用方拿到空结果就必须走兜底或转人工，
    绝不允许"用低分片段凑一个答案"。

    Args:
        payload: 检索请求（query / 各路 top_k / 过滤条件）。
        with_context: 为 True 时额外返回 `answer_context`。

    Returns:
        统一响应包装，`data` 为检索结果。
    """
    service = get_rag_service()
    response: RagRetrieveResponse = service.retrieve(payload)

    data = response.model_dump(mode="json")
    if with_context:
        # 复用同一次检索的响应，避免 with_context 再跑一遍六步流程
        data["answer_context"] = service.answer_context_from(response)

    logger.info(
        "rag_http_retrieve decision=%s top_score=%.4f hits=%d latency=%dms",
        response.decision.value,
        response.top_score,
        len(response.hits),
        response.latency_ms,
    )
    return ok(data)


@router.post(
    "/cache/clear",
    summary="清空检索缓存",
    response_model=None,
)
def clear_cache() -> dict:
    """清空检索缓存（知识更新/回滚后由 BE-06 调用）。

    存在的理由：知识更新后若不主动清缓存，最长要等一个 TTL
    （120s）才能查到新内容，这与契约「知识更新 5 分钟内生效」虽然
    兼容，但运维演练时需要立即生效的能力。

    Returns:
        统一响应包装，`data.deleted` 为删除的键数。
    """
    deleted = get_rag_service().clear_cache()
    logger.info("rag_cache_cleared deleted=%d", deleted)
    return ok({"deleted": deleted})


__all__ = ["router"]
