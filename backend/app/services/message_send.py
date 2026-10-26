"""出站消息发送服务（BE-02，契约 A 2.3.1 第 6 项 / 说明书第 6 章）。

## 四条 DoD 与实现位置

| DoD | 实现位置 |
| --- | --- |
| ① 发送成功率 ≥ 99.9%（含重试） | `MessageSendService.stats()` 按「请求 / 实际发送 / 成功」三个口径统计，见下 |
| ② 同 `msg_id` 提交 3 次只发 1 条 | `IdempotencyStore`（key=`outbound:<msg_id>`，TTL 24h）+ **结果缓存** |
| ③ 出站内容零外链（命中即测试失败） | `OutboundGate.enforce()` 对**每个分段**逐段扫描；`assert_no_outbound_link` 为验收入口 |
| ④ 长消息自动分段（单条 ≤ 120 字） | `split_message()`，按句切、不硬切、不在数字/单位中间切 |

## 为什么「成功率」要分三个口径

只报一个数字会掩盖问题：

* **投递成功率** = `sent / attempts`（实际发出的请求里成功几次）——衡量通道质量；
* **消息成功率** = `delivered / submitted`（提交的消息最终发出几条）——衡量业务结果；
* **一次通过率** = `first_try_ok / submitted`——衡量通道稳定性，它才是 99.9% 的
  真实含义（重试能救回来，但重试本身是有成本的）。

DoD 的 ≥ 99.9% 按**消息成功率**验收，另外两个作为运维观测指标同时输出。

## 为什么要「分段」而不是「截断」

LLM 出口已经做了 ≤ 120 字截断（`llm_gateway.truncate_reply`），但那只覆盖
生成路径；工具回复（订单五要素、物流七种状态）是**拼装**出来的，很容易超长，
而且截断会丢掉「订单状态」这种关键字段。分段保证内容完整，用户看到的是
两条连续消息，而不是一条「…」。

## 分段的三个约束（都是踩过的真实问题）

1. **不硬切**：120 字处硬切会把「7 天无理由」切成「7 天无理」，把
   `2025****0001` 切成 `2025****00`。切点优先找句末标点，其次逗号，
   都不行才在**非数字非字母**的边界切。
2. **段数有上限（只做提示，不丢内容）**：一条回复切成 10 段，用户体验是刷屏。
   超过 `MAX_SEGMENTS` 时仍在响应里标 `overflow=True`，让坐席台提示运营
   「这条话术偏长，建议精简」——但**绝不截断内容**，因为损失的关键字段
   （订单状态、时效承诺）比刷屏更糟。
3. **每段都要过合规闸**：分段后单段可能只剩半句话，外链正则反而更容易
   漏判（如 `https://` 被切到两段里）。所以**先过闸再分段**，且分段后
   逐段复检（`enforce_segments`）。

## 失败消息落表与告警

DoD 与说明书附录 C 都要求「仍失败则记录失败消息并告警，坐席台可见失败标记」。
失败消息落在 `FailedOutboundMessage` 表（不进内存字典），原因：

* 坐席台是**另一个进程/另一个会话**在读，内存态不可见；
* 失败消息需要留存与重放，重启不能丢；
* 合规审计要求「写接口操作人 + 时间」留存 ≥ 1 年。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.adapters.merchant_message import (
    MerchantChannelError,
    MerchantSendResult,
    MessageChannelPort,
    get_message_channel,
)
from app.compliance.outbound_gate import OutboundGate, get_outbound_gate
from app.core.config import get_settings
from app.core.enums import Channel, OutboundMsgType, Sender
from app.core.errors import MerchantErrorCode
from app.core.idempotency import IdempotencyStore
from app.core.logging import get_logger, mask_text
from app.core.trace import get_trace_id

logger = get_logger(__name__)

#: 出站幂等键前缀。与 `core/idempotency.py` 的口径一致（`outbound:{msg_id}`）
OUTBOUND_IDEMPOTENCY_PREFIX = "outbound"

#: 分段上限。仅用于「偏长提示」，超出的内容**照发不误**（不截断）
MAX_SEGMENTS = 4

#: 句末标点（优先切点）
_SENTENCE_END = "。！？!?；;\n"
#: 次级切点
_SOFT_BREAK = "，,、：:）) "

#: 允许出站的消息类型（商家 0.8 节：CARD/GOODS 本期不接入）
ALLOWED_OUTBOUND_TYPES: frozenset[OutboundMsgType] = frozenset(
    {OutboundMsgType.TEXT, OutboundMsgType.IMAGE}
)

#: 禁止发送的类型（出现即拒绝，不是降级）
FORBIDDEN_OUTBOUND_TYPES: frozenset[OutboundMsgType] = frozenset(
    {OutboundMsgType.CARD, OutboundMsgType.GOODS}
)


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


def _channel_value(channel: Channel | str) -> str:
    """取渠道字面值（`OutboundMessage` 的 `channel` 允许是枚举或裸字符串）。

    Args:
        channel: 渠道枚举或字符串。

    Returns:
        `TAOBAO` 这类大写字面值。
    """
    if isinstance(channel, Channel):
        return channel.value
    return str(channel).upper()


def _sender_value(sender: Sender | str) -> str:
    """取发送方字面值（同上）。"""
    if isinstance(sender, Sender):
        return sender.value
    return str(sender).upper()


def _msg_type_value(msg_type: OutboundMsgType | str) -> str:
    """取消息类型字面值（同上）。"""
    if isinstance(msg_type, OutboundMsgType):
        return msg_type.value
    return str(msg_type).upper()


def _find_cut(text: str, limit: int) -> int:
    """在 `text[:limit]` 里找最佳切点（返回切点下标，切点左侧归本段）。

    优先级：句末标点 > 逗号类 > 空白 > 非数字字母边界 > 硬切。

    Args:
        text: 原文。
        limit: 本段最大字数。

    Returns:
        切点下标，`1 <= 返回值 <= limit`。
    """
    window = text[:limit]
    # 1) 句末标点：取最后一个，保留标点在本段
    for idx in range(len(window) - 1, max(0, len(window) // 3), -1):
        if window[idx] in _SENTENCE_END:
            return idx + 1
    # 2) 次级标点
    for idx in range(len(window) - 1, max(0, len(window) // 4), -1):
        if window[idx] in _SOFT_BREAK:
            return idx + 1
    # 3) 非数字非字母边界：避免把订单号 / 金额 / 规格切开
    for idx in range(len(window) - 1, max(0, len(window) // 4), -1):
        prev, cur = window[idx - 1], window[idx]
        if (prev.isdigit() or prev.isascii() and prev.isalpha()) and (
            cur.isdigit() or (cur.isascii() and cur.isalpha())
        ):
            continue
        return idx
    # 4) 兜底硬切
    return limit


def split_message(
    text: str | None, *, max_chars: int | None = None, soft_behind: bool = False
) -> list[str]:
    """把长文本切成若干段，**每段 ≤ `max_chars` 字**。

    Args:
        text: 原文。空文本返回空列表。
        max_chars: 每段字数上限，默认取配置 `outbound_max_chars_per_segment`（120）。
        soft_behind: 是否把标点留在**上一段**。段中（非末段）留标点是口语化的
            自然断句；**末段**必须把标点留在段内，否则「7 天无理由。」会变成
            「7 天无理由」，话术以逗号收尾。

    Returns:
        分段列表（已 strip、去掉空段）。**不丢弃任何字符**。
    """
    limit = max_chars or get_settings().outbound_max_chars_per_segment
    remaining = (text or "").strip()
    if not remaining:
        return []
    if len(remaining) <= limit:
        return [remaining]

    segments: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            segments.append(remaining)
            break
        cut = _find_cut(remaining, limit)
        if soft_behind and cut == limit:
            # 切点落在标点「之后」（如「，」位于 cut-1），把标点让给上一段。
            # 前置条件 `cut == limit`：只有「窗口正好被标点收尾」时让标点才是
            # 自然的（「…亲肤材质，」）；若 `_find_cut` 已回退过，让标点会白白
            # 损失一个字的容量，而段内本就以标点结尾。
            for offset in (1, 2):
                pos = cut - offset
                if pos >= 1 and remaining[pos] in _SENTENCE_END + _SOFT_BREAK:
                    cut = pos + 1
                    break
        piece = remaining[:cut].strip()
        if not piece:  # pragma: no cover - _find_cut 保证至少切 1 个字符
            piece = remaining[:cut]
        segments.append(piece)
        # **按原文位置继续，不要 strip**：strip 会吃掉切点两侧的空白，
        # 使剩余串的位置前移、`cut` 语义错位（实测导致「拼回去等于原文」
        # 的断言失败，以及分段长度算错）
        remaining = remaining[cut:]
    return segments


# ---------------------------------------------------------------------------
# 请求 / 响应模型（纯 dataclass，路由层再转 Pydantic）
# ---------------------------------------------------------------------------


@dataclass
class OutboundMessage:
    """一条待发送的出站消息（商家 6.3 节请求体 + 内部字段）。"""

    msg_id: str
    session_id: str
    buyer_id: str
    channel: Channel
    content: str
    sender: Sender = Sender.BOT
    msg_type: OutboundMsgType = OutboundMsgType.TEXT
    reply_to: str | None = None
    #: 本会话已核验的订单号（白名单，不脱敏）
    allow_order_nos: set[str] = field(default_factory=set)


@dataclass
class SegmentResult:
    """单个分段的发送结果。

    Attributes:
        segment_id: 分段级 `msg_id`（平台侧幂等键）。
        content: 实际发出的文本（**已过合规闸**）。
        attempts: 尝试次数（含首次）。
        ok: 是否发出。
        platform_msg_id: 平台侧 ID。
        error_code: 商家错误码。
        error_message: 失败描述（已脱敏）。
        blocked: 是否被合规闸拦截替换。
        retries: 重试明细（便于排障与 DoD 断言）。
    """

    segment_id: str
    content: str = ""
    attempts: int = 0
    ok: bool = False
    platform_msg_id: str = ""
    error_code: int | None = None
    error_message: str = ""
    blocked: bool = False
    retries: list[dict[str, Any]] = field(default_factory=list)
    #: 落失败表后的记录 ID（坐席台按它重发）
    failed_record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """导出为可序列化字典（不含原文，只给长度，避免日志夹带隐私）。"""
        return {
            "segment_id": self.segment_id,
            "attempts": self.attempts,
            "ok": self.ok,
            "platform_msg_id": self.platform_msg_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "blocked": self.blocked,
            "failed_record_id": self.failed_record_id,
            "content_length": len(self.content),
            "retries": list(self.retries),
        }


@dataclass
class SendOutcome:
    """一次发送（可能含多段）的完整结果。"""

    msg_id: str
    ok: bool = False
    #: 本次是重复提交（复用首次结果，**未再次发送**）
    duplicate: bool = False
    #: 首次提交时是否已在处理中（并发提交同一 msg_id）
    in_progress: bool = False
    #: 实际发送的分段
    segments: list[SegmentResult] = field(default_factory=list)
    blocked: bool = False
    latency_ms: float = 0.0
    trace_id: str = ""
    #: 失败落表的 ID（非空表示可在坐席台「发送失败」列表看到）
    failed_record_id: str | None = None
    #: 分段数是否超过 `MAX_SEGMENTS`（「话术偏长」提示；**内容照发不截断**）
    overflow: bool = False

    @property
    def sent_count(self) -> int:
        """成功发出的段数。"""
        return sum(1 for s in self.segments if s.ok)

    def to_dict(self) -> dict[str, Any]:
        """导出为路由响应体。"""
        return {
            "msg_id": self.msg_id,
            "ok": self.ok,
            "duplicate": self.duplicate,
            "in_progress": self.in_progress,
            "blocked": self.blocked,
            "overflow": self.overflow,
            "sent_count": self.sent_count,
            "failed_record_id": self.failed_record_id,
            "segment_count": len(self.segments),
            "segments": [s.to_dict() for s in self.segments],
            "latency_ms": round(self.latency_ms, 2),
            "trace_id": self.trace_id,
        }


@dataclass
class SendStats:
    """发送统计（三个口径）。"""

    submitted: int = 0
    delivered: int = 0
    attempts: int = 0
    sent: int = 0
    first_try_ok: int = 0
    duplicates: int = 0
    failed: int = 0

    @property
    def message_success_rate(self) -> float:
        """消息成功率（DoD 口径）：`delivered / submitted`。"""
        return 1.0 if not self.submitted else self.delivered / self.submitted

    @property
    def delivery_success_rate(self) -> float:
        """投递成功率：`sent / attempts`。"""
        return 1.0 if not self.attempts else self.sent / self.attempts

    @property
    def first_try_rate(self) -> float:
        """一次通过率：`first_try_ok / submitted`。"""
        return 1.0 if not self.submitted else self.first_try_ok / self.submitted

    def to_dict(self) -> dict[str, Any]:
        """导出为字典（保留 6 位小数，便于对 99.9% 这类门槛做断言）。"""
        return {
            "submitted": self.submitted,
            "delivered": self.delivered,
            "attempts": self.attempts,
            "sent": self.sent,
            "first_try_ok": self.first_try_ok,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "message_success_rate": round(self.message_success_rate, 6),
            "delivery_success_rate": round(self.delivery_success_rate, 6),
            "first_try_rate": round(self.first_try_rate, 6),
        }


# ---------------------------------------------------------------------------
# 失败消息落表
# ---------------------------------------------------------------------------

_ENGINE_OVERRIDE: Any = None

_FAILED_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS outbound_failed_messages (
    record_id VARCHAR(64) PRIMARY KEY,
    msg_id VARCHAR(128) NOT NULL,
    segment_id VARCHAR(160) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    buyer_id VARCHAR(128) NOT NULL,
    channel VARCHAR(16) NOT NULL,
    msg_type VARCHAR(16) NOT NULL,
    sender VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    error_code INTEGER,
    error_message VARCHAR(512),
    attempts INTEGER NOT NULL DEFAULT 0,
    trace_id VARCHAR(64),
    created_at VARCHAR(32) NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
)
"""


