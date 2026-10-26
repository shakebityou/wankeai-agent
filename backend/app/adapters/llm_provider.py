"""LLM Provider 适配器（BE-16 LLM 网关的驱动层）。

| 实现 | 用途 |
| --- | --- |
| `OpenAiCompatProvider` | 生产：国产大模型 OpenAI 兼容接口（DashScope / 智谱） |
| `TemplateProvider` | 本地 / 单测 / 无 Key：**抽取式模板生成**（确定性、零幻觉） |

**为什么默认必须有 `TemplateProvider`**：任务书要求「三道闸防幻觉」，
而三道闸本身需要可重复的测试基线。模板生成器只把**检索到的原文**
按槽位拼装成回复，不产生任何新事实，因此：

    * 可以用它验证「无依据必拒答」链路是否真的生效；
    * 可以在 CI 中稳定复现，不依赖网络与模型随机性；
    * 切到真实模型只需改 `WK_LLM_PROVIDER` 与 Key，业务代码零改动。

⚠️ **出口域名白名单**：`_assert_allowlisted()` 在每次真实调用前校验，
命中非白名单域名直接抛 `PermissionError`（BE-16 DoD：不得调境外模型）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.adapters.ports import LlmResult
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 中文标点与句末切分（用于模板生成的分句）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")


class TemplateProvider:
    """抽取式模板生成器（确定性、零幻觉、无网络）。

    **工作方式**：把 system prompt 与 user prompt 里出现的「知识片段」
    （以 `【知识片段 N】` 标注的段落）抽出来，按问题类型选择模板，
    用**原文句子**拼装回复，绝不生成原文之外的事实。

    这使它天然满足三道闸中的闸 2（事实一致性）：回复里的每个断言都能在
    上下文中逐字找到。因此它也是**闸逻辑的测试基线**——如果连模板生成器
    都被闸拦下，说明闸过于严格；如果模板生成器漏掉了明显的幻觉，
    说明闸过于宽松。

    Attributes:
        name: 实现名。
        model: 模型标识（写入 `LlmResult.model`）。
    """

    def __init__(self, *, model: str = "local-template-v1") -> None:
        self._model = model

    @property
    def name(self) -> str:
        """实现名。"""
        return "local-template"

    @property
    def model(self) -> str:
        """模型标识。"""
        return self._model

    @staticmethod
    def _extract_fragments(prompt: str) -> list[str]:
        """从提示词中抽取【知识片段 N】段落。

        只在遇到**带编号**的 `【知识片段 N】` 标记之后才开始收集正文。
        两个容易踩的坑（都实际出过缺陷）：

        1. 把「用户问题：…」这类前缀行当成片段 → 模板把问题原文回抄进回复；
        2. 「【知识片段】」是整个上下文的**总标题**（不编号），
           若也当作片段起点，收集到的第一段就是标题本身，
           回复会以「【知识片段】问：…」开头。

        所以判定条件必须是「`【知识片段` 且 **后面跟数字**」。

        Args:
            prompt: 提示词文本。

        Returns:
            片段原文列表（按出现顺序）。
        """
        fragments: list[str] = []
        current: list[str] | None = None
        header_re = re.compile(r"^【知识片段\s*\d+】\s*")
        for line in prompt.splitlines():
            stripped = line.strip()
            if header_re.match(stripped):
                if current:
                    fragments.append("\n".join(current).strip())
                current = []
                rest = header_re.sub("", stripped)
                if rest:
                    current.append(rest)
            elif stripped.startswith("【"):
                # 其他标记（如总标题【知识片段】、及【用户问题】）视为片段边界
                if current:
                    fragments.append("\n".join(current).strip())
                current = None
            elif current is not None:
                if stripped:
                    current.append(stripped)
                else:
                    fragments.append("\n".join(current).strip())
                    current = None
        if current:
            fragments.append("\n".join(current).strip())
        return [f for f in fragments if f]

    @staticmethod
    def _strip_qa_scaffolding(text: str) -> str:
        """去掉知识片段里的「问：/相：/答：」脚手架，只留可对外说的人话。

        FAQ 知识卡在库里的原始结构是：

            问：抱枕可以机洗吗
            相：抱枕怎么洗、毛绒抱枕能机洗吗
            答：抱枕填充三维中空棉，支持机洗……

        「相：」是**相似问**（检索用），对用户说出口毫无意义；
        「问：」「答：」作为标签也不该出现在客服回复里。
        所以取「答：」之后的正文；没有「答：」时去掉所有标签前缀后返回。

        Args:
            text: 片段原文。

        Returns:
            可直接对外表述的正文。
        """
        if "答：" in text or "答:" in text:
            idx = text.find("答：")
            if idx < 0:
                idx = text.find("答:")
            # 「答：」之后的内容才是答案正文
            text = text[idx + 2 :]
        else:
            # 逐行去掉标签前缀
            lines = []
            for line in text.splitlines():
                line = re.sub(r"^\s*(?:问|相|答|Q|A)\s*[：:]\s*", "", line).strip()
                if line:
                    lines.append(line)
            text = "\n".join(lines)
        return text.strip()

    @staticmethod
    def _first_sentences(text: str, limit: int = 3) -> str:
        """取前 N 句（保持原文措辞）。"""
        parts = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p.strip()]
        return "".join(parts[:limit])

    @staticmethod
    def _extract_question(prompt: str) -> str:
        """从提示词中抽取用户问题。"""
        match = re.search(r"(?:用户问题|买家问题|问题)\s*[：:]\s*(.+)", prompt)
        if match:
            return match.group(1).strip()
        # 兜底：取最后一段非空、非标记行
        for line in reversed(prompt.splitlines()):
            stripped = line.strip()
            if stripped and not stripped.startswith("【") and "：" not in stripped[:4]:
                return stripped
        return ""

    def _compose(self, prompt: str, system: str | None) -> str:
        """按模板拼装回复。"""
        fragments = self._extract_fragments(prompt)
        question = self._extract_question(prompt)

        if not fragments:
            # **无依据**：不得编造，直接给出转人工/兜底话术
            return (
                "抱歉，这个我暂时查不到准确信息。"
                "我帮您转接人工客服，由同事为您核实，可以吗？"
            )

        body = self._first_sentences(
            self._strip_qa_scaffolding(fragments[0]), limit=3
        )
        if len(fragments) > 1:
            extra = self._first_sentences(
                self._strip_qa_scaffolding(fragments[1]), limit=2
            )
            if extra and extra not in body:
                body = f"{body}{extra}"

        # 去掉 Markdown 语法（出站必须纯文本，BE-02）
        body = re.sub(r"[*_`#>]", "", body).strip()
        if len(body) > 120:
            body = body[:118] + "。"
        if not body:
            return (
                "抱歉，这个我暂时查不到准确信息，"
                "我帮您转接人工客服核实，可以吗？"
            )
        if question:
            return body
        return body

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LlmResult:
        """生成回复（确定性，无网络）。"""
        start = time.perf_counter()
        text = self._compose(prompt, system)
        # 估算 token（中文约 1 字 1 token），用于成本口径一致性
        prompt_tokens = max(1, len(prompt) // 2)
        completion_tokens = max(1, len(text) // 2)
        _ = start
        return LlmResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self._model,
            provider=self.name,
            cached=False,
        )

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        """流式输出（按句切分，模拟 SSE delta）。"""
        result = await self.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        for part in _SENT_SPLIT_RE.split(result.text):
            if part:
                yield part

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        return True


class OpenAiCompatProvider:
    """OpenAI 兼容协议 Provider（DashScope / 智谱 / 其他境内服务）。

    统一走 `POST {base_url}/chat/completions`，因此同一套代码可对接
    DashScope（`https://dashscope.aliyuncs.com/compatible-mode/v1`）与
    智谱（`https://open.bigmodel.cn/api/paas/v4`）。

    生产要求（BE-16 DoD）：
        * 主备双通道：主通道失败 → 自动切备用（由 `LlmGateway` 负责，不在本类）；
        * 网络层白名单：`_assert_allowlisted()` 每次调用前校验；
        * token 计量：返回 `prompt_tokens` / `completion_tokens` 用于预算告警。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        provider_name: str = "openai-compat",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        """实现名。"""
        return self._provider_name

    @property
    def model(self) -> str:
        """模型标识。"""
        return self._model

    def _endpoint(self) -> str:
        """拼接 chat/completions 端点。"""
        if self._base_url.endswith("/chat/completions"):
            return self._base_url
        return f"{self._base_url}/chat/completions"

    def _assert_allowlisted(self) -> None:
        """校验出口域名白名单（**禁止境外模型 API**）。"""
        from urllib.parse import urlparse

        host = (urlparse(self._base_url).hostname or "").lower()
        allowlist = [d.lower() for d in get_settings().llm_domain_allowlist]
        if host and not any(host == d or host.endswith(f".{d}") for d in allowlist):
            raise PermissionError(
                f"LLM 出口域名 {host!r} 不在白名单内，"
                "禁止调用境外模型 API（BE-16 DoD）"
            )

    def _payload(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """构造请求体。"""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LlmResult:
        """同步完成一次生成。"""
        import httpx

        self._assert_allowlisted()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = self._payload(
            prompt, system, max_tokens, temperature, stream=False
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._endpoint(), json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "") or ""
        usage = body.get("usage") or {}
        return LlmResult(
            text=text.strip(),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            model=body.get("model", self._model),
            provider=self.name,
            cached=False,
        )

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        """流式生成（解析 SSE `data:` 行）。"""
        import httpx

        self._assert_allowlisted()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = self._payload(prompt, system, max_tokens, temperature, stream=True)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", self._endpoint(), json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece

    async def health(self) -> bool:
        """探测一次极短生成。"""
        try:
            result = await self.complete("ping", max_tokens=4)
            return bool(result.text) or result.completion_tokens >= 0
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("LLM 健康检查失败：%s", type(exc).__name__)
            return False


def build_provider(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 20.0,
) -> Any:
    """按配置构造 Provider（`local` → 模板生成器）。"""
    if provider == "local" or not base_url or not api_key:
        return TemplateProvider(model=model or "local-template-v1")
    name = {"dashscope": "dashscope", "zhipu": "zhipu"}.get(provider, provider)
    return OpenAiCompatProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        provider_name=name,
    )


