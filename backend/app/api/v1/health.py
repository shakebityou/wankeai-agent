"""健康检查与指标路由（运维探针，不带鉴权）。

| 端点 | 用途 | 说明 |
| --- | --- | --- |
| `GET /health` | 存活探针 | 恒 200，只证明进程活着（compose healthcheck 用） |
| `GET /health/live` | 存活探针（K8s 口径预留） | 同 `/health` |
| `GET /health/ready` | 就绪探针 | 关键依赖（缓存）不可用则 503 |
| `GET /health/deps` | 依赖明细 | 逐个探测适配器，返回 `HealthReport` |
| `GET /metrics` | Prometheus 抓取 | 文本格式 |

**探测策略**：`/health/deps` 逐个 `await adapter.health()` 并计时，
单个依赖探测失败不影响其它依赖（用 `asyncio.gather(return_exceptions=True)`），
因为排障时最需要的就是「哪些坏了、哪些还好」的完整视图。

关键依赖与非关键依赖的区分见 `_CRITICAL`：缓存不可用 → 整体 DOWN；
向量库 / 稀疏索引 / 重排 / 嵌入不可用 → DEGRADED（RAG 降级但对话可用）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Response

from app.adapters.cache import get_cache
from app.adapters.embedding import get_embedder
from app.adapters.media_store import get_media_store
from app.adapters.reranker import get_reranker
from app.adapters.sparse_index import get_sparse_index
from app.adapters.vector_store import get_vector_store
from app.core.config import get_settings
from app.core.enums import HealthStatus
from app.core.logging import get_logger
from app.core.metrics import render_metrics, sync_circuit_breakers
from app.core.ratelimit import all_breakers_snapshot
from app.core.response import ok
from app.schemas.common import DependencyHealth, HealthReport

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

#: 关键依赖：任一不可用则整体 DOWN（对话链路直接不可用）
_CRITICAL: frozenset[str] = frozenset({"cache"})

_STARTED_AT = time.monotonic()


def _uptime() -> float:
    """进程已运行秒数。"""
    return round(time.monotonic() - _STARTED_AT, 3)


async def _probe(name: str, factory: Any) -> DependencyHealth:
    """探测单个依赖，**永不抛异常**。

    Args:
        name: 依赖名（同时作为 `_CRITICAL` 的判定键）。
        factory: 返回适配器实例的可调用对象（惰性调用，避免未配置就报错）。

    Returns:
        `DependencyHealth`；探测异常时 `status=DOWN` 并把异常类型写入 `detail`
        （**只写类型名，不写消息**——异常消息可能夹带连接串与凭据）。
    """
    started = time.perf_counter()
    impl: str | None = None
    try:
        adapter = factory()
        impl = type(adapter).__name__
        healthy = await asyncio.wait_for(adapter.health(), timeout=3.0)
        status = HealthStatus.UP if healthy else HealthStatus.DOWN
        detail = None if healthy else "健康检查返回 False"
    except TimeoutError:
        status = HealthStatus.DOWN
        detail = "探测超时（3s）"
    except Exception as exc:  # noqa: BLE001 - 探针不抛异常
        status = HealthStatus.DOWN
        detail = f"探测异常：{type(exc).__name__}"
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return DependencyHealth(
        name=name, status=status, latency_ms=latency_ms, detail=detail, impl=impl
    )


@router.get("/health", summary="存活探针")
async def health() -> dict[str, Any]:
    """存活探针：恒 200，只证明进程活着。

    Returns:
        统一响应壳（契约 C 2.5.5）。
    """
    settings = get_settings()
    return ok(
        {
            "status": HealthStatus.UP.value,
            "version": settings.app_version,
            "env": settings.env,
            "uptime_seconds": _uptime(),
        }
    )


@router.get("/health/live", summary="存活探针（K8s 口径）")
async def health_live() -> dict[str, Any]:
    """存活探针别名（`/health`）。"""
    return await health()


@router.get("/health/ready", summary="就绪探针")
async def health_ready(response: Response) -> dict[str, Any]:
    """就绪探针：关键依赖不可用返回 503。

    与 `/health/deps` 的区别：本端点只探关键依赖，轻量，供负载均衡摘除节点用。

    Args:
        response: FastAPI 响应对象（用于改状态码）。

    Returns:
        统一响应壳（契约 C 2.5.5）。
    """
    report = await _build_report(deep=False)
    if report.status == HealthStatus.DOWN:
        response.status_code = 503
    return ok(report.model_dump(mode="json"))


@router.get("/health/deps", summary="依赖明细")
async def health_deps(response: Response) -> dict[str, Any]:
    """依赖明细：逐个探测全部适配器。

    任一**关键**依赖 DOWN → 整体 DOWN（HTTP 503）；
    仅非关键依赖 DOWN → DEGRADED（HTTP 200，降级可用）。

    Args:
        response: FastAPI 响应对象（用于改状态码）。

    Returns:
        统一响应壳（契约 C 2.5.5）。
    """
    report = await _build_report(deep=True)
    if report.status == HealthStatus.DOWN:
        response.status_code = 503
    return ok(report.model_dump(mode="json"))


async def _build_report(*, deep: bool) -> HealthReport:
    """构造健康报告。

    Args:
        deep: True 探测全部依赖；False 只探关键依赖。
    """
    settings = get_settings()
    probes: list[tuple[str, Any]] = [
        ("cache", get_cache),
        ("vector_store", get_vector_store),
        ("sparse_index", get_sparse_index),
        ("embedder", get_embedder),
        ("reranker", get_reranker),
        ("media_store", get_media_store),
    ]
    if not deep:
        probes = [p for p in probes if p[0] in _CRITICAL]

    results = await asyncio.gather(
        *(_probe(name, factory) for name, factory in probes),
        return_exceptions=True,
    )
    dependencies: list[DependencyHealth] = []
    for (name, _), item in zip(probes, results, strict=True):
        if isinstance(item, DependencyHealth):
            dependencies.append(item)
        else:  # pragma: no cover - _probe 已兜底，理论不可达
            dependencies.append(
                DependencyHealth(
                    name=name, status=HealthStatus.DOWN, detail="探测结果异常"
                )
            )

    breakers = all_breakers_snapshot()
    sync_circuit_breakers(breakers)

    down = [d.name for d in dependencies if d.status == HealthStatus.DOWN]
    critical_down = [n for n in down if n in _CRITICAL]
    if critical_down:
        status = HealthStatus.DOWN
    elif down:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.UP

    return HealthReport(
        status=status,
        version=settings.app_version,
        env=settings.env,
        uptime_seconds=_uptime(),
        dependencies=dependencies,
        circuit_breakers=breakers,
    )


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus 文本格式指标。"""
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


__all__ = ["router"]
