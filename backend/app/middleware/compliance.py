"""出站脱敏中间件（BE-17 执行位置②「接口返回时」的 FastAPI 中间件形态）。

## 为什么需要它（`core/logging.py` 的 `SensitiveFilter` 覆盖不到的地方）

`SensitiveFilter` 只作用于**日志**。而 BE-17 DoD ② 的要求是
「日志 / 错误堆栈 / 监控看板中**零完整敏感字段**」——其中「接口返回」这一路
完全可能夹带完整手机号与地址：失败消息表里存的就是未发出的原文，
`/internal/v1/message/failures` 会把它原样吐给坐席台；坐席台再把内容
贴进工单备注，敏感字段就流到了第二个系统。

所以在**响应出口**再设一道闸，与日志闸互为补充：

| 闸 | 位置 | 覆盖 |
| --- | --- | --- |
| `SensitiveFilter` | 日志 formatter 之前 | 日志 / 异常堆栈 |
| `OutboundGate` | 文本发送前 | 用户可见出站文本（外链 + 脱敏 + 禁用表述） |
| **本中间件** | HTTP 响应出口 | 所有接口返回值（含坐席台、BFF） |

## 为什么只对白名单前缀生效，不全站开

`/internal/v1/kb/*` 与 `/internal/v1/rag/*` 返回的是**知识库内容**
（商品知识卡、FAQ、政策条款）。这些内容由运营维护、需要原样编辑与预览，
把它们按 PII 正则脱敏会出现两类问题：

1. 知识卡里的示例订单号（`202501150001`）被改成 `2025****0001`，
   运营改不回来，FE-05 的「编辑后保存」会直接存进脱敏后的坏数据；
2. 政策条款里的金额（`满 99 元`）可能被姓名/地址规则误伤
   （交接文档 4.8 记过「满 99 元**」这个真实缺陷）。

因此默认只对**携带用户原始数据的路径**生效，见 `DEFAULT_MASK_PREFIXES`。
新增接口时按「这个返回值里有没有用户提供的原文」来判断是否加入。

## 为什么用纯 ASGI 而不是 `BaseHTTPMiddleware`

`BaseHTTPMiddleware` 会把响应体缓冲进 anyio 内存流，SSE（`/bff/v1/chat/stream`）
会失去实时性。本中间件同样刻意做成纯 ASGI，并且**只在 content-type 为
`application/json` 时才缓冲**，其它（SSE、文件下载、Prometheus 文本）
一律原样透传，不改变流式语义。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.core.logging import get_logger, mask_value
from app.core.metrics import COMPLIANCE_MASKED_TOTAL

logger = get_logger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: 默认启用脱敏的路径前缀（**只含携带用户原始数据的接口**，理由见模块 docstring）
#:
#: **刻意不含 `/bff/v1`**：BFF 是内部坐席台，工单号（`TS`+12 位）与买家订单号
#: 是坐席检索、回填、对账的主键。它们都是 12 位数字串，会被 `mask_value` 打成
#: `TS25****9360`——坐席拿着打码号查不到工单（实测 404），也回填不了商家系统。
#: 坐席侧的合规由角色裁剪（`require_role`）、字段裁剪（`app/schemas/bff.py`
#: 里没声明的字段不下发）与写操作全量审计（`AuditLog`）保证；买家侧出站文本
#: 仍由合规闸与 `/internal/v1/message` 的脱敏兜住。
DEFAULT_MASK_PREFIXES: tuple[str, ...] = (
    "/api/v1/callback",  # 渠道回调：回显里带 received/enqueued 计数与 trace
    "/internal/v1/message",  # 出站消息：失败表里是**未发出的原文**
)

#: 不做 JSON 解析直接透传的 content-type（流式 / 文本 / 二进制）
_PASSTHROUGH_CONTENT_TYPES: tuple[str, ...] = (
    "text/event-stream",
    "text/plain",
    "application/octet-stream",
    "multipart/",
)


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    """从 ASGI 响应头列表里取一个头的值（大小写不敏感）。"""
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value.decode("latin-1", errors="replace")
    return ""


class ComplianceMaskMiddleware:
    """响应出口脱敏中间件。

    Attributes:
        app: 内层 ASGI 应用。
        prefixes: 生效的路径前缀；空元组表示**全站生效**。
        enabled: 总开关（对应 `Settings.compliance_mask_responses`）。
        stats: 脱敏计数（`masked` = 实际改写过内容的响应数）。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefixes: tuple[str, ...] = DEFAULT_MASK_PREFIXES,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.prefixes = tuple(prefixes)
        self.enabled = enabled
        self.stats: dict[str, int] = {"scanned": 0, "masked": 0, "passthrough": 0}

    # ------------------------------------------------------------------

    def _should_mask(self, path: str) -> bool:
        """判断该路径是否需要脱敏。"""
        if not self.enabled:
            return False
        if not self.prefixes:
            return True  # 空前缀 = 全站
        return any(path.startswith(p) for p in self.prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self._should_mask(path):
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = {
            "buffering": False,
            "start": None,
            "chunks": [],
            "path": path,
            #: 已经向客户端发过 body 分片（异常兜底时判断是否还需补发）
            "streaming": False,
            "flushed": False,
        }

        async def send_wrapper(message: Message) -> None:
            """按 content-type 决定透传还是缓冲后脱敏。"""
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = _header_value(headers, b"content-type").lower()
                if self._is_json(content_type):
                    state["buffering"] = True
                    state["start"] = message
                    state["headers"] = headers
                else:
                    state["streaming"] = True
                    await send(message)
                return

            if message["type"] == "http.response.body":
                if state["buffering"]:
                    state["chunks"].append(message.get("body", b"") or b"")
                    if not message.get("more_body", False):
                        await self._flush(state, send)
                    return
                await send(message)
                return

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 应用抛异常时，缓冲中的响应绝不吞掉——按原样发出，
            # 让统一的异常处理器（已脱敏）接管。
            #
            # 只有当 start 还没发出（`streaming=False`）时才需要补发；
            # 已经在流式透传的情况（`streaming=True`）下响应已开始，
            # 再补一个 start 会撞 ASGI 的「多次 response.start」断言。
            if not state["streaming"]:
                await self._flush_unmasked(state, send)
            raise

    # ------------------------------------------------------------------

    @staticmethod
    def _is_json(content_type: str) -> bool:
        """是否应当解析并脱敏的响应体。"""
        if not content_type:
            return False
        if any(pt in content_type for pt in _PASSTHROUGH_CONTENT_TYPES):
            return False
        return "application/json" in content_type or content_type.endswith("+json")

    async def _flush(self, state: dict[str, Any], send: Send) -> None:
        """把缓冲的 JSON 响应体脱敏后发出。"""
        start = state.get("start")
        chunks: list[bytes] = state.get("chunks", [])
        if start is None:  # pragma: no cover - 理论上不会发生
            return

        raw = b"".join(chunks)
        self.stats["scanned"] += 1
        masked = self._mask_json(raw)
        if masked is None:
            # 解析失败（非 JSON 或结构异常）：原样发出，**不阻断业务**
            await self._flush_unmasked(state, send)
            return

        if masked == raw:
            self.stats["passthrough"] += 1
            await self._flush_unmasked(state, send)
            return

        self.stats["masked"] += 1
        try:
            COMPLIANCE_MASKED_TOTAL.labels(category="response").inc()
        except Exception:  # noqa: BLE001 - 指标失败不影响响应
            pass
        logger.info(
            "接口返回值已脱敏 path=%s bytes=%d->%d",
            state.get("path"),
            len(raw),
            len(masked),
        )

        headers = [
            (k, v)
            for k, v in state.get("headers", [])
            if k.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(masked)).encode("latin-1")))
        start = dict(start)
        start["headers"] = headers
        await send(start)
        await send({"type": "http.response.body", "body": masked})

    @staticmethod
    def _mask_json(raw: bytes) -> bytes | None:
        """解析 JSON → 递归脱敏 → 重新序列化。

        Args:
            raw: 原始响应体字节。

        Returns:
            脱敏后的字节；**解析失败返回 None**（调用方按原样发出）。
        """
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非 JSON 一律不碰
            return None
        try:
            masked = mask_value(payload)
            return json.dumps(masked, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:  # noqa: BLE001 - 序列化失败同样不阻断
            return None

    async def _flush_unmasked(self, state: dict[str, Any], send: Send) -> None:
        """把缓冲的响应体按原样发出（未脱敏：解析失败 / 无变化 / 异常路径）。

        分两个 `send`：先 `http.response.start`，再**逐个** body chunk。
        逐块发而不是合并成一个 body，是为了保持流式响应的分片语义
        （大响应被合并会让客户端一次性拿到全部内容，失去边下边渲染的能力）。
        """
        start = state.get("start")
        if start is None:  # pragma: no cover - 理论上不会发生
            return
        if state.get("flushed"):  # 防止异常路径与正常收尾重复发送
            return
        state["flushed"] = True
        await send(start)
        for chunk in state.get("chunks", []):
            await send({"type": "http.response.body", "body": chunk})

    def snapshot(self) -> dict[str, int]:
        """导出统计（运维看板用）。"""
        return dict(self.stats)


__all__ = [
    "DEFAULT_MASK_PREFIXES",
    "ComplianceMaskMiddleware",
]