# ---------------------------------------------------------------------------
# 单例工厂（业务层唯一入口）
# ---------------------------------------------------------------------------

_PROVIDER: Any | None = None
_FALLBACK: Any | None = None


def get_llm_provider() -> LlmProviderPort:
    """获取主 LLM Provider 单例。

    判定顺序：
        1. `llm_base_url` 与 `llm_api_key` 同时非空 → `OpenAiCompatProvider`；
        2. 否则 → `TemplateProvider`（本地 / CI / 无 Key）。

    **默认必须是模板生成器**：它只把检索到的原文按槽位拼装，不产生新事实，
    因此可作为「无依据必拒答」链路的确定性测试基线（见模块 docstring）。
    """
    global _PROVIDER  # noqa: PLW0603
    if _PROVIDER is None:
        settings = get_settings()
        _PROVIDER = build_provider(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    return _PROVIDER


def set_llm_provider(provider: Any | None) -> None:
    """替换主 Provider（**测试专用**）。"""
    global _PROVIDER  # noqa: PLW0603
    _PROVIDER = provider


def get_llm_fallback_provider() -> LlmProviderPort:
    """获取备用 LLM Provider 单例（BE-16 DoD：主备切换 100%）。

    备用通道未配置时**回落为主通道**，保证 `LlmGateway` 的调用方无需
    判断「有没有备用」，切换逻辑集中在网关。
    """
    global _FALLBACK  # noqa: PLW0603
    if _FALLBACK is None:
        settings = get_settings()
        if settings.llm_fallback_base_url and settings.llm_fallback_api_key.get_secret_value():
            _FALLBACK = build_provider(
                provider=settings.llm_fallback_provider,
                base_url=settings.llm_fallback_base_url,
                api_key=settings.llm_fallback_api_key.get_secret_value(),
                model=settings.llm_fallback_model,
                timeout=settings.llm_timeout_seconds,
            )
        else:
            _FALLBACK = get_llm_provider()
    return _FALLBACK


def set_llm_fallback_provider(provider: Any | None) -> None:
    """替换备用 Provider（**测试专用**）。"""
    global _FALLBACK  # noqa: PLW0603
    _FALLBACK = provider


__all__ = [
    "OpenAiCompatProvider",
    "TemplateProvider",
    "build_provider",
    "get_llm_fallback_provider",
    "get_llm_provider",
    "set_llm_fallback_provider",
    "set_llm_provider",
]
