"""Embedding 适配器（维度锁定 **1024**，项目文档 4.4 节硬性要求）。

| 实现 | 用途 | 说明 |
| --- | --- | --- |
| `LocalHashEmbedder` | 本地 / 单测 / 无外部依赖 | 确定性哈希投影（特征哈希 + 字符 n-gram），无需网络 |
| `ApiEmbedder` | 生产 | 调用国产大模型 Embedding API（如 DashScope `text-embedding-v3`，1024 维） |

**为什么本地实现必须存在**：任务书 3.1 节锁定「国产 Embedding（1024 维）」，
但开发机/CI 未必能连外网。本地实现保证 **RAG 六步链路可端到端跑通且可复现**
（同一文本恒得同一向量），切换到真实 API 只需改一个配置项。

⚠️ 向量库中**不得包含库存 / 价格字段**（BE-05 DoD），该约束由
`app.services.knowledge` 的切分与入库逻辑保证，与本模块无关。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any

from app.adapters.ports import EmbedderPort
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 目标维度（硬性，不可改）
EMBEDDING_DIM = 1024

_WS_RE = re.compile(r"\s+")
#: 中文标点与常见无意义符号（参与特征会引入噪声）。
#: 用 `re.escape` 拼装字符类，避免手写转义时踩到 `]` / `-` / `\` 的坑。
_PUNCT_CHARS = (
    "，。！？、；：（）【】《》"
    "“”‘’"
    "—…~·,.!?;:()[]{}\"'`|/\\-_*#$%^&+=<>@"
)
_PUNCT_RE = re.compile(f"[\\s{re.escape(_PUNCT_CHARS)}]+")


def normalize_text(text: str) -> str:
    """检索用文本归一化：全角转半角、去多余空白、统一小写。

    注意：**只用于向量化与分词**，不改变原始文本（原文仍入库）。
    """
    if not text:
        return ""
    # 全角字符 → 半角（ASCII 可见范围）
    chars = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return _WS_RE.sub(" ", "".join(chars)).strip().lower()


def char_ngrams(text: str, n: int) -> list[str]:
    """提取字符 n-gram（对中文与拼写变体友好）。"""
    clean = _PUNCT_RE.sub("", text)
    if len(clean) < n:
        return [clean] if clean else []
    return [clean[i : i + n] for i in range(len(clean) - n + 1)]


def tokenize(text: str) -> list[str]:
    """分词：优先 jieba，缺失时退化为字符 2-gram。

    任务书 3.1 节允许 jieba 用于本地 BM25 与关键词抽取。
    """
    clean = normalize_text(text)
    if not clean:
        return []
    try:
        import jieba  # 延迟导入：未安装时不影响向量化主流程

        words = [w.strip() for w in jieba.lcut(clean) if w.strip()]
    except Exception:  # noqa: BLE001 - 分词失败退化处理
        words = []
    words = [w for w in words if _PUNCT_RE.sub("", w)]
    if not words:
        words = char_ngrams(clean, 2)
    # 追加字符 2-gram，提升口语化查询的召回（如「小熊挂件」vs「小熊 挂件」）
    return words + char_ngrams(clean, 2)


def _hash_bucket(feature: str) -> tuple[int, float]:
    """把特征映射到 (桶下标, 符号)。

    用 blake2b 保证**跨进程、跨运行稳定**（Python 内置 hash 对 str 加了随机盐，
    不能用于持久化向量）。
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    bucket = value % EMBEDDING_DIM
    sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
    return bucket, sign


