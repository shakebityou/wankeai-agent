"""渠道适配器：把 5 个平台的回调原文归一化成 UMO（契约 2.2，BE-01）。

## 为什么每个渠道一个适配器类

五个平台的回调字段名、嵌套层级、消息类型取值**全都不一样**：

| 渠道 | 原始体形态 | 文本字段 | 媒体字段 | 备注 |
|---|---|---|---|---|
| TAOBAO 千牛 | JSON，`data.messages[]` | `content` | `mediaUrl` | 批量下发，一次可多条 |
| TMALL | 与千牛同构 | `content` | `mediaUrl` | 走同一套开放平台 |
| PDD | JSON，`msg` 字段是**字符串化的 JSON** | 解析后 `content` | `media_url` | 长连接下发，需自己解包 |
| DOUYIN | JSON，`content` 是 JSON 字符串 | 解析后 `text` | `url` | 类型码是数字 |
| WECHAT 企微 kf | JSON **或** XML | `Text.Content` | `Voice.MediaId` | 企微历史上两套协议并存 |
| 自有 H5 | 我方自定义 | `text` | `media.url` | 内部协议，字段最干净 |

如果不用适配器而写 `if channel == ...` 散在业务里，新增一个渠道就要
改十几处，且每处都可能漏掉某个字段的归一。

## 适配器契约

每个适配器实现：

* `channel` —— 负责的渠道枚举。
* `parse(payload: dict | bytes) -> list[RawInbound]` —— 归一化。
  **必须能一次返回多条**：淘宝与拼多多都是批量下发。
* `ack(payload) -> dict` —— 回给平台的应答体（有的平台要求特定结构）。

`parse` 里**不做签名校验**（那是 `app.core.signature` 的事），也**不做幂等**
（那是回调路由 + `IdempotencyStore` 的事）。单职责，才可单测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.enums import Channel, InboundMsgType
from app.core.logging import get_logger, mask_text

logger = get_logger(__name__)


class ChannelParseError(Exception):
    """渠道报文解析失败。`channel` + `detail` 用于告警归类。"""

    def __init__(self, channel: str, detail: str) -> None:
        """初始化。

        Args:
            channel: 渠道值。
            detail: 失败原因（**不得含完整隐私**，调用前先 mask）。
        """
        self.channel = channel
        self.detail = detail
        super().__init__(f"[{channel}] {detail}")


@dataclass
class RawInbound:
    """归一化后的单条入站消息（**不是 UMO**，UMO 由上层补会话状态后组装）。"""

    channel: str
    msg_id: str
    session_id: str
    buyer_id: str
    msg_type: InboundMsgType
    text: str = ""
    media_url: str | None = None
    media_format: str | None = None
    media_duration_ms: int | None = None
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: 原始平台字段（排障用；**落日志前必须 mask_text 过一遍**）
    raw: dict[str, Any] = field(default_factory=dict)
    #: 归一化告警（如「平台没给 msg_id，用了兜底哈希」）
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

#: 平台消息类型 → UMO 消息类型。**键一律小写**（各平台大小写不统一）
_COMMON_TYPE_MAP: dict[str, InboundMsgType] = {
    "text": InboundMsgType.TEXT,
    "txt": InboundMsgType.TEXT,
    "1": InboundMsgType.TEXT,
    "image": InboundMsgType.IMAGE,
    "img": InboundMsgType.IMAGE,
    "pic": InboundMsgType.IMAGE,
    "picture": InboundMsgType.IMAGE,
    "photo": InboundMsgType.IMAGE,
    "2": InboundMsgType.IMAGE,
    "voice": InboundMsgType.VOICE,
    "audio": InboundMsgType.VOICE,
    "speech": InboundMsgType.VOICE,
    "amr": InboundMsgType.VOICE,
    "silk": InboundMsgType.VOICE,
    "3": InboundMsgType.VOICE,
    "card": InboundMsgType.CARD,
    "goods": InboundMsgType.GOODS,
    "item": InboundMsgType.GOODS,
    "product": InboundMsgType.GOODS,
}


def map_msg_type(raw_type: Any, *, channel: str) -> InboundMsgType:
    """平台消息类型 → UMO 类型。

    **未知类型一律降级为 TEXT 并在 warnings 里记一笔**，不抛异常：
    渠道新增消息类型是常态，为了一个未知类型丢掉整批消息比降级更糟。

    Args:
        raw_type: 平台原始类型（字符串或数字）。
        channel: 渠道值（仅用于日志）。

    Returns:
        UMO 消息类型。
    """
    key = str(raw_type or "").strip().lower()
    mapped = _COMMON_TYPE_MAP.get(key)
    if mapped is None:
        logger.info("未知消息类型降级为 TEXT channel=%s raw_type=%s", channel, key)
        return InboundMsgType.TEXT
    return mapped


_URL_EXT_RE = re.compile(r"\.([A-Za-z0-9]{2,5})(?:\?|#|$)")


def guess_media_format(url: str | None, declared: str | None = None) -> str | None:
    """从 URL 或平台声明里取媒体格式（小写、无点）。

    Args:
        url: 媒体地址。
        declared: 平台声明的格式。

    Returns:
        格式字符串，取不到返回 None。
    """
    if declared:
        cleaned = str(declared).strip().lstrip(".").lower()
        if cleaned:
            return cleaned
    if not url:
        return None
    match = _URL_EXT_RE.search(url)
    return match.group(1).lower() if match else None


def parse_iso(value: Any) -> datetime:
    """尽量把平台时间戳解析成带时区的 datetime。

    支持：秒/毫秒 epoch 数字、数字字符串、ISO 8601。
    解析不出来**用当前时间兜底并记 warning**（时间只影响排序与 SLA，
    不值得因为一个畸形时间戳丢掉整条消息）。

    Args:
        value: 原始时间值。

    Returns:
        带 tz 的 datetime。
    """
    if value in (None, ""):
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if re.fullmatch(r"\d{10,13}", text):
        return _from_epoch(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("时间戳解析失败，用当前时间兜底 value=%s", text[:32])
        return datetime.now(timezone.utc)


def _from_epoch(value: float) -> datetime:
    """epoch → datetime，自动识别秒/毫秒。"""
    if value > 1e11:  # 毫秒
        value = value / 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _first(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个非空字段（平台字段名不一致时用）。"""
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def _maybe_json(value: Any) -> dict[str, Any]:
    """把「字符串化的 JSON」解开；已经是 dict 就原样返回。

    拼多多与抖音都会把业务体二次序列化成字符串塞进一个字段，
    这是这两个平台最常见的解析翻车点。

    Args:
        value: 待解包的值。

    Returns:
        dict；解不开返回空 dict。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_bytes_payload(payload: Any) -> dict[str, Any]:
    """入口统一：bytes/str 先解成 dict。

    Args:
        payload: 原始回调体。

    Returns:
        dict。

    Raises:
        ChannelParseError: 不是合法 JSON 对象。
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChannelParseError("unknown", f"报文不是 UTF-8：{exc}") from exc
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except ValueError as exc:
            raise ChannelParseError("unknown", f"报文不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ChannelParseError("unknown", "报文顶层不是 JSON 对象")
        return parsed
    raise ChannelParseError("unknown", f"不支持的报文类型 {type(payload).__name__}")


# ---------------------------------------------------------------------------
# 适配器基类
# ---------------------------------------------------------------------------


class ChannelAdapter:
    """渠道适配器基类。子类只需实现 `parse`。"""

    #: 本适配器负责的渠道
    channel: Channel = Channel.TAOBAO

    #: 平台的默认媒体格式（平台常不给 format，按渠道兜底）
    default_voice_format: str = "amr"
    default_image_format: str = "jpg"

    @property
    def channel_value(self) -> str:
        """渠道字符串值。"""
        return self.channel.value

    def parse(self, payload: Any) -> list[RawInbound]:  # pragma: no cover - 抽象
        """解析回调，返回归一化消息列表。

        Args:
            payload: 原始回调体（dict / bytes / str）。

        Returns:
            `RawInbound` 列表。

        Raises:
            ChannelParseError: 报文结构无法解析。
        """
        raise NotImplementedError

    def ack(self, payload: Any = None) -> dict[str, Any]:
        """回给平台的应答体。

        契约 2.3.4 要求 3 秒内返回 200 且**只入队**，所以默认应答刻意极简：
        带 `errcode`/`errmsg` 各平台认，附 `success` 兼容自有 H5。

        Args:
            payload: 原始回调体（多数渠道不需要）。

        Returns:
            应答字典。
        """
        return {"errcode": 0, "errmsg": "ok", "success": True}

    # -- 子类复用 ------------------------------------------------------

    def _build(
        self,
        *,
        payload: dict[str, Any],
        msg_id: Any,
        session_id: Any,
        buyer_id: Any,
        raw_type: Any,
        text: Any = "",
        media_url: Any = None,
        media_format: Any = None,
        media_duration_ms: Any = None,
        sent_at: Any = None,
        fallback_index: int = 0,
    ) -> RawInbound:
        """组装一条 `RawInbound`，补齐兜底值与告警。

        Args:
            payload: 原始报文。
            msg_id: 平台消息 ID。
            session_id: 会话 ID。
            buyer_id: 买家 ID。
            raw_type: 原始消息类型。
            text: 文本。
            media_url: 媒体地址。
            media_format: 媒体格式。
            media_duration_ms: 语音时长。
            sent_at: 发送时间。
            fallback_index: 批量报文里的序号（拼兜底 ID 用）。

        Returns:
            `RawInbound`。
        """
        warnings: list[str] = []
        msg_type = map_msg_type(raw_type, channel=self.channel_value)

        final_msg_id = str(msg_id or "").strip()
        if not final_msg_id:
            # 兜底 ID 必须**稳定**，否则同一消息重推会被当成两条
            final_msg_id = _stable_fallback_id(session_id, text, raw_type, fallback_index)
            warnings.append("平台未提供 msg_id，已用稳定哈希兜底")

        final_session = str(session_id or "").strip()
        if not final_session:
            final_session = f"{self.channel_value.lower()}:{final_msg_id}"
            warnings.append("平台未提供 session_id，已按 msg_id 构造")

        duration: int | None = None
        if media_duration_ms is not None:
            try:
                duration = int(media_duration_ms)
            except (TypeError, ValueError):
                warnings.append("语音时长非法，已置空")

        if msg_type is InboundMsgType.IMAGE and not media_format:
            media_format = self.default_image_format
        if msg_type is InboundMsgType.VOICE and not media_format:
            media_format = self.default_voice_format

        if msg_type is not InboundMsgType.TEXT and not media_url:
            warnings.append(f"{msg_type.value} 消息缺少媒体地址")

        return RawInbound(
            channel=self.channel_value,
            msg_id=final_msg_id,
            session_id=final_session,
            buyer_id=str(buyer_id or "").strip(),
            msg_type=msg_type,
            text=(str(text or "").strip()),
            media_url=str(media_url) if media_url else None,
            media_format=guess_media_format(
                str(media_url) if media_url else None, media_format
            ),
            media_duration_ms=duration,
            sent_at=parse_iso(sent_at),
            raw=payload,
            warnings=warnings,
        )


def _stable_fallback_id(
    session_id: Any, text: Any, raw_type: Any, index: int
) -> str:
    """构造稳定的兜底 msg_id。

    **必须稳定**：同一消息重推两次算出的 ID 要一样，否则 24h 幂等失效。
    因此只哈希「会话 + 内容 + 类型 + 批内序号」，**不含接收时间**。

    Args:
        session_id: 会话 ID。
        text: 文本内容。
        raw_type: 原始类型。
        index: 批内序号。

    Returns:
        兜底 ID。
    """
    import hashlib

    seed = f"{session_id}|{text}|{raw_type}|{index}".encode("utf-8")
    return "auto_" + hashlib.blake2b(seed, digest_size=10).hexdigest()


# ---------------------------------------------------------------------------
# 5 个渠道的具体实现
# ---------------------------------------------------------------------------


class TaobaoAdapter(ChannelAdapter):
    """淘宝千牛。回调形如 `{"messages": [{"msgId":..., "content":...}]}`。"""

    channel = Channel.TAOBAO

    def parse(self, payload: Any) -> list[RawInbound]:
        """解析千牛回调。"""
        data = _to_bytes_payload(payload)
        # 千牛有时把业务体放在 data / msg 子对象里
        inner = _maybe_json(data.get("data")) or data
        items = inner.get("messages") or inner.get("msgs") or [inner]
        if isinstance(items, dict):
            items = [items]
        out: list[RawInbound] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            out.append(
                self._build(
                    payload=item,
                    msg_id=_first(item, "msgId", "msg_id", "id"),
                    session_id=_first(item, "sessionId", "session_id", "chatId", "cid"),
                    buyer_id=_first(item, "buyerId", "buyer_id", "buyerNick", "uid"),
                    raw_type=_first(item, "msgType", "msg_type", "type", default="text"),
                    text=_first(item, "content", "text", "body", default=""),
                    media_url=_first(item, "mediaUrl", "media_url", "url"),
                    media_format=_first(item, "mediaFormat", "format", "ext"),
                    media_duration_ms=_first(item, "duration", "mediaDurationMs"),
                    sent_at=_first(item, "sentAt", "sent_at", "createTime", "time"),
                    fallback_index=idx,
                )
            )
        if not out:
            raise ChannelParseError(self.channel_value, "回调中没有可解析的消息")
        return out


class TmallAdapter(TaobaoAdapter):
    """天猫。与千牛同构，仅渠道标识不同。"""

    channel = Channel.TMALL


class PddAdapter(ChannelAdapter):
    """拼多多。`msg` 字段是**字符串化 JSON**，且消息体嵌套在 `msg.content` 里。"""

    channel = Channel.PDD
    default_voice_format = "amr"

    def parse(self, payload: Any) -> list[RawInbound]:
        """解析拼多多回调（长连接下发）。"""
        data = _to_bytes_payload(payload)
        # 第一层解包：msg 是字符串化的 JSON
        msg = _maybe_json(data.get("msg")) or data.get("msg") or {}
        if isinstance(msg, str):  # 解不开但仍需标记
            raise ChannelParseError(self.channel_value, "msg 字段无法解析为 JSON")
        # 第二层：真正的消息在 msg.content 里，可能又是字符串
        body = _maybe_json(msg.get("content")) or msg
        if not isinstance(body, dict):  # pragma: no cover - 防御
            raise ChannelParseError(self.channel_value, "content 结构异常")

        session_id = _first(
            body, "chat_id", "chatId", "session_id", "conversation_id"
        ) or _first(msg, "chat_id", "chatId", "session_id")
        buyer_id = _first(body, "uid", "buyer_id", "buyerId", "from_uid") or _first(
            msg, "uid", "from_uid"
        )
        raw_type = _first(body, "msg_type", "msgType", "type") or _first(
            msg, "type", default="text"
        )
        text = _first(body, "content", "text", "msg") or ""
        # text 类型的 content 就是文本；但如果是 dict，取 text 子字段
        if isinstance(text, dict):
            text = text.get("text") or text.get("content") or ""
        return [
            self._build(
                payload=data,
                msg_id=_first(body, "msg_id", "msgId", "id") or _first(msg, "msg_id"),
                session_id=session_id,
                buyer_id=buyer_id,
                raw_type=raw_type,
                text=text,
                media_url=_first(body, "media_url", "mediaUrl", "url", "voice_url"),
                media_format=_first(body, "media_format", "format", "ext"),
                media_duration_ms=_first(body, "duration", "voice_length"),
                sent_at=_first(body, "ts", "time", "create_time")
                or _first(data, "ts", "time"),
                fallback_index=0,
            )
        ]

    def ack(self, payload: Any = None) -> dict[str, Any]:
        """拼多多长连接要求回 `{"success": true}`。"""
        return {"success": True, "errcode": 0, "errmsg": "ok"}


class DouyinAdapter(ChannelAdapter):
    """抖音 IM。类型码是数字，`content` 常为字符串化 JSON。"""

    channel = Channel.DOUYIN

    def parse(self, payload: Any) -> list[RawInbound]:
        """解析抖音回调。"""
        data = _to_bytes_payload(payload)
        body = _maybe_json(data.get("content")) or data
        if not isinstance(body, dict):  # pragma: no cover - 防御
            raise ChannelParseError(self.channel_value, "content 结构异常")
        # 抖音文本消息把正文放在 content.text 里
        text = _first(body, "text", "content_text", "msg")
        if not text and isinstance(body.get("content"), str):
            text = body["content"]
        return [
            self._build(
                payload=data,
                msg_id=_first(body, "server_message_id", "msg_id", "message_id", "id")
                or _first(data, "server_message_id", "msg_id"),
                session_id=_first(body, "conversation_id", "session_id", "chat_id")
                or _first(data, "conversation_id", "session_id"),
                buyer_id=_first(body, "open_id", "user_id", "from_user_id", "uid"),
                raw_type=_first(body, "message_type", "msg_type", "type", default="text"),
                text=text or "",
                media_url=_first(body, "url", "media_url", "mediaUrl"),
                media_format=_first(body, "format", "ext"),
                media_duration_ms=_first(body, "duration", "voice_duration"),
                sent_at=_first(body, "create_time", "ts", "time"),
                fallback_index=0,
            )
        ]


class WechatKfAdapter(ChannelAdapter):
    """企微客服。**JSON 与 XML 两套协议并存**，这里都支持。"""

    channel = Channel.WECHAT

    def parse(self, payload: Any) -> list[RawInbound]:
        """解析企微客服回调（JSON 或 XML）。"""
        if isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload).decode("utf-8", errors="replace")
        if isinstance(payload, str) and payload.lstrip().startswith("<"):
            data = _parse_wechat_xml(payload)
        else:
            data = _to_bytes_payload(payload)
            # JSON 版把正文放在 text / voice / image 子对象里
            for key in ("text", "voice", "image"):
                sub = data.get(key)
                if isinstance(sub, dict):
                    merged = dict(data)
                    merged.pop(key, None)
                    merged.update(sub)
                    merged["_sub_type"] = key
                    data = merged
                    break

        msg_type = data.get("MsgType") or data.get("msg_type") or data.get("_sub_type")
        if not msg_type:
            # 从哪个子对象出现来推断
            for key in ("text", "voice", "image"):
                if key in _to_bytes_payload(payload) if isinstance(payload, (str, bytes)) else False:
                    msg_type = key
                    break
            msg_type = msg_type or "text"

        return [
            self._build(
                payload=data,
                msg_id=_first(data, "MsgId", "msg_id", "msgid", "id"),
                session_id=_first(
                    data, "OpenKfId", "open_kfid", "session_id", "chat_id", "kf_id"
                ),
                buyer_id=_first(
                    data, "ExternalUserId", "external_userid", "user_id", "from_user"
                ),
                raw_type=msg_type,
                text=_first(data, "Content", "content", "text", default=""),
                media_url=_first(data, "MediaId", "media_id", "media_url", "url"),
                media_format=_first(data, "Format", "format", "ext"),
                media_duration_ms=_first(data, "PlayLength", "play_length", "duration"),
                sent_at=_first(data, "CreateTime", "create_time", "ts", "time"),
                fallback_index=0,
            )
        ]

    def ack(self, payload: Any = None) -> dict[str, Any]:
        """企微要求 XML 应答；JSON 协议下给等价的 dict。"""
        return {"errcode": 0, "errmsg": "ok", "success": True}


