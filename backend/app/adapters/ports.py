"""外部依赖端口（Port）定义。

架构约定：业务层只依赖本包的**协议（Protocol）**，不直接 import 具体驱动。
这样做的目的有两个：

1. **可测试**：单测无需真实 Redis / Milvus / ES / PostgreSQL。
2. **可切换**：生产用任务书 3.1 节锁定的组件；本地无 Docker 时用等价实现，
   行为契约一致（同一套方法签名与语义）。

端口清单：

| 端口 | 生产实现 | 本地等价实现 | 对应任务 |
| --- | --- | --- | --- |
| `CachePort` | Redis 7 | 进程内字典 + TTL 线程 | BE-03 BE-18 |
| `VectorStorePort` | Milvus 2.4 | numpy 余弦相似度索引 | BE-05 BE-06 |
| `SparseIndexPort` | Elasticsearch 8 | jieba 分词 + BM25 | BE-05 BE-06 |
| `RepositoryPort` | PostgreSQL 14 | SQLite（同 SQLAlchemy 模型） | BE-03 BE-06 BE-14 |
| `MediaStorePort` | 阿里云 OSS | 本地文件系统 | BE-10 BE-11 |
| `LlmProviderPort` | 国产大模型 API | 确定性抽取式生成器 | BE-16 |
| `AsrPort` | 阿里云 / 讯飞语音识别 | 本地占位实现 + 可注入替身 | BE-10 |
| `OcrPort` | 阿里云 OCR / PaddleOCR | 本地模板匹配 + 可注入替身 | BE-12 |
| `ImageClassifierPort` | 国产 VLM（多模态） | 本地特征规则分类 | BE-11 |
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CachePort(Protocol):
    """键值缓存端口。

    用于四类场景（任务书 3.1 节 Redis 用途）：
        ① 会话状态（TTL 30 分钟）
        ② Nonce 去重（TTL 300s）
        ③ 幂等键（msg_id TTL 24h）
        ④ 限流计数（令牌桶）
    """

    async def get(self, key: str) -> str | None:
        """读取字符串值，不存在返回 None。"""
        ...

    async def set(
        self, key: str, value: str, *, ttl: int | None = None
    ) -> None:
        """写入字符串值。`ttl` 为秒，None 表示不过期。"""
        ...

    async def delete(self, key: str) -> None:
        """删除键。"""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀的所有键，返回删除条数。

        知识更新后清检索缓存用。**不要用 `KEYS` 实现**（O(N) 阻塞），
        生产 Redis 上必须走 `SCAN`。
        """
        ...

    async def exists(self, key: str) -> bool:
        """键是否存在。"""
        ...

    async def setnx(self, key: str, value: str, *, ttl: int | None = None) -> bool:
        """仅当键不存在时写入（原子）。

        Returns:
            True 表示本次写入成功（首次），False 表示键已存在（重复）。
        """
        ...

    async def incrby(self, key: str, amount: int = 1, *, ttl: int | None = None) -> int:
        """原子自增并返回新值。用于限流计数。"""
        ...

    async def expire(self, key: str, ttl: int) -> None:
        """设置过期时间（秒）。"""
        ...

    async def get_json(self, key: str) -> Any | None:
        """读取并反序列化 JSON 值。"""
        ...

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """序列化为 JSON 后写入。"""
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


