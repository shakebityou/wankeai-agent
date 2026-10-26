"""图片接入校验 + 五路分流（BE-11）。

职责边界：
* **校验**（体积 / 格式 / 可解码）——不合规**不进入下游**，直接给兜底话术。
* **分流**——调 `ImageClassifierPort` 得到五路之一，供 `pipeline.py` 决定
  后续动作（订单截图走 BE-12，瑕疵图走售后话术，表情包走轻引导）。
* **不做** OCR 本身（那是 `order_screenshot.py` / `app.adapters.ocr`），也不查订单。

## 三类异常的处理口径（DoD 要求）

| 异常 | 判定 | 用户侧 | 内部标识 |
| --- | --- | --- | --- |
| 体积超限 | `len(data) > image_max_bytes` | 统一「图片没能加载出来」 | `IMAGE_TOO_LARGE` |
| 格式不支持 | MIME 不在白名单 **且** 实际解码格式不在白名单 | 同上 | `IMAGE_FORMAT_UNSUPPORTED` |
| 解码失败 | PIL 打不开 | 同上 | `IMAGE_DECODE_FAILED` |

三类都**不转人工**、不抛到编排层——图片坏了是传输问题，不是用户诉求。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from app.adapters.vlm import get_image_classifier
from app.core.config import get_settings
from app.core.enums import ImageRoute
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 校验失败原因（也是降级标识）。
REASON_EMPTY = "IMAGE_EMPTY"
REASON_TOO_LARGE = "IMAGE_TOO_LARGE"
REASON_FORMAT = "IMAGE_FORMAT_UNSUPPORTED"
REASON_DECODE = "IMAGE_DECODE_FAILED"

#: 图片扩展名 → MIME（用户上传时 content_type 常缺失，靠扩展名兜）
_EXT_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

#: PIL 格式名 → 白名单里的扩展名
_PIL_FORMAT_EXT: dict[str, str] = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
}


class ImageRejected(Exception):
    """图片不合规（体积/格式/解码）。

    Attributes:
        reason: 上面四个 `REASON_*` 之一。
        detail: 排障用的细节，**不进用户话术**。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(slots=True)
class ImageInfo:
    """通过校验的图片元信息。"""

    width: int
    height: int
    format: str
    ext: str
    content_type: str
    size_bytes: int

    @property
    def aspect(self) -> float:
        """高宽比（高 / 宽）。"""
        return self.height / float(self.width) if self.width else 0.0

    def to_dict(self) -> dict[str, Any]:
        """转字典（写进消息 extra / 上下文卡片）。"""
        return {
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "aspect": round(self.aspect, 3),
        }


def _allowed_formats() -> list[str]:
    """白名单（配置优先，缺省 jpg/jpeg/png/webp）。"""
    settings = get_settings()
    raw = getattr(settings, "image_allowed_formats", None) or []
    formats = [str(x).lower().lstrip(".") for x in raw]
    return formats or ["jpg", "jpeg", "png", "webp"]


def _max_bytes() -> int:
    """体积上限（默认 10MB）。"""
    settings = get_settings()
    return int(getattr(settings, "image_max_bytes", 10 * 1024 * 1024))


def validate_image(
    data: bytes,
    *,
    content_type: str = "",
    filename: str = "",
) -> ImageInfo:
    """校验图片并返回元信息。

    Args:
        data: 原始字节。
        content_type: 声明的 MIME（可能为空或不准）。
        filename: 原始文件名（用于按扩展名推断）。

    Returns:
        `ImageInfo`。

    Raises:
        ImageRejected: 体积超限 / 格式不支持 / 解码失败。
    """
    if not data:
        raise ImageRejected(REASON_EMPTY, "空字节")

    limit = _max_bytes()
    if len(data) > limit:
        raise ImageRejected(
            REASON_TOO_LARGE, f"{len(data)} > {limit} bytes"
        )

    allowed = _allowed_formats()
    ext_hint = ""
    if filename and "." in filename:
        ext_hint = filename.rsplit(".", 1)[-1].lower()
    elif content_type.startswith("image/"):
        ext_hint = content_type.split("/", 1)[1].lower()

    # 先按声明/扩展名快速判断，再以**实际解码结果**为准——
    # 用户把 .png 改名成 .jpg 传上来的情况很常见，只信声明会误判。
    if ext_hint and ext_hint not in allowed:
        raise ImageRejected(
            REASON_FORMAT, f"声明格式 {ext_hint} 不在白名单 {allowed}"
        )

    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 无 Pillow 视为不可解码
        raise ImageRejected(REASON_DECODE, f"Pillow 不可用：{type(exc).__name__}") from exc

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            img.verify()
    except Exception as exc:  # noqa: BLE001 - 解码失败统一归一类
        raise ImageRejected(REASON_DECODE, type(exc).__name__) from exc

    ext = _PIL_FORMAT_EXT.get(fmt, "")
    if ext not in allowed:
        raise ImageRejected(
            REASON_FORMAT, f"实际格式 {fmt or '未知'} 不在白名单 {allowed}"
        )

    return ImageInfo(
        width=int(width),
        height=int(height),
        format=fmt,
        ext=ext,
        content_type=content_type or _EXT_MIME.get(ext, f"image/{ext}"),
        size_bytes=len(data),
    )


@dataclass(slots=True)
class RouteResult:
    """五路分流结果（含分类器原始信息）。"""

    route: str
    confidence: float
    description: str = ""
    labels: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转字典。"""
        return {
            "route": self.route,
            "confidence": round(self.confidence, 3),
            "description": self.description,
            "labels": self.labels,
            "degraded": list(self.degraded),
        }


async def route_image(
    data: bytes,
    *,
    ocr_text: str = "",
    content_type: str = "image/png",
) -> RouteResult:
    """调分类器判定五路之一。

    分类器**保证**返回一个合法路由；这里再兜一层：万一实现返回了不认识的
    路由名（比如远端模型乱答），归 `MEME_IRRELEVANT` 而不是让上层 KeyError。
    """
    classifier = get_image_classifier()
    try:
        result = await classifier.classify(
            data, ocr_text=ocr_text, content_type=content_type
        )
    except Exception as exc:  # noqa: BLE001 - 分类器崩了也不能让整轮失败
        logger.warning("图片分类失败：%s", type(exc).__name__)
        return RouteResult(
            route=ImageRoute.MEME_IRRELEVANT.value,
            confidence=0.0,
            degraded=["VLM_CALL_FAILED"],
        )

    route = str(getattr(result, "route", "") or "")
    valid = {r.value for r in ImageRoute}
    degraded = list(getattr(result, "degraded", []) or [])
    if route not in valid:
        degraded.append("VLM_UNKNOWN_ROUTE")
        route = ImageRoute.MEME_IRRELEVANT.value

    return RouteResult(
        route=route,
        confidence=float(getattr(result, "confidence", 0.0) or 0.0),
        description=str(getattr(result, "description", "") or ""),
        labels=list(getattr(result, "labels", []) or []),
        degraded=degraded,
    )


__all__ = [
    "ImageInfo",
    "ImageRejected",
    "REASON_DECODE",
    "REASON_EMPTY",
    "REASON_FORMAT",
    "REASON_TOO_LARGE",
    "RouteResult",
    "route_image",
    "validate_image",
]
