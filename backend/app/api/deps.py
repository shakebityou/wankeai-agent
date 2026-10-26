"""FastAPI 依赖（`Depends()` 可注入项）。

**设计原则**：依赖只做「取单例 + 校验」，不承载业务逻辑。业务逻辑在
`app.services.*`，路由层薄到可以直接读。

所有依赖都从 `app.adapters` 的工厂取，因此单测里用
`set_cache(...)` / `set_llm_provider(...)` 就能整条链路替换，无需打补丁。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Request

from app.adapters.cache import get_cache
from app.adapters.embedding import get_embedder
from app.adapters.llm_provider import get_llm_provider
from app.adapters.media_store import get_media_store
from app.adapters.ports import (
    CachePort,
    EmbedderPort,
    LlmProviderPort,
    MediaStorePort,
    RerankerPort,
    SparseIndexPort,
    VectorStorePort,
)
from app.adapters.reranker import get_reranker
from app.adapters.sparse_index import get_sparse_index
from app.adapters.vector_store import get_vector_store
from app.compliance.outbound_gate import OutboundGate, get_outbound_gate
from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, ValidationError
from app.core.response import PageParams
from app.core.trace import get_buyer_id, get_session_id, get_trace_id

# ---------------------------------------------------------------------------
# 配置与基础设施
# ---------------------------------------------------------------------------


def settings_dep() -> Settings:
    """当前配置（`WK_*` 环境变量 + `.env`）。"""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def cache_dep() -> CachePort:
    """缓存（生产 Redis 7 / 本地 InMemoryCache）。"""
    return get_cache()


CacheDep = Annotated[CachePort, Depends(cache_dep)]


def vector_store_dep() -> VectorStorePort:
    """向量库（生产 Milvus 2.4 / 本地 NumpyVectorStore）。"""
    return get_vector_store()


VectorStoreDep = Annotated[VectorStorePort, Depends(vector_store_dep)]


def sparse_index_dep() -> SparseIndexPort:
    """稀疏索引（生产 ES 8 BM25 / 本地 Bm25SparseIndex）。"""
    return get_sparse_index()


SparseIndexDep = Annotated[SparseIndexPort, Depends(sparse_index_dep)]


def embedder_dep() -> EmbedderPort:
    """向量化（1024 维）。"""
    return get_embedder()


EmbedderDep = Annotated[EmbedderPort, Depends(embedder_dep)]


def reranker_dep() -> RerankerPort:
    """Cross-Encoder 重排。"""
    return get_reranker()


RerankerDep = Annotated[RerankerPort, Depends(reranker_dep)]


def media_store_dep() -> MediaStorePort:
    """对象存储（生产 OSS / 本地 LocalMediaStore）。"""
    return get_media_store()


MediaStoreDep = Annotated[MediaStorePort, Depends(media_store_dep)]


def llm_provider_dep() -> LlmProviderPort:
    """主 LLM（**出口域名受 `llm_domain_allowlist` 约束，禁止境外模型**）。"""
    return get_llm_provider()


LlmProviderDep = Annotated[LlmProviderPort, Depends(llm_provider_dep)]


def outbound_gate_dep() -> OutboundGate:
    """出站合规闸（HC-1）。"""
    return get_outbound_gate()


OutboundGateDep = Annotated[OutboundGate, Depends(outbound_gate_dep)]


# ---------------------------------------------------------------------------
# 请求上下文
# ---------------------------------------------------------------------------


def trace_id_dep() -> str:
    """当前 trace_id（由 `TraceMiddleware` 写入 ContextVar）。"""
    return get_trace_id()


TraceIdDep = Annotated[str, Depends(trace_id_dep)]


def session_id_dep() -> str | None:
    """当前会话 ID（业务层用 `set_session_id()` 绑定后可见）。"""
    return get_session_id()


SessionIdDep = Annotated[str | None, Depends(session_id_dep)]


def buyer_id_dep() -> str | None:
    """当前买家 ID（业务层用 `set_buyer_id()` 绑定后可见）。"""
    return get_buyer_id()


BuyerIdDep = Annotated[str | None, Depends(buyer_id_dep)]


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


def page_params_dep(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=50, description="每页条数，最大 50（契约 C 2.5.5）")
    ] = 20,
) -> PageParams:
    """分页参数（`page` 默认 1、`page_size` 默认 20 最大 50）。"""
    return PageParams(page=page, page_size=page_size)


PageParamsDep = Annotated[PageParams, Depends(page_params_dep)]


# ---------------------------------------------------------------------------
# 内部服务鉴权（契约 B 2.4）
# ---------------------------------------------------------------------------

#: 内部服务调用头（任务书 2.4 节：内部接口不对外暴露，走服务间 Token）
INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def verify_internal_token(
    request: Request,
    x_internal_token: Annotated[str | None, Header(alias=INTERNAL_TOKEN_HEADER)] = None,
) -> None:
    """校验内部服务 Token（契约 B 的 `/internal/v1/*` 全部依赖本函数）。

    本地环境（`WK_ENV=local`）且未配置 Token 时**放行**，便于单测与本地联调；
    其余环境缺失或错误一律 403，不泄露「Token 配没配」这一信息。

    Raises:
        ForbiddenError: Token 缺失或不匹配（非本地环境）。
    """
    settings = get_settings()
    expected = settings.internal_token.get_secret_value()

    if not expected:
        if settings.env == "local":
            return
        raise ForbiddenError("内部服务未配置访问凭证，已拒绝调用")

    if not x_internal_token or x_internal_token != expected:
        raise ForbiddenError("内部服务凭证无效")


InternalAuthDep = Annotated[None, Depends(verify_internal_token)]

#: 路由级内部鉴权依赖（供 `APIRouter(dependencies=[...])` 使用）。
#:
#: **不要**在 `APIRouter(dependencies=[...])` 里直接写 `InternalAuthDep`：
#: 那是 `Annotated[None, Depends(...)]`，属于**参数注解**形态，FastAPI 只认
#: 参数位。塞进 router 的 dependencies 列表时，它会被当作一个 `Depends`
#: 对象去取 `.dependency` 属性，于是 import 期就抛
#: `AttributeError: type object 'NoneType' has no attribute 'dependency'`，
#: 表现为整个 app 起不来（路由是模块加载时注册的）。
#: 路由级必须用下面这个真正的 `Depends(...)` 实例。
InternalAuthGuard = Depends(verify_internal_token)


# ---------------------------------------------------------------------------
# 场景注入（联调与降级演练，任务书 5.1 节）
# ---------------------------------------------------------------------------

#: 允许的 Mock 场景（任务书 5.1：错误注入按请求头切换）
MOCK_SCENARIOS: frozenset[str] = frozenset(
    {
        "normal",
        "error_40401",
        "error_40001",
        "error_40002",
        "error_42901",
        "error_50301",
        "error_50401",
        "error_40901",
        "error_10005",
        "error_40101",
        "error_40102",
        "no_evidence",
        "slow_order",
        "slow_llm",
    }
)


def mock_scenario_dep(
    x_mock_scenario: Annotated[
        str | None, Header(alias="X-Mock-Scenario")
    ] = None,
) -> str:
    """读取 `X-Mock-Scenario` 请求头（**仅本地 / 测试环境生效**）。

    生产环境一律忽略该头，防止外部用请求头操纵降级行为。

    Raises:
        ValidationError: 非本地环境却携带该头，或场景名不在白名单。
    """
    settings = get_settings()
    if not x_mock_scenario:
        return "normal"

    if settings.env not in ("local", "dev", "test"):
        raise ValidationError("生产环境不接受 X-Mock-Scenario 注入")

    scenario = x_mock_scenario.strip().lower()
    if scenario not in MOCK_SCENARIOS:
        raise ValidationError(
            f"未知的 Mock 场景 {scenario!r}，可选：{sorted(MOCK_SCENARIOS)}"
        )
    return scenario


MockScenarioDep = Annotated[str, Depends(mock_scenario_dep)]


__all__ = [
    "BuyerIdDep",
    "CacheDep",
    "EmbedderDep",
    "InternalAuthDep",
    "InternalAuthGuard",
    "LlmProviderDep",
    "MOCK_SCENARIOS",
    "MediaStoreDep",
    "MockScenarioDep",
    "OutboundGateDep",
    "PageParamsDep",
    "RerankerDep",
    "SessionIdDep",
    "SettingsDep",
    "SparseIndexDep",
    "TraceIdDep",
    "VectorStoreDep",
    "buyer_id_dep",
    "cache_dep",
    "embedder_dep",
    "llm_provider_dep",
    "media_store_dep",
    "mock_scenario_dep",
    "outbound_gate_dep",
    "page_params_dep",
    "reranker_dep",
    "session_id_dep",
    "settings_dep",
    "sparse_index_dep",
    "trace_id_dep",
    "vector_store_dep",
    "verify_internal_token",
]