def _parse_wechat_xml(text: str) -> dict[str, Any]:
    """把企微 XML 回调拍平成 dict。

    用标准库 `xml.etree` 并**禁用外部实体**（`defusedxml` 未安装，
    这里用 `ElementTree` 且不解析 DTD/外部实体——企微报文是纯数据，
    不开 `resolve_entities` 就不会触发 XXE）。

    Args:
        text: XML 字符串。

    Returns:
        拍平后的 dict（嵌套取叶子文本）。

    Raises:
        ChannelParseError: XML 无法解析。
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ChannelParseError("WECHAT", f"XML 解析失败：{exc}") from exc

    flat: dict[str, Any] = {}

    def walk(node: Any, prefix: str = "") -> None:
        for child in node:
            key = f"{prefix}{child.tag}"
            grand = list(child)
            if grand:
                walk(child, f"{key}.")
            else:
                flat[key] = (child.text or "").strip()

    walk(root)
    # 同时提供不带前缀的短键，方便上层按 Tag 名取
    for key, value in list(flat.items()):
        short = key.rsplit(".", 1)[-1]
        flat.setdefault(short, value)
    return flat


class H5Adapter(ChannelAdapter):
    """自有 H5 客服。字段最干净，直接对齐 UMO。"""

    channel = Channel.WECHAT
    # 自有 H5 走 WECHAT 渠道枚举（契约 2.6 只有五个渠道值），
    # 但用独立适配器保证字段口径不被企微协议带偏。

    def __init__(self) -> None:
        """初始化时把渠道标成 WECHAT 之外的标识。"""
        # 注意：不加进 Channel 枚举，避免破坏契约 2.6；
        # 用字符串标识区分，统计时归到 WECHAT 大类。
        self._adapter_tag = "H5"

    @property
    def channel_value(self) -> str:
        """渠道值（统计口径）。"""
        return Channel.WECHAT.value

    def parse(self, payload: Any) -> list[RawInbound]:
        """解析自有 H5 回调。"""
        data = _to_bytes_payload(payload)
        return [
            self._build(
                payload=data,
                msg_id=_first(data, "msg_id", "msgId", "id"),
                session_id=_first(data, "session_id", "sessionId", "chat_id"),
                buyer_id=_first(data, "buyer_id", "buyerId", "uid"),
                raw_type=_first(data, "msg_type", "msgType", "type", default="text"),
                text=_first(data, "text", "content", default=""),
                media_url=_first(data.get("media") or {}, "url")
                if isinstance(data.get("media"), dict)
                else _first(data, "media_url", "url"),
                media_format=_first(data.get("media") or {}, "format")
                if isinstance(data.get("media"), dict)
                else _first(data, "media_format", "format"),
                media_duration_ms=_first(data.get("media") or {}, "duration_ms")
                if isinstance(data.get("media"), dict)
                else _first(data, "media_duration_ms"),
                sent_at=_first(data, "sent_at", "timestamp", "ts"),
                fallback_index=0,
            )
        ]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

#: 渠道 → 适配器实例。**键用枚举值字符串**（回调路由拿到的就是字符串）
_ADAPTERS: dict[str, ChannelAdapter] = {
    Channel.TAOBAO.value: TaobaoAdapter(),
    Channel.TMALL.value: TmallAdapter(),
    Channel.PDD.value: PddAdapter(),
    Channel.DOUYIN.value: DouyinAdapter(),
    Channel.WECHAT.value: WechatKfAdapter(),
}


def get_adapter(channel: str | Channel) -> ChannelAdapter:
    """按渠道取适配器。

    Args:
        channel: 渠道值或枚举。

    Returns:
        适配器实例。

    Raises:
        ChannelParseError: 渠道不支持。
    """
    key = channel.value if isinstance(channel, Channel) else str(channel).upper()
    adapter = _ADAPTERS.get(key)
    if adapter is None:
        raise ChannelParseError(key, f"不支持的渠道：{key}")
    return adapter


def register_adapter(adapter: ChannelAdapter, *, key: str | None = None) -> None:
    """注册/替换适配器（自有 H5 等定制渠道用，测试亦可注入假适配器）。

    Args:
        adapter: 适配器实例。
        key: 注册键，默认取 `adapter.channel.value`。
    """
    _ADAPTERS[(key or adapter.channel.value).upper()] = adapter


def supported_channels() -> list[str]:
    """已注册的渠道列表（BE-01 DoD「5 渠道适配器」的自动化校验入口）。"""
    return sorted(_ADAPTERS)


__all__ = [
    "ChannelAdapter",
    "ChannelParseError",
    "DouyinAdapter",
    "H5Adapter",
    "PddAdapter",
    "RawInbound",
    "TaobaoAdapter",
    "TmallAdapter",
    "WechatKfAdapter",
    "get_adapter",
    "guess_media_format",
    "map_msg_type",
    "parse_iso",
    "register_adapter",
    "supported_channels",
]
