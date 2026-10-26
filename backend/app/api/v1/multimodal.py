"""L2 多模态预处理路由（BE-10 / BE-11 / BE-12）。

`POST /internal/v1/multimodal/preprocess` —— 把**原始**语音/图片归一成
与纯文本路径**结构完全同构**的对象，交给 `POST /internal/v1/chat/turn`。

## 为什么单独开一个入口，而不是塞进 `/chat/turn`

`/chat/turn` 的入参是**已经归一化**的 UMO（`umo.normalized.text` 有值），
它的注释里写明「本接口不再重复做多模态处理，避免同一个文件被下载两次」。
多模态处理要下载媒体、跑 ffmpeg、调 ASR/OCR/VLM，耗时和失败模式与对话编排
完全不同，混在一起会让对话接口的 P99 被一个转码超时拖垮。所以拆成两个接口：

| 接口 | 输入 | 输出 | 耗时量级 |
| --- | --- | --- | --- |
| `/multimodal/preprocess` | 原始媒体 + 可选提示文字 | 归一化消息 | 百毫秒~秒级 |
| `/chat/turn` | 归一化 UMO | 回复 | 十毫秒~百毫秒 |

## 两种媒体来源

* `media_base64`：渠道回调已经把文件下载到本地时用（最常见）；
* `media_url`：只有 URL、由本服务负责下载时用（`file://` 与 `http(s)://`）。

下载失败属**预期分支**（BE-11 DoD ③），返回 200 + 兜底话术，不抛 500——
渠道侧看到 5xx 会重推，而重推同一个坏文件永远不会成功。
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import InternalAuthGuard
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.response import ok
from app.services.multimodal import get_pipeline

logger = get_logger(__name__)

router = APIRouter(
    prefix="/multimodal", tags=["multimodal"], dependencies=[InternalAuthGuard]
)

#: 单次请求体上限（媒体 base64 膨胀约 4/3，留出余量）。超过直接 400，
#: 不能让一个 50MB 的 base64 先把进程内存吃满再报错。
MAX_BASE64_CHARS = 16 * 1024 * 1024


class PreprocessRequest(BaseModel):
    """多模态预处理请求。

    Attributes:
        msg_type: `TEXT` / `VOICE` / `IMAGE`（`CARD`/`GOODS` 本期不接入）。
        text: 用户随媒体一起发的文字（图片场景下是**很强的分类证据**，
            例如「收到就是这样，有破损」）。
        media_base64: 媒体文件 base64（与 `media_url` 二选一）。
        media_url: 媒体地址（`file://` 或 `http(s)://`）。
        content_type: MIME，如 `audio/amr` / `image/png`。
        filename: 原始文件名，用于**推断**格式（`_guess_audio_format` 优先用它）。
        hotwords: ASR 热词表（商品名、SKU 名等）。
        save_media: 是否把原始媒体落盘（语音落盘后受 7 天生命周期约束）。
    """

    msg_type: str = Field(default="TEXT", description="TEXT / VOICE / IMAGE")
    text: str = Field(default="", max_length=4000, description="随媒体一起发的文字")
    media_base64: str = Field(default="", description="媒体 base64（与 media_url 二选一）")
    media_url: str = Field(default="", max_length=1000, description="媒体地址")
    content_type: str = Field(default="", max_length=100, description="MIME 类型")
    filename: str = Field(default="", max_length=255, description="原始文件名")
    hotwords: list[str] = Field(default_factory=list, description="ASR 热词表")
    save_media: bool = Field(default=True, description="是否落盘原始媒体")


class PreprocessResponse(BaseModel):
    """预处理结果（字段与文本路径同构，见 BE-11 DoD ①）。"""

    ok: bool
    msg_type: str
    text: str
    text_source: str
    confidence: float
    route: str | None = None
    order_no: str | None = None
    fallback_key: str | None = None
    fallback_text: str = ""
    degraded: list[str] = Field(default_factory=list)
    latency_ms: dict[str, int] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    media_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def _decode_media(payload: PreprocessRequest) -> bytes:
    """取媒体字节：base64 优先，其次 URL。"""
    if payload.media_base64:
        raw = payload.media_base64.strip()
        if len(raw) > MAX_BASE64_CHARS:
            raise ValidationError(
                f"媒体过大：base64 长度 {len(raw)} 超过上限 {MAX_BASE64_CHARS}"
            )
        # 允许前端直接粘 data URL（`data:image/png;base64,xxx`）
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("媒体 base64 解码失败") from exc

    if payload.media_url:
        return _read_url(payload.media_url)

    return b""


def _read_url(url: str) -> bytes:
    """读 `file://` 或 `http(s)://`。

    读不到就抛 `ValidationError`，由上层转成「下载失败」兜底话术——
    这里**不吞异常**，因为调用方需要区分「没给媒体」和「给了但读不到」。
    """
    if url.startswith("file://"):
        from pathlib import Path

        raw = url[7:]
        # 标准写法 `file:///D:/x` 取完前缀是 `/D:/x`，Windows 上要削掉这个前导
        # 斜杠才是有效盘符路径；POSIX 上 `/home/x` 必须保留，所以只在「斜杠 +
        # 盘符 + 冒号」时才削。
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        path = Path(raw)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ValidationError(f"媒体文件读取失败：{path.name}") from exc

    if url.startswith(("http://", "https://")):
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 网络异常统一归为下载失败
            raise ValidationError(f"媒体下载失败：{type(exc).__name__}") from exc

    raise ValidationError(f"不支持的媒体地址：{url[:40]}")


@router.post(
    "/preprocess",
    summary="多模态预处理：语音转写 / 图片五路分流 / 订单号提取",
    response_model=None,
)
def preprocess(payload: PreprocessRequest) -> dict[str, Any]:
    """把原始媒体归一成统一消息对象。

    Returns:
        统一响应包装，`data` 为 `PreprocessResponse`。**业务失败（转码失败、
        图片过大、识别不出订单号）也返回 200**，`ok=false` + `fallback_text`，
        由调用方把兜底话术发回用户。

    Raises:
        ValidationError: 请求本身不合法（base64 解不开、媒体过大、地址不支持）。
    """
    media = _decode_media(payload)

    if payload.msg_type.upper() in {"VOICE", "IMAGE"} and not media:
        # 媒体没给：这是调用方的编程错误，不是用户问题，必须 400。
        raise ValidationError(
            f"{payload.msg_type.upper()} 消息必须提供 media_base64 或 media_url"
        )

    result = get_pipeline().preprocess(
        msg_type=payload.msg_type.upper(),
        text=payload.text,
        media=media or None,
        content_type=payload.content_type,
        filename=payload.filename,
        hotwords=tuple(payload.hotwords),
        save_media=payload.save_media,
    )
    data = result.to_dict()
    return ok(data)


@router.get("/health", summary="多模态预处理健康检查")
def multimodal_health() -> dict[str, Any]:
    """确认流水线与三个适配器可实例化。"""
    pipe = get_pipeline()
    return ok(
        {
            "pipeline": "ready",
            "asr": getattr(pipe.asr, "name", "unknown"),
            "ocr": getattr(pipe.ocr, "name", "unknown"),
            "media_store": type(pipe.store).__name__,
        }
    )


__all__ = ["PreprocessRequest", "PreprocessResponse", "preprocess", "router"]
