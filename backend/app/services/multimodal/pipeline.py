"""多模态统一入口（BE-10 / BE-11 / BE-12 的汇合点）。

## 这个模块存在的唯一理由：**结构同构**

任务书 BE-11 的 DoD 写的是「图片路径与文本/语音路径**结构同构**」。字面意思是：
不管用户发的是文字、语音还是图片，**进入编排层的东西必须是同一个结构**，
这样意图识别、检索、工具调用、三道闸、转人工**一行都不用分叉**。

所以这里定义 `NormalizedMessage`：三条路径都必须填出它，字段含义完全一致。
编排层只认 `text`（归一后的用户意图文本）与 `degraded`（降级标识），
其余字段是**证据**，用于写消息记录、上下文卡片和排障。

## 三条路径各自的归一规则

| 输入 | 归一后的 `text` | `text_source` |
| --- | --- | --- |
| 文本 | 原样 | `ORIGINAL` |
| 语音 | ASR 识别文本 | `ASR` |
| 图片-订单截图 | `帮我查一下订单 {12位订单号}` | `OCR` |
| 图片-商品图 | 分类器描述，兜底「我发的是商品图，想咨询这款商品」 | `VLM` |
| 图片-瑕疵图 | 「商品收到后有瑕疵，我想反馈质量问题」 | `VLM` |
| 图片-尺码表 | 「麻烦帮我看下尺码表，我该选多大码」 | `VLM` |
| 图片-表情包 | **不进入编排**，直接轻引导 | `VLM` |

## 失败时怎么办

`ok=False` + `fallback_key` 指向 `messages.py` 里的话术。调用方（chat API）
看到 `ok=False` 就**直接回话术**，不调编排器——因为此时没有任何可信的用户意图，
硬塞一句空文本进编排只会让 RAG 检索出无关知识、甚至误判情绪转人工。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.asyncbridge import run_maybe_async
from app.core.config import get_settings
from app.core.enums import ImageRoute, InboundMsgType
from app.core.logging import get_logger
from app.services.multimodal import messages as mm_messages
from app.services.multimodal.audio import (
    REASON_EMPTY,
    REASON_TOOL_MISSING,
    REASON_UNSUPPORTED,
    TranscodeError,
    transcode_to_wav,
    wav_duration_seconds,
)
from app.services.multimodal.image import ImageRejected, route_image, validate_image
from app.services.multimodal.order_screenshot import DEGRADED_AMBIGUOUS, find_order_no

#: 抠号置信度到这个数，才把「这图是订单截图」当作分类强信号。
#: 实测分档：真订单截图 ≥0.75，中文偏旁拼出来的假号码稳定落在 0.6（且带
#: `ORDER_NO_AMBIGUOUS`），所以 0.7 能干净地把两者分开。
_ORDER_SIGNAL_CONFIDENCE = 0.7

logger = get_logger(__name__)

#: 文本来源（与 `Message.text_source` 取值一致）。
SOURCE_ORIGINAL = "ORIGINAL"
SOURCE_ASR = "ASR"
SOURCE_OCR = "OCR"
SOURCE_VLM = "VLM"


@dataclass(slots=True)
class NormalizedMessage:
    """归一后的入站消息（**三条路径共用的唯一结构**）。

    Attributes:
        ok: 是否归一成功。`False` 时调用方直接回 `fallback_text`，不调编排。
        msg_type: 原始入站类型（`TEXT` / `VOICE` / `IMAGE` …）。
        text: 归一后的用户意图文本（**编排层唯一输入**）。
        text_source: 文本来源，见模块级 `SOURCE_*`。
        confidence: 归一置信度（文本恒 1.0；语音看 ASR；图片看 OCR/分类）。
        route: 图片五路之一的中文值，非图片为空。
        order_no: 图片订单截图里抠出的 12 位订单号。
        degraded: 降级标识列表（会合并进对话响应的 `degraded`）。
        latency_ms: 各阶段耗时（`asr` / `ocr` / `vlm` / `total`）。
        fallback_key: `ok=False` 时用哪条话术（`messages.FALLBACK_TEXTS` 的 key）。
        media_url: 原始媒体落地后的地址（`local://` / OSS）。
        attachments: 附件证据（写消息记录与上下文卡片用）。
        extra: 其余证据（图片元信息、ASR 时长等）。
    """

    ok: bool = True
    msg_type: str = InboundMsgType.TEXT.value
    text: str = ""
    text_source: str = SOURCE_ORIGINAL
    confidence: float = 1.0
    route: str = ""
    order_no: str = ""
    degraded: list[str] = field(default_factory=list)
    latency_ms: dict[str, int] = field(default_factory=dict)
    fallback_key: str = ""
    media_url: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fallback_text(self) -> str:
        """`ok=False` 时应回复的话术；`ok=True` 时为空串。"""
        return "" if self.ok else mm_messages.fallback_text(self.fallback_key)

    def to_dict(self) -> dict[str, Any]:
        """转字典（写消息记录 / 响应排障字段）。"""
        return {
            "ok": self.ok,
            "msg_type": self.msg_type,
            "text": self.text,
            "text_source": self.text_source,
            "confidence": round(self.confidence, 3),
            "route": self.route,
            "order_no": self.order_no,
            "degraded": list(self.degraded),
            "latency_ms": dict(self.latency_ms),
            "fallback_key": self.fallback_key,
            "fallback_text": self.fallback_text,
            "media_url": self.media_url,
            "attachments": list(self.attachments),
            "extra": dict(self.extra),
        }


class MultimodalPipeline:
    """多模态预处理流水线。

    Args:
        asr: ASR 实现；`None` 时用 `get_asr()`。
        ocr: OCR 实现；`None` 时用 `get_ocr()`。
        media_store: 媒体存储；`None` 时用 `get_media_store()`。
    """

    def __init__(
        self,
        *,
        asr: Any | None = None,
        ocr: Any | None = None,
        media_store: Any | None = None,
    ) -> None:
        self._asr = asr
        self._ocr = ocr
        self._store = media_store

    # ------------------------------------------------------------ 惰性依赖
    @property
    def asr(self) -> Any:
        """ASR 实现（惰性）。"""
        if self._asr is None:
            from app.adapters.asr import get_asr

            self._asr = get_asr()
        return self._asr

    @property
    def ocr(self) -> Any:
        """OCR 实现（惰性）。"""
        if self._ocr is None:
            from app.adapters.ocr import get_ocr

            self._ocr = get_ocr()
        return self._ocr

    @property
    def store(self) -> Any:
        """媒体存储（惰性）。"""
        if self._store is None:
            from app.adapters.media_store import get_media_store

            self._store = get_media_store()
        return self._store

    # ------------------------------------------------------------ 主入口
    def preprocess(
        self,
        *,
        msg_type: str = InboundMsgType.TEXT.value,
        text: str = "",
        media: bytes | None = None,
        content_type: str = "",
        filename: str = "",
        hotwords: tuple[str, ...] = (),
        save_media: bool = True,
    ) -> NormalizedMessage:
        """把任意入站消息归一成 `NormalizedMessage`。

        Args:
            msg_type: 入站类型（不区分大小写）。
            text: 文本内容（`TEXT` 必填；其他类型可作为补充说明）。
            media: 媒体字节（`VOICE` / `IMAGE` 必填）。
            content_type: MIME 类型。
            filename: 原始文件名（推断格式用）。
            hotwords: ASR 热词。
            save_media: 是否把原始媒体落盘（默认是，供 7 天生命周期管理）。

        Returns:
            `NormalizedMessage`。**任何异常都在这里收敛成 `ok=False`**，
            不往上抛——预处理失败不该让整轮对话 500。
        """
        kind = (msg_type or InboundMsgType.TEXT.value).strip().upper()
        started = time.perf_counter()
        result = NormalizedMessage(msg_type=kind)

        try:
            if kind == InboundMsgType.VOICE.value:
                self._handle_voice(
                    result,
                    media=media,
                    content_type=content_type,
                    filename=filename,
                    hotwords=hotwords,
                    save_media=save_media,
                )
            elif kind == InboundMsgType.IMAGE.value:
                self._handle_image(
                    result,
                    media=media,
                    content_type=content_type,
                    filename=filename,
                    hint_text=text,
                    save_media=save_media,
                )
            else:
                # TEXT / CARD / GOODS：文本原样透传（卡片/商品消息的文本字段
                # 由渠道适配层填好，这里不重复解析）。
                result.text = (text or "").strip()
                result.text_source = SOURCE_ORIGINAL
                result.confidence = 1.0
                if not result.text:
                    result.ok = False
                    result.fallback_key = "IMAGE_IRRELEVANT"
                    result.degraded.append("EMPTY_TEXT")
        except Exception as exc:  # noqa: BLE001 - 预处理绝不外抛
            logger.warning(
                "多模态预处理异常 type=%s msg_type=%s", type(exc).__name__, kind
            )
            result.ok = False
            result.fallback_key = (
                "VOICE_UNHEARD" if kind == InboundMsgType.VOICE.value else "IMAGE_UNREADABLE"
            )
            result.degraded.append(f"PREPROCESS_ERROR:{type(exc).__name__}")

        result.latency_ms["total"] = int((time.perf_counter() - started) * 1000)
        return result

    # ------------------------------------------------------------ 语音
    def _handle_voice(
        self,
        result: NormalizedMessage,
        *,
        media: bytes | None,
        content_type: str,
        filename: str,
        hotwords: tuple[str, ...],
        save_media: bool,
    ) -> None:
        """语音：转码 → 时长校验 → ASR。"""
        if not media:
            result.ok = False
            result.fallback_key = "VOICE_UNHEARD"
            result.degraded.append("VOICE_EMPTY")
            return

        settings = get_settings()
        fmt = self._guess_audio_format(content_type=content_type, filename=filename)

        t = time.perf_counter()
        try:
            wav = transcode_to_wav(media, fmt)
        except TranscodeError as exc:
            result.latency_ms["asr"] = int((time.perf_counter() - t) * 1000)
            result.ok = False
            result.degraded.append(f"ASR_TRANSCODE_FAILED:{exc.reason}")
            # 格式不支持与工具缺失对用户的含义不同：前者「换一条」，后者「打字」
            result.fallback_key = (
                "VOICE_UNSUPPORTED"
                if exc.reason in {REASON_UNSUPPORTED, REASON_EMPTY}
                else "VOICE_UNHEARD"
            )
            if exc.reason == REASON_TOOL_MISSING:
                logger.error("转码工具缺失：%s", exc.detail)
            return
        transcode_ms = int((time.perf_counter() - t) * 1000)
        result.latency_ms["asr"] = transcode_ms

        # 时长上限（防止超长语音把 ASR 配额打爆）
        max_seconds = int(getattr(settings, "voice_max_duration_seconds", 60))
        duration = wav_duration_seconds(wav)
        if max_seconds and duration > max_seconds:
            result.ok = False
            result.fallback_key = "VOICE_TOO_LONG"
            result.degraded.append("VOICE_TOO_LONG")
            result.extra["duration_seconds"] = round(duration, 2)
            return

        if save_media:
            result.media_url = self._save_media(
                media, prefix="voice", content_type=content_type or "audio/amr"
            )

        # ASR
        t = time.perf_counter()
        asr_result = run_maybe_async(
            self.asr.transcribe(wav, hotwords=hotwords, sample_rate=16000)
        )
        result.latency_ms["asr"] = transcode_ms + int(
            (time.perf_counter() - t) * 1000
        )

        recognized = str(getattr(asr_result, "text", "") or "").strip()
        confidence = float(getattr(asr_result, "confidence", 0.0) or 0.0)
        result.degraded.extend(list(getattr(asr_result, "degraded", []) or []))
        result.extra["duration_seconds"] = round(duration, 2)
        result.extra["asr_confidence"] = round(confidence, 3)

        if not recognized:
            result.ok = False
            result.fallback_key = "VOICE_UNHEARD"
            result.degraded.append("ASR_EMPTY")
            return

        low = float(getattr(settings, "asr_low_confidence", 0.6))
        if confidence and confidence < low:
            # 识别出来了但没把握：宁可用户再说一遍，也不要拿错字去检索/查单。
            result.ok = False
            result.fallback_key = "VOICE_UNHEARD"
            result.degraded.append("ASR_LOW_CONFIDENCE")
            return

        result.text = recognized
        result.text_source = SOURCE_ASR
        result.confidence = confidence or 0.9
        result.attachments.append(
            {
                "type": "voice",
                "media_url": result.media_url,
                "duration_seconds": round(duration, 2),
                "text_source": SOURCE_ASR,
            }
        )

    # ------------------------------------------------------------ 图片
    def _handle_image(
        self,
        result: NormalizedMessage,
        *,
        media: bytes | None,
        content_type: str,
        filename: str,
        hint_text: str,
        save_media: bool,
    ) -> None:
        """图片：校验 → OCR → 五路分流 → 按路由生成意图文本。"""
        if not media:
            result.ok = False
            result.fallback_key = "IMAGE_UNREADABLE"
            result.degraded.append("IMAGE_EMPTY")
            return

        # ① 校验（三类异常都归「图片没能加载出来」）
        t = time.perf_counter()
        try:
            info = validate_image(
                media, content_type=content_type, filename=filename
            )
        except ImageRejected as exc:
            result.latency_ms["ocr"] = int((time.perf_counter() - t) * 1000)
            result.ok = False
            result.fallback_key = "IMAGE_UNREADABLE"
            result.degraded.append(exc.reason)
            result.extra["reject_detail"] = exc.detail
            return

        result.extra["image"] = info.to_dict()

        # ② OCR（订单截图必需；其他路由只是辅助信号，失败不影响分流）
        t = time.perf_counter()
        ocr_text = ""
        ocr_confidence = 0.0
        ocr_blocks: list[dict[str, Any]] = []
        try:
            ocr_result = run_maybe_async(
                self.ocr.recognize(media, content_type=info.content_type)
            )
            ocr_text = str(getattr(ocr_result, "text", "") or "")
            ocr_confidence = float(getattr(ocr_result, "confidence", 0.0) or 0.0)
            ocr_blocks = list(getattr(ocr_result, "blocks", []) or [])
            result.degraded.extend(
                list(getattr(ocr_result, "degraded", []) or [])
            )
        except Exception as exc:  # noqa: BLE001 - OCR 失败不阻断分流
            logger.warning("OCR 失败：%s", type(exc).__name__)
            result.degraded.append("OCR_CALL_FAILED")
        result.latency_ms["ocr"] = int((time.perf_counter() - t) * 1000)

        # ②-2 先抠订单号，再分流。
        # 为什么把顺序倒过来：本地 OCR 只认 0-9，从「订单号：」这类中文标签拿不到
        # 任何语义信号，于是**中文偏旁会被误配成数字并粘进号码**（实测尺码表的
        # 「身高 150 体重 45」会变成 15 位假号码）。而「这一行 12 位数字是不是真号码」
        # 恰恰是区分订单截图与尺码表最可靠的判据——它靠字形几何和分数分布判断，
        # 不依赖 OCR 是否认得出中文，比在噪声文本上跑正则稳得多。
        found_order = find_order_no(ocr_text, blocks=ocr_blocks)
        order_signal = (
            found_order.found
            and found_order.confidence >= _ORDER_SIGNAL_CONFIDENCE
            and DEGRADED_AMBIGUOUS not in found_order.degraded
        )

        # ③ 五路分流
        # 用户同时发的文字是**很强的分类证据**（「这个有瑕疵」配一张特写图，
        # 本地规则和真 VLM 都该利用），所以一并喂给分类器；OCR 文本仍排在最前。
        t = time.perf_counter()
        signal_text = "订单号" if order_signal else ""
        classify_text = "\n".join(
            x for x in (ocr_text, signal_text, hint_text.strip()) if x
        )
        routed = run_maybe_async(
            route_image(
                media, ocr_text=classify_text, content_type=info.content_type
            )
        )
        result.latency_ms["vlm"] = int((time.perf_counter() - t) * 1000)
        result.route = routed.route
        result.degraded.extend(routed.degraded)
        result.extra["route_labels"] = routed.labels

        if save_media:
            result.media_url = self._save_media(
                media, prefix="image", content_type=info.content_type
            )

        attachment: dict[str, Any] = {
            "type": "image",
            "route": routed.route,
            "route_confidence": round(routed.confidence, 3),
            "description": routed.description,
            "media_url": result.media_url,
            "size": {"width": info.width, "height": info.height},
        }
        if ocr_text:
            attachment["ocr_excerpt"] = ocr_text[:200]

        # ④ 按路由生成意图文本
        if routed.route == ImageRoute.ORDER_SCREENSHOT.value:
            found = found_order
            result.degraded.extend(found.degraded)
            result.extra["ocr_confidence"] = round(ocr_confidence, 3)
            if not found.found:
                result.ok = False
                result.fallback_key = "ORDER_NO_NOT_FOUND"
                result.degraded.append("ORDER_NO_NOT_FOUND")
                result.attachments.append(attachment)
                return
            result.order_no = found.order_no
            result.text = f"帮我查一下订单 {found.order_no}"
            result.text_source = SOURCE_OCR
            result.confidence = found.confidence
            attachment["order_no"] = found.order_no
            attachment["order_no_confidence"] = round(found.confidence, 3)

        elif routed.route == ImageRoute.DEFECT_IMAGE.value:
            result.text = "商品收到后有瑕疵，我想反馈质量问题"
            result.text_source = SOURCE_VLM
            result.confidence = routed.confidence
            if hint_text.strip():
                result.text = f"{hint_text.strip()}（用户同时发了瑕疵照片）"

        elif routed.route == ImageRoute.SIZE_CHART.value:
            result.text = "麻烦帮我看下尺码表，我该选多大码"
            result.text_source = SOURCE_VLM
            result.confidence = routed.confidence
            if hint_text.strip():
                result.text = f"{hint_text.strip()}（附尺码表截图）"

        elif routed.route == ImageRoute.PRODUCT_IMAGE.value:
            result.text = (
                hint_text.strip()
                or routed.description
                or "我发的是商品图，想咨询这款商品"
            )
            result.text_source = SOURCE_VLM
            result.confidence = routed.confidence

        else:  # MEME_IRRELEVANT
            result.ok = False
            result.fallback_key = "IMAGE_IRRELEVANT"
            result.text_source = SOURCE_VLM
            result.degraded.append("IMAGE_IRRELEVANT")

        result.attachments.append(attachment)

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _guess_audio_format(*, content_type: str, filename: str) -> str:
        """从 MIME / 文件名推断音频格式。"""
        if filename and "." in filename:
            return filename.rsplit(".", 1)[-1].lower()
        if content_type and "/" in content_type:
            sub = content_type.split("/", 1)[1].lower()
            if "silk" in sub:
                return "silk"
            if "amr" in sub:
                return "amr"
            if "mpeg" in sub or "mp3" in sub:
                return "mp3"
            if "wav" in sub or "x-wav" in sub:
                return "wav"
            return sub
        return "amr"  # 平台语音消息默认 amr

    def _save_media(
        self, data: bytes, *, prefix: str, content_type: str
    ) -> str:
        """把原始媒体落盘并返回地址（失败不影响主流程）。"""
        try:
            key = f"{prefix}/{hashlib.sha256(data).hexdigest()[:24]}"
            return str(
                run_maybe_async(
                    self.store.put(key, data, content_type=content_type)
                )
            )
        except Exception as exc:  # noqa: BLE001 - 落盘失败只丢证据，不阻断
            logger.warning("媒体落盘失败：%s", type(exc).__name__)
            return ""


#: 进程级单例
_PIPELINE: MultimodalPipeline | None = None


def get_pipeline() -> MultimodalPipeline:
    """获取多模态流水线（单例）。"""
    global _PIPELINE  # noqa: PLW0603
    if _PIPELINE is None:
        _PIPELINE = MultimodalPipeline()
    return _PIPELINE


def set_pipeline(pipeline: MultimodalPipeline | None) -> None:
    """替换单例（测试用）。"""
    global _PIPELINE  # noqa: PLW0603
    _PIPELINE = pipeline


__all__ = [
    "MultimodalPipeline",
    "NormalizedMessage",
    "SOURCE_ASR",
    "SOURCE_OCR",
    "SOURCE_ORIGINAL",
    "SOURCE_VLM",
    "get_pipeline",
    "set_pipeline",
]