@runtime_checkable
class VectorStorePort(Protocol):
    """向量库端口（1024 维，支持元数据过滤）。

    硬性约束（BE-05 DoD）：**库存 / 价格字段不得存在于向量库**。
    """

    async def upsert(
        self, chunks: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]
    ) -> int:
        """批量写入 / 覆盖 chunk 与向量，返回成功条数。"""
        ...

    async def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索。

        Args:
            vector: 查询向量，维度必须等于 `embedding_dim`。
            top_k: 召回条数。
            filter_expr: 元数据过滤表达式（Milvus 语法）。

        Returns:
            形如 `[{"chunk_id": ..., "score": float, "metadata": {...}}]`，
            按 score 降序。
        """
        ...

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        ...

    async def count(self) -> int:
        """集合内 chunk 总数。"""
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


@runtime_checkable
class SparseIndexPort(Protocol):
    """稀疏检索端口（BM25 一路召回）。"""

    async def upsert(self, chunks: Sequence[dict[str, Any]]) -> int:
        """批量写入 / 覆盖 chunk 到稀疏索引。"""
        ...

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 检索，返回按 score 降序的结果。"""
        ...

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 chunk_id 删除。"""
        ...

    async def count(self) -> int:
        """索引内 chunk 总数。"""
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """Cross-Encoder 重排端口（RAG 六步第 5 步）。"""

    async def rerank(
        self, query: str, candidates: Sequence[dict[str, Any]], *, top_n: int
    ) -> list[dict[str, Any]]:
        """对候选集重排，返回 Top-N（含重排后的 `score`）。"""
        ...


@runtime_checkable
class EmbedderPort(Protocol):
    """Embedding 端口，输出维度锁定 1024（项目文档 4.4 节硬性要求）。"""

    @property
    def dim(self) -> int:
        """向量维度，必须为 1024。"""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化。"""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化（部分模型对 query 与 document 使用不同前缀）。"""
        ...


@runtime_checkable
class MediaStorePort(Protocol):
    """对象存储端口（语音原始文件 / 图片 / 导出文件）。

    合规约束：语音原始文件**生命周期 7 天自动删除**（BE-10 DoD）。
    """

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """写入对象，返回可访问 URL。"""
        ...

    async def get(self, key: str) -> bytes:
        """读取对象内容。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象。"""
        ...

    async def set_lifecycle_days(self, key_prefix: str, days: int) -> None:
        """设置前缀下对象生命周期（天）。"""
        ...


@runtime_checkable
class LlmProviderPort(Protocol):
    """LLM Provider 端口。

    网络层强制：**禁止调用境外模型 API**（BE-16 DoD），
    出口域名必须命中 `Settings.llm_domain_allowlist`。
    """

    @property
    def name(self) -> str:
        """Provider 名称，用于日志与成本统计。"""
        ...

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> "LlmResult":
        """同步补全（非流式）。"""
        ...

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> "Iterable[str]":
        """流式补全，逐段产出文本增量（SSE `delta` 事件用）。"""
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


class LlmResult:
    """LLM 补全结果（放这里避免循环 import）。

    Attributes:
        text: 生成的文本。
        prompt_tokens: 输入 token 数（成本统计）。
        completion_tokens: 输出 token 数。
        model: 实际使用的模型名。
        provider: 实际使用的 Provider 名。
        cached: 是否命中相同问题缓存。
    """

    __slots__ = (
        "text",
        "prompt_tokens",
        "completion_tokens",
        "model",
        "provider",
        "cached",
    )

    def __init__(
        self,
        text: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "",
        provider: str = "",
        cached: bool = False,
    ) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model
        self.provider = provider
        self.cached = cached

    @property
    def total_tokens(self) -> int:
        """总 token 数。"""
        return self.prompt_tokens + self.completion_tokens

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return (
            f"LlmResult(provider={self.provider!r}, model={self.model!r}, "
            f"tokens={self.total_tokens}, cached={self.cached}, "
            f"text={self.text[:30]!r})"
        )


@runtime_checkable
class AsrPort(Protocol):
    """语音识别端口（BE-10 第 ③ 步）。

    **网络层强制**：只允许调用境内服务（与 BE-16 同一条合规约束）。
    """

    @property
    def name(self) -> str:
        """实现名，用于日志与降级标识。"""
        ...

    async def transcribe(
        self,
        wav: bytes,
        *,
        hotwords: Sequence[str] = (),
        sample_rate: int = 16000,
    ) -> "AsrResult":
        """把 16kHz/16bit/单声道 wav 转成文本。

        Args:
            wav: 转码后的 wav 字节（**必须是 16k/16bit/mono**，由 BE-10 保证）。
            hotwords: 热词表（商品名、SKU、品牌词），提升专有名词识别率。
            sample_rate: 采样率，仅用于校验。

        Returns:
            `AsrResult`。识别不出内容时 `text=""`、`confidence=0.0`，
            **不抛异常**——异常由上层决定话术。
        """
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


class AsrResult:
    """ASR 结果。

    Attributes:
        text: 识别文本，可能为空串。
        confidence: 逐句置信度的加权均值，0-1。
        duration_ms: 音频时长。
        degraded: 降级标识（如 `ASR_NO_ENGINE` / `ASR_LOW_CONFIDENCE`）。
    """

    __slots__ = ("text", "confidence", "duration_ms", "degraded")

    def __init__(
        self,
        text: str,
        *,
        confidence: float = 0.0,
        duration_ms: int = 0,
        degraded: Sequence[str] = (),
    ) -> None:
        self.text = text
        self.confidence = confidence
        self.duration_ms = duration_ms
        self.degraded = list(degraded)

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return (
            f"AsrResult(text={self.text[:30]!r}, confidence={self.confidence:.2f}, "
            f"duration_ms={self.duration_ms}, degraded={self.degraded})"
        )


@runtime_checkable
class OcrPort(Protocol):
    """OCR 端口（BE-12 第 ① 步）。"""

    @property
    def name(self) -> str:
        """实现名。"""
        ...

    async def recognize(self, image: bytes, *, content_type: str = "image/png") -> "OcrResult":
        """识别图片中的文字。

        Returns:
            `OcrResult`。识别失败返回 `text=""`、`confidence=0.0`，**不抛异常**。
        """
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


class OcrResult:
    """OCR 结果。

    Attributes:
        text: 按阅读顺序拼接的全文（含换行）。
        confidence: 整体置信度 0-1。
        blocks: 文本块列表，每项 `{"text": str, "confidence": float, "box": [x,y,w,h]}`。
        degraded: 降级标识（如 `OCR_NO_ENGINE` / `OCR_LOW_CONFIDENCE`）。
    """

    __slots__ = ("text", "confidence", "blocks", "degraded")

    def __init__(
        self,
        text: str,
        *,
        confidence: float = 0.0,
        blocks: Sequence[dict[str, Any]] = (),
        degraded: Sequence[str] = (),
    ) -> None:
        self.text = text
        self.confidence = confidence
        self.blocks = list(blocks)
        self.degraded = list(degraded)

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return (
            f"OcrResult(text={self.text[:40]!r}, confidence={self.confidence:.2f}, "
            f"blocks={len(self.blocks)}, degraded={self.degraded})"
        )


@runtime_checkable
class ImageClassifierPort(Protocol):
    """图片五路分流端口（BE-11 第 ② 步，对应 `AI-07`）。"""

    @property
    def name(self) -> str:
        """实现名。"""
        ...

    async def classify(
        self,
        image: bytes,
        *,
        ocr_text: str = "",
        content_type: str = "image/png",
    ) -> "ImageClassifyResult":
        """判定图片属于五路中的哪一路。

        Args:
            image: 已归一化（格式/尺寸已校验）的图片字节。
            ocr_text: 预跑 OCR 得到的文本，分类器可直接利用（订单号/尺码词）。
            content_type: MIME 类型。

        Returns:
            `ImageClassifyResult`。**必须给出一个路由**，不得返回 None——
            编排层没有「图片类型未知」这个分支，判不准就归 `MEME_IRRELEVANT`
            走礼貌引导，而不是让整轮对话失败。
        """
        ...

    async def health(self) -> bool:
        """健康检查。"""
        ...


class ImageClassifyResult:
    """图片分类结果。

    Attributes:
        route: 五路之一的中文值（见 `ImageRoute`）。
        confidence: 分类置信度 0-1。
        description: 图片内容的一句话描述（VLM 实现会填，规则实现可为空）。
        labels: Top-N 候选 `[{"route": str, "score": float}]`。
        degraded: 降级标识。
    """

    __slots__ = ("route", "confidence", "description", "labels", "degraded")

    def __init__(
        self,
        route: str,
        *,
        confidence: float = 0.0,
        description: str = "",
        labels: Sequence[dict[str, Any]] = (),
        degraded: Sequence[str] = (),
    ) -> None:
        self.route = route
        self.confidence = confidence
        self.description = description
        self.labels = list(labels)
        self.degraded = list(degraded)

    def __repr__(self) -> str:  # pragma: no cover - 展示用
        return (
            f"ImageClassifyResult(route={self.route!r}, "
            f"confidence={self.confidence:.2f}, degraded={self.degraded})"
        )


__all__ = [
    "AsrPort",
    "AsrResult",
    "CachePort",
    "EmbedderPort",
    "ImageClassifyResult",
    "ImageClassifierPort",
    "LlmProviderPort",
    "LlmResult",
    "MediaStorePort",
    "OcrPort",
    "OcrResult",
    "RerankerPort",
    "SparseIndexPort",
    "VectorStorePort",
]
