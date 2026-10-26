"""语音识别适配器（BE-10 第 ③ 步）。

| 实现 | 用途 |
| --- | --- |
| `LocalAsr` | 本地/单测：按音频指纹查固定话术表；查不到则明确「无识别引擎」 |
| `HttpAsr` | 生产：阿里云 / 讯飞语音识别（HTTP 接口，境内服务） |

**为什么本地实现不「假装」识别出文字**：ASR 是外部模型能力，本地没有引擎时
编造文本会污染下游意图识别与检索（还会让验收数据虚高）。所以本地实现的行为是
**如实报告无引擎**（`degraded=["ASR_NO_ENGINE"]`、`confidence=0`），由 BE-10 的
兜底话术接住「语音没听清，麻烦您再发一次或打字～」。

**演示/回归怎么办**：`LocalAsr` 支持一个**指纹→文本**的固定表（`fixture` 参数或
`WK_ASR_FIXTURE_FILE` 指向的 JSON）。同一段音频字节的 sha256 恒定，所以演示可复现，
且不会把「不存在的识别能力」带进生产路径——没有表就是没有引擎。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.adapters.ports import AsrResult
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _load_fixture_file(path: str | Path | None) -> dict[str, Any]:
    """读取指纹表；文件不存在或损坏返回空表（不阻断启动）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置坏掉不应让服务起不来
        logger.warning("ASR 指纹表损坏（%s），按空表处理", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


class LocalAsr:
    """本地 ASR：指纹表命中才出文本，否则如实报无引擎。"""

    def __init__(self, fixture: dict[str, Any] | None = None) -> None:
        self._fixture: dict[str, Any] = dict(fixture or {})
        self._loaded_from_file = False

    @property
    def name(self) -> str:
        """实现名。"""
        return "local-fixture"

    def _ensure_loaded(self) -> None:
        """首次使用时从配置的文件补载（避免 import 期读盘）。"""
        if self._loaded_from_file or self._fixture:
            self._loaded_from_file = True
            return
        settings = get_settings()
        path = getattr(settings, "asr_fixture_file", "") or ""
        if path:
            self._fixture = _load_fixture_file(path)
        self._loaded_from_file = True

    async def transcribe(
        self,
        wav: bytes,
        *,
        hotwords: Any = (),
        sample_rate: int = 16000,
    ) -> AsrResult:
        """按 sha256 查表；未命中返回空文本 + `ASR_NO_ENGINE`。"""
        self._ensure_loaded()
        digest = hashlib.sha256(wav).hexdigest()
        hit = self._fixture.get(digest)
        if isinstance(hit, str):
            return AsrResult(hit, confidence=0.95, degraded=[])
        if isinstance(hit, dict):
            text = str(hit.get("text", ""))
            conf = float(hit.get("confidence", 0.95))
            return AsrResult(text, confidence=conf, degraded=list(hit.get("degraded", [])))
        logger.info(
            "本地 ASR 无识别引擎（音频 %d 字节，指纹 %s…）", len(wav), digest[:8]
        )
        return AsrResult("", confidence=0.0, degraded=["ASR_NO_ENGINE"])

    async def health(self) -> bool:
        """本地实现恒为健康（不依赖外部服务）。"""
        return True


class HttpAsr:
    """HTTP ASR（阿里云 / 讯飞，境内服务）。

    走「上传 16k/16bit/mono wav → 拿回 JSON」的通用形态；不同厂商的字段差异
    由 `_extract_text` 吸收。**不做重试**：BE-10 的兜底话术比静默重试更快让用户
    得到反馈，重试由上层限流器统一控制。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        provider: str = "aliyun",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._provider = provider

    @property
    def name(self) -> str:
        """实现名。"""
        return f"http-{self._provider}"

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> tuple[str, float]:
        """从各厂商响应里取文本与置信度。"""
        for key in ("text", "result", "transcript"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                conf = payload.get("confidence", payload.get("score", 0.9))
                return value, float(conf)
        # 阿里云：{"flash_result":{"sentences":[{"text":..,"confidence":..}]}}
        sentences = (
            payload.get("flash_result", {}).get("sentences")
            or payload.get("sentences")
            or []
        )
        if isinstance(sentences, list) and sentences:
            texts = [str(s.get("text", "")) for s in sentences if isinstance(s, dict)]
            confs = [
                float(s.get("confidence", 0.9))
                for s in sentences
                if isinstance(s, dict)
            ]
            return "".join(texts), (sum(confs) / len(confs) if confs else 0.9)
        return "", 0.0

    async def transcribe(
        self,
        wav: bytes,
        *,
        hotwords: Any = (),
        sample_rate: int = 16000,
    ) -> AsrResult:
        """调用远端识别；任何异常都降级成「没听清」而不是 500。"""
        try:
            import httpx

            headers = {"Authorization": f"Bearer {self._api_key}"}
            files = {"audio": ("voice.wav", wav, "audio/wav")}
            data = {"sample_rate": str(sample_rate), "format": "wav"}
            hot = " ".join(str(w) for w in (hotwords or ()))
            if hot:
                data["hotwords"] = hot
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/asr", headers=headers, files=files, data=data
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 外部依赖失败必须兜底
            logger.warning("ASR 调用失败：%s", type(exc).__name__)
            return AsrResult("", confidence=0.0, degraded=["ASR_CALL_FAILED"])
        text, conf = self._extract_text(payload if isinstance(payload, dict) else {})
        if not text:
            return AsrResult("", confidence=0.0, degraded=["ASR_EMPTY_RESULT"])
        return AsrResult(text, confidence=conf, degraded=[])

    async def health(self) -> bool:
        """探测 `/health`。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self._base_url}/health",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("ASR 健康检查失败：%s", type(exc).__name__)
            return False


#: 进程级单例
_ASR: object | None = None


def get_asr() -> object:
    """获取 ASR 实现（单例）。

    策略：`WK_ASR_PROVIDER` 为 `aliyun`/`xfyun` 且配了 base_url + api_key 时用
    `HttpAsr`，否则用 `LocalAsr`（指纹表或如实报告无引擎）。
    """
    global _ASR  # noqa: PLW0603
    if _ASR is None:
        settings = get_settings()
        provider = (getattr(settings, "asr_provider", "local") or "local").lower()
        base_url = getattr(settings, "asr_base_url", "") or ""
        api_key = ""
        try:
            api_key = settings.asr_api_key.get_secret_value()
        except Exception:  # noqa: BLE001 - 未配置时按空处理
            api_key = ""
        if provider in {"aliyun", "xfyun"} and base_url and api_key:
            logger.info("ASR 实现：HttpAsr（%s）", provider)
            _ASR = HttpAsr(base_url=base_url, api_key=api_key, provider=provider)
        else:
            logger.info("ASR 实现：LocalAsr（无外部识别引擎，命中指纹表才出文本）")
            _ASR = LocalAsr()
    return _ASR


def set_asr(impl: object | None) -> None:
    """替换 ASR 实现（测试用）。"""
    global _ASR  # noqa: PLW0603
    _ASR = impl


__all__ = ["HttpAsr", "LocalAsr", "get_asr", "set_asr"]