class LocalHashEmbedder:
    """本地确定性 Embedding（特征哈希 + 字符 n-gram）。

    算法（无监督、无需训练、无网络）：

        1. 文本归一化 → 分词（jieba）+ 字符 2/3-gram
        2. 每个特征经 blake2b 稳定哈希映射到 1024 维中的一维，带 ±1 符号
        3. 权重：词 1.0，字符 2-gram 0.7，字符 3-gram 0.4（越长越稀疏，降权）
        4. L2 归一化 → 余弦相似度即点积

    特性：
        * **确定性**：同一文本恒得同一向量，可持久化、可复现（CI 友好）。
        * **可解释**：相似度来自字面重叠，不会产生「幻觉式语义相似」。
        * **有界**：无外部依赖，毫秒级，适合单测与压测基线。

    局限（**必须写进运维手册**）：不具备真正的语义泛化能力。
    上线前应切到 `ApiEmbedder`，并重跑 300 条 RAG 问答集验证 Recall@5 ≥ 95%。
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    @property
    def name(self) -> str:
        """实现名，用于健康检查与日志。"""
        return "local-hash-1024"

    def _vectorize(self, text: str) -> list[float]:
        """把文本编码为 L2 归一化的 1024 维向量。"""
        vec = [0.0] * self._dim
        clean = normalize_text(text)
        if not clean:
            return vec

        features: list[tuple[str, float]] = []
        try:
            import jieba

            for word in jieba.lcut(clean):
                word = word.strip()
                if len(word) >= 2:
                    features.append((f"w:{word}", 1.0))
                elif word:
                    features.append((f"c:{word}", 0.5))
        except Exception:  # noqa: BLE001 - 无 jieba 时只走 n-gram
            pass

        for gram in char_ngrams(clean, 2):
            features.append((f"g2:{gram}", 0.7))
        for gram in char_ngrams(clean, 3):
            features.append((f"g3:{gram}", 0.4))

        if not features:
            for gram in char_ngrams(clean, 1):
                features.append((f"g1:{gram}", 1.0))

        for feature, weight in features:
            bucket, sign = _hash_bucket(feature)
            vec[bucket] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化（文档侧）。"""
        return [self._vectorize(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。"""
        return self._vectorize(text)

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        return True


class ApiEmbedder:
    """国产 Embedding API 实现（OpenAI 兼容 `/v1/embeddings` 协议）。

    默认对接 DashScope（`text-embedding-v3`，输出 1024 维），
    也兼容任何 OpenAI 兼容的境内服务。**禁止指向境外域名**（BE-16 DoD）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-v3",
        dim: int = EMBEDDING_DIM,
        timeout: float = 20.0,
        batch_size: int = 25,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._batch_size = batch_size

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    @property
    def name(self) -> str:
        """实现名。"""
        return f"api-{self._model}"

    def _endpoint(self) -> str:
        """拼接 embeddings 端点。"""
        if self._base_url.endswith("/embeddings"):
            return self._base_url
        if "/v1" in self._base_url:
            return f"{self._base_url}/embeddings"
        return f"{self._base_url}/v1/embeddings"

    def _assert_allowlisted(self) -> None:
        """校验出口域名在白名单内（**禁止境外模型 API**）。"""
        from urllib.parse import urlparse

        host = (urlparse(self._base_url).hostname or "").lower()
        allowlist = [d.lower() for d in get_settings().llm_domain_allowlist]
        if host and not any(host == d or host.endswith(f".{d}") for d in allowlist):
            raise PermissionError(
                f"Embedding 出口域名 {host!r} 不在白名单内，"
                "禁止调用境外模型 API（BE-16 DoD）"
            )

    async def _call(self, texts: Sequence[str]) -> list[list[float]]:
        """调用一次 API。"""
        import httpx

        self._assert_allowlisted()
        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._endpoint(), json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
        data = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
        return [list(map(float, item["embedding"])) for item in data]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化，自动按 batch_size 分批。"""
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            out.extend(await self._call(batch))
        return out

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。"""
        vectors = await self._call([text])
        return vectors[0] if vectors else [0.0] * self._dim

    async def health(self) -> bool:
        """探测一次极短文本。"""
        try:
            vec = await self.embed_query("健康检查")
            return len(vec) == self._dim
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("Embedding API 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_EMBEDDER: EmbedderPort | None = None


def get_embedder() -> EmbedderPort:
    """获取 Embedding 实现（单例）。

    策略：配置了 `WK_EMBEDDING_BASE_URL` + `WK_EMBEDDING_API_KEY` 时用 API 实现，
    否则用本地确定性实现。
    """
    global _EMBEDDER  # noqa: PLW0603
    if _EMBEDDER is None:
        settings = get_settings()
        base_url = getattr(settings, "embedding_base_url", "") or ""
        api_key = ""
        secret = getattr(settings, "embedding_api_key", None)
        if secret is not None:
            api_key = secret.get_secret_value()
        if base_url and api_key:
            logger.info("Embedding 实现：ApiEmbedder（%s）", base_url)
            _EMBEDDER = ApiEmbedder(base_url=base_url, api_key=api_key)
        else:
            logger.info("Embedding 实现：LocalHashEmbedder（1024 维确定性实现）")
            _EMBEDDER = LocalHashEmbedder()
    return _EMBEDDER


def set_embedder(embedder: EmbedderPort | None) -> None:
    """替换 Embedding 实现（测试用）。"""
    global _EMBEDDER  # noqa: PLW0603
    _EMBEDDER = embedder


__all__ = [
    "ApiEmbedder",
    "EMBEDDING_DIM",
    "LocalHashEmbedder",
    "char_ngrams",
    "get_embedder",
    "normalize_text",
    "set_embedder",
    "tokenize",
]
