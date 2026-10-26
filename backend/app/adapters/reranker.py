"""重排适配器（Cross-Encoder，RAG 六步流程第 5 步）。

**为什么必须有重排**：双路召回（向量 20 + BM25 20）经 RRF 融合后仍有 30~40 条候选，
直接取 Top-5 送 LLM 会把噪声一起送进去，导致「答非所问」。Cross-Encoder 对
(query, chunk) 逐对打分，把真正相关的顶上来，是 Recall@5 ≥ 95% 的关键一步。

| 实现 | 用途 |
| --- | --- |
| `ApiReranker` | 生产：国产 Cross-Encoder Rerank API（如 DashScope `gte-rerank`） |
| `LexicalReranker` | 本地 / 单测：字面重叠 + 关键词命中 + 权威度的确定性打分 |

⚠️ **打分口径统一**：两条路径都必须输出 **0~1 的归一化分数**，
因为 `decision` 阈值（0.72 / 0.55）是硬性的，分数口径不一致会导致分流决策漂移。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.adapters.embedding import char_ngrams, normalize_text, tokenize
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _clamp01(value: float) -> float:
    """裁剪到 [0, 1]。"""
    return max(0.0, min(1.0, value))


def _real_words(query: str) -> list[str]:
    """只取查询里的真实词（剔除跨词 2-gram 碎片，并去重保序）。

    `embedding.tokenize()` 把「jieba 词」与「字符 2-gram」拼在一起返回，
    后半段对 BM25 召回有用（抗分词差异），但对**覆盖率类打分**有害：
    `盲盒可以退换吗` 会产出 `盒可`/`以退`/`换吗` 这类跨词碎片，
    它们在任何 chunk 里都不存在，却占掉分母，分数被系统性腰斩。

    判据：跑一次纯 jieba 得到真实词集合，只保留 tokenize 输出中
    属于该集合的项。这样 n-gram 碎片被剔除，而 `优惠活动` 这类
    jieba 合成词被保留。

    Args:
        query: 原始查询。

    Returns:
        去重后的真实词列表（保持首次出现顺序，仅保留长度 ≥ 2 的词）。
    """
    clean = normalize_text(query)
    words = _jieba_only(clean)
    if not words:
        # jieba 不可用（极简环境）：退回长度 ≥ 2 的全部 token，至少不崩
        return list(dict.fromkeys(t for t in tokenize(query) if len(t) >= 2))
    kept = [t for t in tokenize(query) if len(t) >= 2 and t in words]
    return list(dict.fromkeys(kept))


def _jieba_only(clean: str) -> set[str]:
    """单独跑一次 jieba，返回真实词集合（不含 n-gram）。"""
    try:
        import jieba  # 延迟导入，与 embedding.tokenize 保持同一可选依赖

        return {w.strip() for w in jieba.lcut(clean) if w.strip()}
    except Exception:  # noqa: BLE001 - 分词不可用时视为无真实词
        return set()


class LexicalReranker:
    """确定性字面重排（本地 / 单测 / LLM 不可用时的降级路径）。

    打分公式（三项加权，输出 0~1）：

        score = 0.60 * 词覆盖率        # 查询词有多少比例在 chunk 中出现
              + 0.25 * 字符 2-gram 覆盖率  # 抗分词差异（口语化查询）
              + 0.15 * 权威度奖励        # authority=1（官方政策）优先

    设计取舍：
        * **不用余弦相似度**——向量分已在第 1 路召回使用，重排若再算一遍等于没重排。
        * **不做语义泛化**——本地路径刻意保持「字面可解释」，
          避免在离线环境给出虚假的高分，掩盖真实检索缺陷。
        * **权威度奖励上限 0.15**——保证「相关性为主、权威性为辅」，
          否则一条无关的官方政策会压过一条高度相关的 FAQ。
    """

    def __init__(self, *, title_boost: float = 0.08) -> None:
        self._title_boost = title_boost

    @property
    def name(self) -> str:
        """实现名。"""
        return "lexical-local"

    def _score_one(self, query: str, candidate: dict[str, Any]) -> float:
        """给单个候选打分。

        **分词去重是必须的**：`tokenize()` 同时产出结巴词与相邻字 2-gram
        （`盲盒可以退换吗` → `['盲盒','可以','退换','吗','盲盒','盒可','可以','以退','退换','换吗']`），
        其中 `盲盒`/`可以`/`退换` 各出现两次、`盒可`/`以退`/`换吗` 是跨词碎片。
        若直接 `hit / len(terms)`，分母里一半是重复项与碎片，覆盖率被系统性压低，
        所有「同一领域」的 chunk 都挤在 0.3~0.5 这个窄带里，而决策阈值是 0.72/0.55
        ——结果是**正确答案也永远到不了 DIRECT_ANSWER**。
        去重后分母才等于「查询词种类数」，覆盖率才是可解释的比例。

        Args:
            query: 已改写的查询。
            candidate: 候选字典（含 content / title / metadata）。

        Returns:
            `[0, 1]` 区间的相关性分数。
        """
        content = str(candidate.get("content", ""))
        metadata = candidate.get("metadata", {}) or {}
        title = str(candidate.get("title") or metadata.get("title") or "")
        haystack = normalize_text(f"{title} {content}")
        if not haystack:
            return 0.0

        query_norm = normalize_text(query)
        # ⚠️ tokenize() 返回「jieba 真实词 + 字符 2-gram」两段拼接，
        # 而后半段的跨词碎片（`盒可`/`以退`/`换吗`）几乎永不命中，
        # 混进覆盖率分母会把分数系统性压低。这里只取前缀段（真实词），
        # n-gram 由下面独立的 gram_cov 通道负责，两者互不重复计权。
        query_terms = list(dict.fromkeys(_real_words(query)))
        if query_terms:
            hit = sum(1 for t in query_terms if t in haystack)
            term_cov = hit / len(query_terms)
        else:
            term_cov = 0.0

        query_grams = list(dict.fromkeys(char_ngrams(query_norm, 2)))
        if query_grams:
            gram_hit = sum(1 for g in query_grams if g in haystack)
            gram_cov = gram_hit / len(query_grams)
        else:
            gram_cov = 0.0

        authority = int(metadata.get("authority", 2) or 2)
        # authority 1 → 1.0；authority 5 → 0.2
        authority_bonus = max(0.0, (6 - authority) / 5.0)

        score = 0.60 * term_cov + 0.25 * gram_cov + 0.15 * authority_bonus

        # 标题命中是强信号（用户常直接问「奶油小熊能机洗吗」）
        title_norm = normalize_text(title)
        if title_norm and (title_norm in query_norm or query_norm in title_norm):
            score += self._title_boost

        return _clamp01(score)

    async def rerank(
        self,
        query: str,
        candidates: Sequence[dict[str, Any]],
        *,
        top_n: int,
    ) -> list[dict[str, Any]]:
        """重排并返回 Top-N（写入 `rerank_score` 与 `score`）。"""
        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            item["rerank_score"] = self._score_one(query, item)
            scored.append(item)
        scored.sort(key=lambda c: -float(c.get("rerank_score", 0.0)))
        out = scored[:top_n]
        for item in out:
            item["score"] = float(item["rerank_score"])
        return out

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        return True


class ApiReranker:
    """Cross-Encoder Rerank API 实现（国产境内服务）。

    兼容两类请求体（按服务自动适配）：
        * DashScope：`POST {base}/api/v1/services/rerank/text-rerank/text-rerank`
          `{"model": ..., "input": {"query": ..., "documents": [...]}, "parameters": {...}}`
        * OpenAI 兼容：`POST {base}/v1/rerank`
          `{"model": ..., "query": ..., "documents": [...], "top_n": n}`

    返回的 `relevance_score` 直接作为最终分数（厂商已归一化到 0~1）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "gte-rerank",
        timeout: float = 10.0,
        protocol: str = "auto",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._protocol = protocol

    @property
    def name(self) -> str:
        """实现名。"""
        return f"api-{self._model}"

    def _assert_allowlisted(self) -> None:
        """校验出口域名白名单（**禁止境外模型 API**）。"""
        from urllib.parse import urlparse

        host = (urlparse(self._base_url).hostname or "").lower()
        allowlist = [d.lower() for d in get_settings().llm_domain_allowlist]
        if host and not any(host == d or host.endswith(f".{d}") for d in allowlist):
            raise PermissionError(
                f"Rerank 出口域名 {host!r} 不在白名单内（BE-16 DoD）"
            )

    def _is_dashscope(self) -> bool:
        """是否走 DashScope 协议。"""
        if self._protocol in {"dashscope", "openai"}:
            return self._protocol == "dashscope"
        return "dashscope" in self._base_url or "aliyuncs" in self._base_url

    async def rerank(
        self,
        query: str,
        candidates: Sequence[dict[str, Any]],
        *,
        top_n: int,
    ) -> list[dict[str, Any]]:
        """调用重排 API。失败时抛异常，由上层决定降级（不要静默返回原序）。"""
        import httpx

        if not candidates:
            return []
        self._assert_allowlisted()
        documents = [str(c.get("content", "")) for c in candidates]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._is_dashscope():
            url = (
                f"{self._base_url}/api/v1/services/rerank/"
                "text-rerank/text-rerank"
            )
            payload: dict[str, Any] = {
                "model": self._model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top_n, "return_documents": False},
            }
        else:
            url = f"{self._base_url}/v1/rerank"
            payload = {
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()

        if self._is_dashscope():
            raw_results = (body.get("output") or {}).get("results", [])
        else:
            raw_results = body.get("results", [])

        out: list[dict[str, Any]] = []
        for item in raw_results[:top_n]:
            idx = int(item.get("index", 0))
            if idx < 0 or idx >= len(candidates):
                continue
            merged = dict(candidates[idx])
            score = _clamp01(float(item.get("relevance_score", 0.0)))
            merged["rerank_score"] = score
            merged["score"] = score
            out.append(merged)
        out.sort(key=lambda c: -float(c.get("score", 0.0)))
        return out

    async def health(self) -> bool:
        """探测一次极短重排。"""
        try:
            await self.rerank(
                "健康检查",
                [{"chunk_id": "probe", "content": "健康检查"}],
                top_n=1,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("Rerank API 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_RERANKER: Any = None


def get_reranker() -> Any:
    """获取重排实现（单例）。

    策略：配置了 `WK_RERANK_BASE_URL` + `WK_RERANK_API_KEY` 时用 API 实现，
    否则用本地字面重排。
    """
    global _RERANKER  # noqa: PLW0603
    if _RERANKER is None:
        settings = get_settings()
        base_url = getattr(settings, "rerank_base_url", "") or ""
        secret = getattr(settings, "rerank_api_key", None)
        api_key = secret.get_secret_value() if secret is not None else ""
        if base_url and api_key:
            logger.info("重排实现：ApiReranker（%s）", base_url)
            _RERANKER = ApiReranker(base_url=base_url, api_key=api_key)
        else:
            logger.info("重排实现：LexicalReranker（确定性字面重排）")
            _RERANKER = LexicalReranker()
    return _RERANKER


def set_reranker(reranker: Any) -> None:
    """替换重排实现（测试用）。"""
    global _RERANKER  # noqa: PLW0603
    _RERANKER = reranker


__all__ = [
    "ApiReranker",
    "LexicalReranker",
    "get_reranker",
    "set_reranker",
]
