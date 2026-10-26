"""外部依赖适配器（生产实现 + 本地等价实现）。

架构原则：**业务层只依赖 `app.adapters.ports` 中的 Protocol**，不 import 具体驱动。
本包按任务书 3.1 节锁定的技术栈提供生产实现，同时提供行为等价的本地实现，
使得在没有 Docker 的开发机上也能跑通端到端链路。

| 模块 | 生产实现 | 本地等价实现 |
| --- | --- | --- |
| `cache` | `RedisCache`（Redis 7 主从） | `InMemoryCache` |
| `vector_store` | `MilvusVectorStore`（Milvus 2.4） | `NumpyVectorStore` |
| `sparse_index` | `EsSparseIndex`（Elasticsearch 8 BM25） | `Bm25SparseIndex`（jieba） |
| `repository` | PostgreSQL 14（SQLAlchemy） | SQLite（同模型） |
| `media_store` | `OssMediaStore`（阿里云 OSS） | `LocalMediaStore` |
| `embedding` | `ApiEmbedder`（国产 API，1024 维） | `LocalHashEmbedder` |
| `reranker` | `ApiReranker`（Cross-Encoder） | `LexicalReranker` |
| `llm_provider` | `OpenAiCompatProvider`（主+备双通道） | `TemplateProvider`（抽取式） |

**生产/本地切换**由配置与环境变量控制，业务代码零改动：

    WK_ENV=local              → 全部走本地等价实现（默认，CI 与开发机）
    WK_ENV=prod               → 全部走生产实现（Milvus/ES/Redis/PG/OSS）
    WK_VECTOR_STORE=local     → 单项强制本地（降级演练）
"""

from app.adapters.cache import InMemoryCache, RedisCache, get_cache, set_cache
from app.adapters.embedding import (
    EMBEDDING_DIM,
    ApiEmbedder,
    LocalHashEmbedder,
    get_embedder,
    set_embedder,
)
from app.adapters.llm_provider import (
    OpenAiCompatProvider,
    TemplateProvider,
    build_provider,
    get_llm_fallback_provider,
    get_llm_provider,
    set_llm_fallback_provider,
    set_llm_provider,
)
from app.adapters.media_store import (
    LocalMediaStore,
    OssMediaStore,
    get_media_store,
    set_media_store,
)
from app.adapters.ports import (
    CachePort,
    EmbedderPort,
    LlmProviderPort,
    LlmResult,
    MediaStorePort,
    RerankerPort,
    SparseIndexPort,
    VectorStorePort,
)
from app.adapters.reranker import ApiReranker, LexicalReranker, get_reranker, set_reranker
from app.adapters.sparse_index import (
    Bm25SparseIndex,
    EsSparseIndex,
    get_sparse_index,
    set_sparse_index,
)
from app.adapters.vector_store import (
    FORBIDDEN_FIELDS,
    ForbiddenFieldError,
    MilvusVectorStore,
    NumpyVectorStore,
    assert_no_forbidden_fields,
    get_vector_store,
    set_vector_store,
)

__all__ = [
    "EMBEDDING_DIM",
    "FORBIDDEN_FIELDS",
    "ApiEmbedder",
    "ApiReranker",
    "Bm25SparseIndex",
    "CachePort",
    "EmbedderPort",
    "EsSparseIndex",
    "ForbiddenFieldError",
    "InMemoryCache",
    "LexicalReranker",
    "LlmProviderPort",
    "LlmResult",
    "LocalHashEmbedder",
    "LocalMediaStore",
    "MediaStorePort",
    "MilvusVectorStore",
    "NumpyVectorStore",
    "OpenAiCompatProvider",
    "OssMediaStore",
    "RedisCache",
    "RerankerPort",
    "SparseIndexPort",
    "TemplateProvider",
    "VectorStorePort",
    "assert_no_forbidden_fields",
    "build_provider",
    "get_cache",
    "get_embedder",
    "get_llm_fallback_provider",
    "get_llm_provider",
    "get_media_store",
    "get_reranker",
    "get_sparse_index",
    "get_vector_store",
    "set_cache",
    "set_embedder",
    "set_llm_fallback_provider",
    "set_llm_provider",
    "set_media_store",
    "set_reranker",
    "set_sparse_index",
    "set_vector_store",
]