def _engine() -> Any:
    """取失败表用的引擎（测试可替换）。

    注意：**每次现取 `get_engine()`，不要在模块级缓存**——`get_engine()`
    本身就是全局单例，而测试会通过 `reset_engine()` 换掉它。一旦在这里
    缓存了旧引擎，测试之间就会出现「写进 A 库、读的是 B 库」的脏失败
    （交接文档 4.5 记过同类坑：只换 `_ENGINE` 不换 `_SESSION_FACTORY`）。
    """
    if _ENGINE_OVERRIDE is not None:
        return _ENGINE_OVERRIDE
    from app.db.engine import get_engine

    return get_engine()


def set_failed_table_engine(engine: Any) -> None:  # noqa: ANN401 - 测试注入
    """替换失败表引擎（**测试专用**）。传 None 恢复默认。"""
    global _ENGINE_OVERRIDE  # noqa: PLW0603
    _ENGINE_OVERRIDE = engine


class FailedMessageStore:
    """失败消息表（BE-02 DoD：失败消息落表并告警，坐席台可见失败标记）。

    用原生 SQL 而非 ORM 模型，原因：这张表是**运维表**，不进业务迁移链
    （业务表 14 张已冻结，见交接文档 3.3）。它由本模块幂等建表，
    任何后端（SQLite / SQL Server / PostgreSQL）都能跑。
    """

    def __init__(self) -> None:
        self._ready = False
        self._lock = threading.Lock()

    def _ensure(self, conn: Any) -> None:
        """幂等建表（每进程一次）。"""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            from sqlalchemy import text

            conn.execute(text(_FAILED_TABLE_DDL))
            self._ready = True

    def record(
        self,
        *,
        msg_id: str,
        segment_id: str,
        session_id: str,
        buyer_id: str,
        channel: str,
        msg_type: str,
        sender: str,
        content: str,
        error_code: int | None,
        error_message: str,
        attempts: int,
        trace_id: str,
    ) -> str:
        """写入一条失败记录，返回 `record_id`。

        Args:
            msg_id: 原始消息 ID。
            segment_id: 分段级 ID。
            session_id: 会话 ID。
            buyer_id: 买家 ID（脱敏后稳定 ID）。
            channel: 渠道值。
            msg_type: 消息类型。
            sender: 发送方。
            content: 未发出的内容（**含用户可见明文，故表需按合规要求留存**）。
            error_code: 商家错误码。
            error_message: 脱敏后的错误描述。
            attempts: 尝试次数。
            trace_id: 全链路 ID。

        Returns:
            `record_id`。
        """
        from sqlalchemy import text

        record_id = f"ofm_{uuid.uuid4().hex[:16]}"
        sql = text(
            "INSERT INTO outbound_failed_messages (record_id, msg_id, segment_id, "
            "session_id, buyer_id, channel, msg_type, sender, content, error_code, "
            "error_message, attempts, trace_id, created_at, resolved) VALUES "
            "(:record_id, :msg_id, :segment_id, :session_id, :buyer_id, :channel, "
            ":msg_type, :sender, :content, :error_code, :error_message, :attempts, "
            ":trace_id, :created_at, 0)"
        )
        params = {
            "record_id": record_id,
            "msg_id": msg_id,
            "segment_id": segment_id,
            "session_id": session_id,
            "buyer_id": buyer_id,
            "channel": channel,
            "msg_type": msg_type,
            "sender": sender,
            "content": content,
            "error_code": error_code,
            "error_message": mask_text(error_message)[:512],
            "attempts": attempts,
            "trace_id": trace_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        engine = _engine()
        with engine.begin() as conn:
            self._ensure(conn)
            conn.execute(sql, params)
        return record_id

    def list_failed(
        self, *, session_id: str | None = None, unresolved_only: bool = True, limit: int = 50
    ) -> list[dict[str, Any]]:
        """列出失败记录（坐席台「发送失败」列表的数据源）。"""
        from sqlalchemy import text

        where = ["1=1"]
        params: dict[str, Any] = {"limit": limit}
        if session_id:
            where.append("session_id = :session_id")
            params["session_id"] = session_id
        if unresolved_only:
            where.append("resolved = 0")
        sql = text(
            "SELECT record_id, msg_id, segment_id, session_id, channel, msg_type, "
            "sender, content, error_code, error_message, attempts, trace_id, "
            "created_at FROM outbound_failed_messages WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC LIMIT :limit"
        )
        engine = _engine()
        try:
            with engine.begin() as conn:
                self._ensure(conn)
                rows = conn.execute(sql, params).mappings().all()
        except Exception:  # noqa: BLE001 - 表还没建（从未失败过）时返回空
            return []
        return [dict(r) for r in rows]

    def mark_resolved(self, record_id: str) -> bool:
        """标记已处理（重放成功后调用）。"""
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as conn:
            self._ensure(conn)
            result = conn.execute(
                text(
                    "UPDATE outbound_failed_messages SET resolved = 1 "
                    "WHERE record_id = :rid"
                ),
                {"rid": record_id},
            )
        return bool(result.rowcount)


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class MessageSendService:
    """出站消息发送服务。

    Attributes:
        channel_port: 出站通道（默认全局单例）。
        gate: 合规闸（默认全局单例）。
        store: 失败消息表。
    """

    def __init__(
        self,
        *,
        channel_port: MessageChannelPort | None = None,
        gate: OutboundGate | None = None,
        idempotency: IdempotencyStore | None = None,
        failed_store: FailedMessageStore | None = None,
        sleep: Any = None,
    ) -> None:
        self._channel = channel_port
        self._gate = gate
        self._idem = idempotency
        self.failed_store = failed_store or FailedMessageStore()
        self._stats = SendStats()
        self._stats_lock = threading.Lock()
        #: 可替换的 sleep（测试注入 0 延迟）
        self._sleep = sleep or time.sleep

    # -- 惰性依赖 -----------------------------------------------------------

    @property
    def channel(self) -> MessageChannelPort:
        """出站通道。"""
        if self._channel is None:
            self._channel = get_message_channel()
        return self._channel

    @property
    def gate(self) -> OutboundGate:
        """合规闸。"""
        if self._gate is None:
            self._gate = get_outbound_gate()
        return self._gate

    @property
    def idempotency(self) -> IdempotencyStore:
        """幂等存储。"""
        if self._idem is None:
            settings = get_settings()
            self._idem = IdempotencyStore(ttl=settings.outbound_idempotency_ttl_seconds)
        return self._idem

    def stats(self) -> dict[str, Any]:
        """当前统计（三个口径）。"""
        with self._stats_lock:
            return self._stats.to_dict()

    def reset_stats(self) -> None:
        """清零统计（测试用）。"""
        with self._stats_lock:
            self._stats = SendStats()

    # -- 幂等 ---------------------------------------------------------------

    @staticmethod
    def idempotency_key(msg_id: str, channel: Channel | str = "") -> str:
        """出站幂等键：`outbound:<CHANNEL>:<msg_id>`（不用 `build_key` 的哈希形式）。

        直接拼而不是哈希，是为了让运维在 Redis 里能按前缀 `outbound:TAOBAO:`
        批量排查；出站 `msg_id` 是我方生成的 UUID 串，长度与字符集都受控。

        **必须带渠道**：五个平台的 `msg_id` 空间彼此独立，淘宝的 `123` 与
        拼多多的 `123` 是两条不同的消息，不加渠道会互相误判成重复。

        Args:
            msg_id: 消息 ID。
            channel: 渠道值；为空时键里省略渠道段（兼容旧调用）。

        Returns:
            幂等键。
        """
        if not channel:
            return f"{OUTBOUND_IDEMPOTENCY_PREFIX}:{msg_id}"
        return f"{OUTBOUND_IDEMPOTENCY_PREFIX}:{channel}:{msg_id}"

    # -- 主入口 -------------------------------------------------------------

    def send(self, message: OutboundMessage) -> SendOutcome:
        """发送一条消息（自动分段、幂等、重试、落失败表）。

        Args:
            message: 待发送消息。

        Returns:
            `SendOutcome`。**永远返回对象**，失败信息在字段里（不抛异常给调用方）。

        Raises:
            ValueError: 请求本身非法（禁止的 msg_type / 必填缺失）。这类错误
                重试无意义，必须让调用方立刻知道。
        """
        started = time.perf_counter()
        trace_id = get_trace_id()
        outcome = SendOutcome(msg_id=message.msg_id, trace_id=trace_id)

        # ---- 0. 入参校验（禁止的类型直接拒绝，不降级）----
        self._validate(message)

        key = self.idempotency_key(message.msg_id, message.channel)

        # ---- 1. 幂等：先读已完成结果 ----
        cached = self._idem_get_result(key)
        if cached is not None:
            outcome.duplicate = True
            outcome.ok = bool(cached.get("ok"))
            outcome.blocked = bool(cached.get("blocked"))
            outcome.failed_record_id = cached.get("failed_record_id")
            with self._stats_lock:
                self._stats.duplicates += 1
            logger.info(
                "出站消息重复提交，复用首次结果 msg_id=%s ok=%s", message.msg_id, outcome.ok
            )
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        # ---- 2. 幂等：占位（并发提交同一 msg_id 时只有一个能进来）----
        if not self._idem_acquire(key, message):
            outcome.duplicate = True
            outcome.in_progress = True
            outcome.ok = False
            with self._stats_lock:
                self._stats.duplicates += 1
            logger.warning("出站消息并发提交，首次仍在处理中 msg_id=%s", message.msg_id)
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            return outcome

        try:
            # ---- 3. 合规闸（**先过闸再分段**，见模块 docstring）----
            scan = self.gate.scan(
                message.content, allow_order_nos=message.allow_order_nos
            )
            safe_text = scan.text
            outcome.blocked = scan.blocked
            if scan.blocked:
                from app.core.metrics import record_compliance_block

                record_compliance_block("outbound")

            # ---- 4. 分段 ----
            segments = self._segments_for(message, safe_text)
            outcome.overflow = self._would_overflow(message, safe_text)

            # ---- 5. 逐段发送（每段自带幂等 + 重试）----
            with self._stats_lock:
                self._stats.submitted += 1

            failed_ids: list[str] = []
            for idx, text in enumerate(segments):
                seg = self._send_segment(message, text, idx, trace_id, scan.blocked)
                outcome.segments.append(seg)
                if not seg.ok:
                    failed_ids.append(seg.segment_id)

            outcome.ok = not failed_ids
            outcome.failed_record_id = next(
                (s.failed_record_id for s in outcome.segments if s.failed_record_id),
                None,
            )

            if outcome.ok:
                self._idem_mark_done(
                    key,
                    {
                        "ok": True,
                        "blocked": outcome.blocked,
                        "failed_record_id": None,
                        "segments": [s.to_dict() for s in outcome.segments],
                    },
                )
            elif outcome.sent_count > 0:
                # 部分成功：**写结果但不释放**。释放后再重放会让已发出的那几段
                # 重复出现在用户侧；部分成功应当由坐席台按失败记录**手动重发**。
                self._idem_mark_done(
                    key,
                    {
                        "ok": False,
                        "partial": True,
                        "blocked": outcome.blocked,
                        "failed_record_id": outcome.failed_record_id,
                        "segments": [s.to_dict() for s in outcome.segments],
                    },
                )
            else:
                # 全部失败：**不写结果、只释放占位**，让上游用同一 msg_id 重试。
                # 若这里写了 DONE，渠道重推同一条入站消息就会被判重复，
                # 用户的疑问就永远没人回答了。失败消息已落表，可查可重发。
                self._idem_release(key)

        except Exception:
            # 未预期的异常必须释放占位，否则这条消息永远发不出去
            self._idem_release(key)
            raise

        outcome.latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "出站消息处理完成 msg_id=%s ok=%s segments=%d sent=%d blocked=%s ms=%.1f",
            message.msg_id,
            outcome.ok,
            len(outcome.segments),
            outcome.sent_count,
            outcome.blocked,
            outcome.latency_ms,
        )
        return outcome

    def resend(self, record_id: str) -> SendOutcome:
        """重放一条失败消息（坐席台「重发」按钮）。

        Args:
            record_id: 失败记录 ID。

        Returns:
            `SendOutcome`。

        Raises:
            LookupError: 记录不存在。
        """
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as conn:
            self.failed_store._ensure(conn)  # noqa: SLF001 - 同模块内协作
            row = conn.execute(
                text(
                    "SELECT * FROM outbound_failed_messages WHERE record_id = :rid"
                ),
                {"rid": record_id},
            ).mappings().first()
        if row is None:
            raise LookupError(record_id)

        # 重发用**新的 msg_id**：旧的幂等键已标记 DONE，直接复用会立刻被判重复
        message = OutboundMessage(
            msg_id=f"{row['msg_id']}_r{uuid.uuid4().hex[:6]}",
            session_id=row["session_id"],
            buyer_id=row["buyer_id"],
            channel=Channel(row["channel"]),
            content=row["content"],
            sender=Sender(row["sender"]),
            msg_type=OutboundMsgType(row["msg_type"]),
        )
        outcome = self.send(message)
        if outcome.ok:
            self.failed_store.mark_resolved(record_id)
        return outcome

    # -- 内部：校验与分段 ---------------------------------------------------

    @staticmethod
    def _validate(message: OutboundMessage) -> None:
        """入参校验（各类「重试无意义」的错误）。"""
        type_value = _msg_type_value(message.msg_type)
        if type_value in {t.value for t in FORBIDDEN_OUTBOUND_TYPES}:
            raise ValueError(
                f"msg_type={type_value} 本期不接入（商家 0.8 节硬约束），"
                "订单/商品信息请改用 TEXT 分行呈现"
            )
        if type_value not in {t.value for t in ALLOWED_OUTBOUND_TYPES}:
            raise ValueError(f"不支持出站 msg_type={type_value}")
        if not message.msg_id or not message.msg_id.strip():
            raise ValueError("msg_id 不得为空（它是幂等键）")
        if not message.session_id or not message.buyer_id:
            raise ValueError("session_id / buyer_id 不得为空")
        if (
            not (message.content or "").strip()
            and type_value == OutboundMsgType.TEXT.value
        ):
            raise ValueError("TEXT 消息内容不得为空")

    @staticmethod
    def _segments_for(message: OutboundMessage, safe_text: str) -> list[str]:
        """分段并逐段复检合规（IMAGE 不分段）。"""
        if _msg_type_value(message.msg_type) == OutboundMsgType.IMAGE.value:
            return [safe_text]
        segments = split_message(safe_text, soft_behind=True)
        if not segments:
            return []
        return get_outbound_gate().enforce_segments(segments)

    @staticmethod
    def _would_overflow(message: OutboundMessage, safe_text: str) -> bool:
        """内容是否超过段数上限（需要给坐席「偏长」提示）。"""
        if _msg_type_value(message.msg_type) == OutboundMsgType.IMAGE.value:
            return False
        limit = get_settings().outbound_max_chars_per_segment
        return len(split_message(safe_text, max_chars=limit)) > MAX_SEGMENTS

    # -- 内部：单段发送 -----------------------------------------------------

    def _send_segment(
        self,
        message: OutboundMessage,
        text: str,
        index: int,
        trace_id: str,
        blocked: bool,
    ) -> SegmentResult:
        """发送单个分段（带重试与失败落表）。"""
        settings = get_settings()
        channel_value = _channel_value(message.channel)
        msg_type_value = _msg_type_value(message.msg_type)
        sender_value = _sender_value(message.sender)
        # 首段沿用原 msg_id（便于与上游对话日志对账），后续段加序号后缀
        seg_id = (
            message.msg_id if index == 0 else f"{message.msg_id}#{index + 1}"
        )
        result = SegmentResult(segment_id=seg_id, content=text, blocked=blocked)

        payload = {
            "msg_id": seg_id,
            "session_id": message.session_id,
            "buyer_id": message.buyer_id,
            "channel": channel_value,
            "msg_type": msg_type_value,
            "content": text,
            "sender": sender_value,
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to

        max_attempts = 1 + max(0, settings.outbound_max_retries)
        budget_deadline = time.monotonic() + settings.outbound_retry_budget_seconds
        last_error: MerchantChannelError | None = None

        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            with self._stats_lock:
                self._stats.attempts += 1
            try:
                sent: MerchantSendResult = self.channel.send(payload)
            except MerchantChannelError as exc:
                last_error = exc
                result.error_code = int(exc.code)
                result.error_message = mask_text(exc.message)
                logger.warning(
                    "出站发送失败 msg_id=%s attempt=%d code=%s status=%s",
                    seg_id,
                    attempt,
                    int(exc.code),
                    exc.http_status,
                )
                if attempt >= max_attempts or not exc.retryable:
                    break
                delay = self._delay_for(attempt, exc)
                if time.monotonic() + delay > budget_deadline:
                    result.retries.append(
                        {"attempt": attempt, "delay": round(delay, 3), "skipped": "budget"}
                    )
                    break
                result.retries.append(
                    {"attempt": attempt, "delay": round(delay, 3), "code": int(exc.code)}
                )
                self._sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001 - 通道实现的意外异常也要落表
                last_error = MerchantChannelError(
                    f"{type(exc).__name__}: {exc}",
                    code=MerchantErrorCode.INTERNAL_ERROR,
                    retryable=False,
                )
                result.error_code = int(MerchantErrorCode.INTERNAL_ERROR)
                result.error_message = mask_text(str(last_error))
                logger.exception("出站通道异常 msg_id=%s", seg_id)
                break

            # 成功
            result.ok = True
            result.platform_msg_id = sent.platform_msg_id
            result.error_code = None
            result.error_message = ""
            with self._stats_lock:
                self._stats.sent += 1
                if attempt == 1:
                    self._stats.first_try_ok += 1
                self._stats.delivered += 1
            self._record_metric(channel_value, msg_type_value, True)
            return result

        # 重试耗尽 → 落失败表 + 告警
        with self._stats_lock:
            self._stats.failed += 1
        self._record_metric(channel_value, msg_type_value, False)
        try:
            result.failed_record_id = self.failed_store.record(
                msg_id=message.msg_id,
                segment_id=seg_id,
                session_id=message.session_id,
                buyer_id=message.buyer_id,
                channel=channel_value,
                msg_type=msg_type_value,
                sender=sender_value,
                content=text,
                error_code=result.error_code,
                error_message=result.error_message
                or (last_error.message if last_error else "unknown"),
                attempts=result.attempts,
                trace_id=trace_id,
            )
        except Exception:  # noqa: BLE001 - 落表失败不能掩盖发送失败本身
            logger.exception("失败消息落表失败 msg_id=%s", seg_id)

        logger.error(
            "出站消息发送失败已落表并告警 msg_id=%s code=%s attempts=%d record=%s",
            seg_id,
            result.error_code,
            result.attempts,
            result.failed_record_id,
        )
        return result

    @staticmethod
    def _delay_for(attempt: int, exc: MerchantChannelError) -> float:
        """计算本次重试的退避时长。

        429 优先服从商家 `Retry-After`（说明书 0.6 节）；其余按指数退避。
        两者都受 `outbound_retry_max_delay_seconds` 兜底约束——商家给个
        `Retry-After: 3600` 时绝不能真的睡一小时。

        Args:
            attempt: 第几次尝试（从 1 开始）。
            exc: 本次失败。

        Returns:
            秒数。
        """
        settings = get_settings()
        cap = settings.outbound_retry_max_delay_seconds
        if exc.code == MerchantErrorCode.RATE_LIMITED and exc.retry_after is not None:
            return max(0.0, min(float(exc.retry_after), cap))
        raw = settings.outbound_retry_base_seconds * (2 ** (attempt - 1))
        return max(0.0, min(raw, cap))

    @staticmethod
    def _record_metric(channel: str, msg_type: str, ok: bool) -> None:
        """记 Prometheus 指标（失败不影响主流程）。"""
        try:
            from app.core.metrics import OUTBOUND_SENDS_TOTAL

            OUTBOUND_SENDS_TOTAL.labels(
                channel=channel, msg_type=msg_type, ok=str(ok).lower()
            ).inc()
        except Exception:  # noqa: BLE001
            pass

    # -- 内部：幂等桥接（同步 ↔ 异步）---------------------------------------

    def _idem_get_result(self, key: str) -> dict[str, Any] | None:
        """读已完成结果（同步驱动异步存储）。"""
        from app.core.asyncbridge import run_maybe_async

        value = run_maybe_async(self.idempotency.get_result(key))
        return value if isinstance(value, dict) else None

    def _idem_acquire(self, key: str, message: OutboundMessage) -> bool:
        """占位 + 写入请求指纹（便于排障「哪个会话的哪条消息」）。"""
        from app.core.asyncbridge import run_maybe_async

        async def _acquire() -> bool:
            ok = await self.idempotency.acquire(key)
            if ok:
                await self.idempotency.cache.set_json(
                    key + ":req",
                    {
                        "msg_id": message.msg_id,
                        "session_id": message.session_id,
                        "channel": _channel_value(message.channel),
                    },
                    ttl=get_settings().outbound_idempotency_ttl_seconds,
                )
            return ok

        return bool(run_maybe_async(_acquire()))

    def _idem_mark_done(self, key: str, result: dict[str, Any]) -> None:
        """写回首次结果。"""
        from app.core.asyncbridge import run_maybe_async

        run_maybe_async(self.idempotency.mark_done(key, result))

    def _idem_release(self, key: str) -> None:
        """释放占位。"""
        from app.core.asyncbridge import run_maybe_async

        run_maybe_async(self.idempotency.release(key))


_SERVICE: MessageSendService | None = None


def get_message_send_service() -> MessageSendService:
    """全局发送服务单例。"""
    global _SERVICE  # noqa: PLW0603
    if _SERVICE is None:
        _SERVICE = MessageSendService()
    return _SERVICE


def set_message_send_service(service: MessageSendService | None) -> None:
    """替换全局发送服务（**测试专用**）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = service


def new_msg_id(prefix: str = "msg") -> str:
    """生成出站 `msg_id`（本地 UUID，任务书 BE-02 核心实现①）。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def outbound_message_from_reply(
    *,
    session_id: str,
    buyer_id: str,
    channel: Channel | str,
    content: str,
    msg_id: str | None = None,
    sender: Sender = Sender.BOT,
    reply_to: str | None = None,
    allow_order_nos: set[str] | None = None,
) -> OutboundMessage:
    """把编排层/坐席台的回复转成待发送消息。

    Args:
        session_id: 会话 ID。
        buyer_id: 买家 ID。
        channel: 渠道。
        content: 回复文本。
        msg_id: 复用调用方的 msg_id（**LLM 回复必须复用入站 msg_id**，
            否则同一条入站消息被重放两次会发出两条回复）。
        sender: 发送方，机器人 BOT / 坐席 AGENT。
        reply_to: 被回复的消息 ID。
        allow_order_nos: 本会话已核验的订单号白名单。

    Returns:
        `OutboundMessage`。
    """
    return OutboundMessage(
        msg_id=msg_id or new_msg_id(),
        session_id=session_id,
        buyer_id=buyer_id,
        channel=Channel(channel),
        content=content,
        sender=sender,
        reply_to=reply_to,
        allow_order_nos=set(allow_order_nos or set()),
    )


__all__ = [
    "ALLOWED_OUTBOUND_TYPES",
    "FORBIDDEN_OUTBOUND_TYPES",
    "FailedMessageStore",
    "MAX_SEGMENTS",
    "MessageSendService",
    "OUTBOUND_IDEMPOTENCY_PREFIX",
    "OutboundMessage",
    "SegmentResult",
    "SendOutcome",
    "SendStats",
    "get_message_send_service",
    "new_msg_id",
    "outbound_message_from_reply",
    "set_failed_table_engine",
    "set_message_send_service",
    "split_message",
]
