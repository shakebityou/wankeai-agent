"""FastAPI 应用入口（模块化单体，任务书 3.1 / 3.2）。

启动命令：

    # 本地开发
    WK_ENV=local uvicorn app.main:app --reload --port 8000

    # 生产（Dockerfile 的 CMD 走这条）
    gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker \
        --workers $WEB_CONCURRENCY --bind 0.0.0.0:8000

分层与挂载（详见 `app/__init__.py` 的 L1–L7 映射）：

    /health, /health/*, /metrics   ← 运维探针（无鉴权，不入 OpenAPI 的 /internal）
    /internal/v1/*                 ← 契约 B 内部服务（X-Internal-Token）
    /bff/v1/*                      ← 契约 C 前端接口（登录态）
    /api/v1/*                      ← 契约 A 商家回调入口（签名校验）

**中间件顺序**（`add_middleware` 是后进先出，即最后添加的在最外层）：
    TraceMiddleware（最外层，建立 ContextVar）
      → MetricsMiddleware
        → ComplianceMaskMiddleware（响应出口脱敏，BE-17 执行位置②）
          → AccessLogMiddleware
            → CORS（按需）
              → 路由

`ComplianceMaskMiddleware` 必须包在 `AccessLogMiddleware` **外面**：访问日志
记的是状态码与耗时，不记响应体，所以顺序对它无影响；但脱敏要在**任何可能
读取响应体的环节之前**完成，把它放外层可以保证「响应体一旦离开路由就已经
是脱敏后的」，内层任何组件（含未来的调试中间件）都拿不到原文。

`lifespan` 里做三件事：日志初始化、启动信息打点、关闭时释放适配器。
**不在这里建连接池**——适配器是惰性单例，首次调用时自建，这样
`/health` 在依赖全挂的情况下也能回答，而不是应用起不来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.v1 import build_internal_router
from app.api.v1.health import router as health_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.metrics import set_app_info
from app.middleware.compliance import ComplianceMaskMiddleware
from app.middleware.trace import (
    AccessLogMiddleware,
    MetricsMiddleware,
    TraceMiddleware,
)

logger = get_logger("app.main")

#: 描述（OpenAPI 文档标题用）
DESCRIPTION = """玩可爱智能客服 Agent · 后端（模块化单体）。

- **契约 A** `/api/v1/*`：商家渠道接口（本项目为调用方 + 回调接收方）
- **契约 B** `/internal/v1/*`：项目组内部服务（前端不可访问）
- **契约 C** `/bff/v1/*`：前端坐席台与运营后台的唯一入口

硬约束：不输出任何外链（HC-1）；数据不出境、不用于训练（HC-2）；
库存/价格/物流单号永远走接口实时查询（HC-3）。
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期。

    启动：初始化日志、打应用信息指标、打印关键配置（**脱敏**）。
    关闭：释放适配器持有的资源。

    Args:
        app: FastAPI 实例。

    Yields:
        None（交给应用运行）。
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    set_app_info(settings.app_version, settings.env)

    logger.info(
        "应用启动",
        extra={
            "extra": {
                "version": settings.app_version,
                "env": settings.env,
                "docs_enabled": settings.docs_enabled,
                "compliance_gate_enabled": settings.compliance_gate_enabled,
                "merchant_base_url": settings.merchant_base_url,
                "effective_qps": settings.effective_qps,
            }
        },
    )

    try:
        yield
    finally:
        await _shutdown_adapters()
        logger.info("应用已停止", extra={"extra": {"version": settings.app_version}})


async def _shutdown_adapters() -> None:
    """关闭适配器（尽力而为，单个失败不影响其它）。"""
    from app.adapters.cache import get_cache  # noqa: PLC0415
    from app.adapters.media_store import get_media_store  # noqa: PLC0415

    for name, factory in (("cache", get_cache), ("media_store", get_media_store)):
        try:
            adapter = factory()
            close = getattr(adapter, "close", None) or getattr(adapter, "aclose", None)
            if close is None:
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001 - 关闭失败不阻断停机
            logger.warning(
                "适配器关闭失败：%s (%s)", name, type(exc).__name__
            )


def create_app() -> FastAPI:
    """应用工厂（`uvicorn --factory` 与测试均用本函数）。

    Returns:
        配置完毕的 `FastAPI` 实例。
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        # 生产由 Nginx 反代，路径前缀不参与路由匹配，保持默认根路径
        root_path="",
    )

    # --- 异常处理（统一响应格式，契约 C 2.5.5）---
    register_exception_handlers(app)

    # --- 中间件（后进先出：最后 add 的最外层）---
    # 1) CORS 按需（默认关闭：前端由 Nginx 同源反代 /bff）
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Trace-Id", "X-Request-Id", "Retry-After"],
        )
    # 2) 访问日志
    app.add_middleware(AccessLogMiddleware)
    # 3) 响应出口脱敏（BE-17 执行位置②：接口返回值零完整敏感字段）
    if settings.compliance_mask_responses:
        app.add_middleware(
            ComplianceMaskMiddleware,
            prefixes=tuple(settings.compliance_mask_prefixes),
            enabled=True,
        )
    # 4) 指标
    app.add_middleware(MetricsMiddleware)
    # 5) trace（最外层，必须最后 add）
    app.add_middleware(TraceMiddleware)

    # --- 路由 ---
    # 运维探针：挂根路径，不带 /internal 前缀
    app.include_router(health_router)
    # 契约 A 渠道回调（商家侧调用，路径 /api/v1/callback/*）
    from app.api.v1.callback import router as callback_router

    app.include_router(callback_router)
    # 契约 B 内部服务
    app.include_router(build_internal_router())
    # 契约 C BFF（前端唯一入口，路径 /bff/v1/*）
    from app.api.bff import build_bff_router

    app.include_router(build_bff_router())

    return app


#: ASGI 入口（`gunicorn app.main:app`）
app = create_app()


__all__ = ["app", "create_app", "lifespan"]
