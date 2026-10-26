"""BE-14 / BE-15 · 转人工服务（建单 + 路由 + 幂等 + 限流 + 离线兜底）。

## 一次 `handoff()` 做了什么

```
幂等检查(缓存/DB) ──命中──> 直接返回原工单（idempotent_hit=True）
      │未命中
      ▼
上下文卡片归一 + 补齐七要素 ──> 记录「提交时缺哪几项」
      ▼
规则/意图 → 选坐席组(G1~G6) → 判断是否在服务时段(BE-15)
      ▼
落库：tickets + ticket_events(CREATED) + audit_logs(TICKET_CREATE)
      ▼
在线？─否─> 离线兜底话术（不承诺时间、不发链接）
      │是
      ▼
令牌桶(5 QPS) ──超限──> 本地队列：事件 RATE_LIMITED，retry_count+1
      │通过
      ▼
调商家 5 号接口 ──失败──> 事件 FAILED，retry_count+1（**工单照建、工单号照给**）
      │成功
      ▼
回写 merchant_ticket_no + synced_at，事件 SYNCED
```

## 三条不能破的约束

1. **`session_id` 唯一**：`tickets.session_id` 是唯一索引，是幂等的最终保障。
   缓存只是加速，缓存挂了也不会重复建单（DB 唯一约束兜底）。
2. **商家侧挂了也要给用户工单号**：本地工单先落库再同步商家。用户拿到的是
   我方工单号，坐席在本地工作台照样能处理。反过来（先调商家再落库）一旦商家
   超时，用户就既没工单号也没人管——这是最坏的结果。
3. **限流不是失败**：5 QPS 是商家给的硬上限，超限就进本地队列，靠
   `sync_pending()` 按 `ticket_retry_backoff`（1/2/4/8/16 秒）补同步。

## 离线兜底（BE-15）

服务时段外**不承诺「马上有人」**，也不给排队位（给了就是假承诺）。话术见
`app/services/handoff/online.py`，禁用「请稍等」「马上」等无限等待表述。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.adapters.merchant_ticket import (
    MerchantTicketPort,
    MerchantTicketResult,
    get_ticket_client,
)
from app.core.asyncbridge import run_maybe_async
from app.core.enums import Channel, HandoffReason, Priority, TicketStatus
from app.core.idempotency import build_key
from app.core.logging import mask_text
from app.core.ratelimit import RateLimitExceeded, TokenBucketRateLimiter
from app.core.trace import new_ticket_no
from app.db.base import utcnow
from app.schemas.handoff import (
    REASON_CN,
    REASON_DEFAULT_PRIORITY,
    HandoffRequest,
    TicketContextCard,
    TranscriptTurn,
)
from app.services.handoff.online import (
    is_online,
    offline_user_message,
    online_user_message,
)
from app.services.handoff.routing import AgentRouter, GroupInfo, get_agent_router
from app.services.handoff.rules import HandoffRuleEngine, get_rule_engine

#: 工单幂等 key 前缀（契约：`ticket:{session_id}`）
TICKET_KEY_PREFIX = "ticket:"
#: 工单幂等 TTL：7 天（契约表）
TICKET_IDEMPOTENCY_TTL = 7 * 24 * 3600
#: 内部工作台工单详情路径（仅坐席内网可打开，**绝不发给用户**）
WORKBENCH_TICKET_PATH = "/workbench/tickets/{ticket_no}"


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class HandoffOutcome:
    """一次转人工的执行结果（编排层与 API 层共用）。

    Attributes:
        text: 给用户看的纯文字话术（**不含链接**）。
        ticket_no: 我方工单号（`TS` + 12 位数字）。
        status: 工单状态。
        priority: 优先级。
        assignee: 受理人（在线且组内有主管时填主管）。
        group_id: 坐席分组 ID。
        group_name: 坐席分组名。
        queue_position: 排队位置（离线时为 None）。
        estimated_wait_seconds: 预计等待秒数（离线时为 None）。
        offline: 是否走了「人工不在线」兜底。
        idempotent_hit: 是否命中幂等。
        user_message: 用户侧提示（同 `text`）。
        created_at: 建单时间。
        context_complete: 落库的上下文卡片七要素是否齐全。
        merchant_ticket_no: 商家侧工单号（未同步时为 None）。
        degraded: 降级标记（如 `TICKET_SYNC_FAILED`、`TICKET_RATE_LIMITED`）。
    """

    text: str = ""
    ticket_no: str | None = None
    status: TicketStatus = TicketStatus.OPEN
    priority: Priority = Priority.NORMAL
    assignee: str | None = None
    group_id: str | None = None
    group_name: str = ""
    queue_position: int | None = None
    estimated_wait_seconds: int | None = None
    offline: bool = False
    idempotent_hit: bool = False
    user_message: str = ""
    created_at: datetime | None = None
    context_complete: bool = True
    merchant_ticket_no: str | None = None
    degraded: list[str] = field(default_factory=list)

    @property
    def handoff_url(self) -> str | None:
        """工单详情地址（**仅坐席内网可用，不得发给用户**）。"""
        if not self.ticket_no:
            return None
        return WORKBENCH_TICKET_PATH.format(ticket_no=self.ticket_no)

    def to_dict(self) -> dict[str, Any]:
        """转 `HandoffResponse` 兼容字典。"""
        return {
            "ticket_no": self.ticket_no,
            "status": self.status.value,
            "priority": self.priority.value,
            "assignee": self.assignee,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "queue_position": self.queue_position,
            "estimated_wait_seconds": self.estimated_wait_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "handoff_url": self.handoff_url,
            "idempotent_hit": self.idempotent_hit,
            "user_message": self.user_message,
            "offline": self.offline,
            "context_complete": self.context_complete,
            "merchant_ticket_no": self.merchant_ticket_no,
            "degraded": list(self.degraded),
        }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class HandoffService:
    """转人工服务（建单、路由、幂等、限流、离线兜底）。"""

    def __init__(
        self,
        *,
        cache: Any = None,
        router: AgentRouter | None = None,
        rule_engine: HandoffRuleEngine | None = None,
        ticket_client: MerchantTicketPort | None = None,
        limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._cache = cache
        self._router = router
        self._engine = rule_engine
        self._client = ticket_client
        self._limiter = limiter

    # -- 依赖（惰性，便于单测替换） -----------------------------------------

    @property
    def cache(self) -> Any:
        """缓存（生产 Redis / 本地 InMemoryCache）。"""
        if self._cache is None:
            from app.adapters.cache import get_cache

            self._cache = get_cache()
        return self._cache

    @property
    def router(self) -> AgentRouter:
        """坐席路由。"""
        if self._router is None:
            self._router = get_agent_router()
        return self._router

    @property
    def engine(self) -> HandoffRuleEngine:
        """规则引擎（API 层的规则管理也用它）。"""
        if self._engine is None:
            self._engine = get_rule_engine()
        return self._engine

    @property
    def client(self) -> MerchantTicketPort:
        """商家工单端口。"""
        if self._client is None:
            self._client = get_ticket_client()
        return self._client

    @property
    def limiter(self) -> TokenBucketRateLimiter:
        """工单创建令牌桶（5 QPS，全局瓶颈）。"""
        if self._limiter is None:
            from app.core.config import get_settings

            qps = get_settings().effective_qps.get("ticket_create", 5)
            self._limiter = TokenBucketRateLimiter(rate=float(qps), capacity=float(qps))
        return self._limiter

    # -- 主流程 -------------------------------------------------------------

    def handoff(
        self,
        *,
        session_id: str,
        reason: HandoffReason | str,
        buyer_id: str = "",
        channel: Channel | str | None = None,
        summary: str = "",
        entities: dict[str, Any] | None = None,
        tried_actions: Sequence[str] | None = None,
        priority: Priority | str | None = None,
        transcript: Sequence[TranscriptTurn | dict[str, Any]] | None = None,
        context_card: TicketContextCard | dict[str, Any] | None = None,
        intent: dict[str, Any] | None = None,
        turn_count: int = 0,
        force_offline: bool | None = None,
    ) -> HandoffOutcome:
        """创建（或幂等返回）一张转人工工单。

        Args:
            session_id: 会话 ID，**幂等键**。
            reason: 转人工原因（5 值）。
            buyer_id: 买家标识。
            channel: 渠道。
            summary: 问题摘要（≤500 字）。
            entities: 结构化实体（order_no / sku_id 等）。
            tried_actions: 已尝试动作名列表。
            priority: 优先级；None 时按 `reason` 推导。
            transcript: 对话摘录（自动截断到最近 20 轮）。
            context_card: AI 上下文卡片（dict 或模型均可）。
            intent: 意图识别结果（dict，含 `category`/`label`）。
            turn_count: 会话轮次（卡片缺 `turn_count` 时用）。
            force_offline: 强制离线（演练与单测用）。

        Returns:
            `HandoffOutcome`。
        """
        started = time.perf_counter()
        reason_enum = self._norm_reason(reason)
        priority_enum = self._norm_priority(priority, reason_enum)
        entities = dict(entities or {})

        # 1) 幂等：缓存 → DB
        cached = self._read_idempotent(session_id)
        if cached is not None:
            return cached
        existing = self._load_existing_ticket(session_id)
        if existing is not None:
            outcome = self._outcome_from_row(existing, idempotent_hit=True)
            self._write_idempotent(session_id, outcome)
            return outcome

        # 2) 上下文卡片：归一 + 补齐七要素
        card, missing_on_submit = self._build_card(
            context_card=context_card,
            session_id=session_id,
            buyer_id=buyer_id,
            channel=channel,
            summary=summary,
            entities=entities,
            tried_actions=tried_actions,
            reason=reason_enum,
            turn_count=turn_count,
            intent=intent,
        )

        # 3) 选组 + 在线判定
        category = (intent or {}).get("category")
        group = self.router.route(
            reason=reason_enum, channel=channel, intent_category=category
        )
        online = (
            (not force_offline)
            and group is not None
            and is_online(group.online_hours)
        )
        if force_offline is True:
            online = False

        queue_position: int | None = None
        estimated_wait: int | None = None
        if online and group is not None:
            queue_position = self.router.queue_position(group.group_id) + 1
            estimated_wait = self.router.estimate_wait_seconds(group, queue_position)

        ticket_no = new_ticket_no()
        transcript_rows = self._norm_transcript(transcript)
        card_dict = card.model_dump(mode="json")
        card_dict["_missing_on_submit"] = list(missing_on_submit)

        # 4) 落库（本地工单先建，商家同步在后）
        created = self._insert_ticket(
            ticket_no=ticket_no,
            session_id=session_id,
            buyer_id=buyer_id,
            channel=channel,
            reason=reason_enum,
            priority=priority_enum,
            card=card,
            card_dict=card_dict,
            entities=entities,
            transcript=transcript_rows,
            group=group,
            queue_position=queue_position,
            estimated_wait=estimated_wait,
            online=online,
            context_complete=card.is_complete(),
        )
        if created is None:
            # 并发下唯一约束命中：说明另一请求已建单，回读返回
            row = self._load_existing_ticket(session_id)
            if row is not None:
                outcome = self._outcome_from_row(row, idempotent_hit=True)
                self._write_idempotent(session_id, outcome)
                return outcome

        # 5) 在线 → 同步商家（限流则进本地队列）
        degraded: list[str] = []
        merchant_ticket_no: str | None = None
        if online:
            merchant_ticket_no, sync_degraded = self._sync_to_merchant(
                ticket_no=ticket_no,
                session_id=session_id,
                reason=reason_enum,
                priority=priority_enum,
                card=card,
                channel=channel,
            )
            degraded.extend(sync_degraded)

        # 6) 话术
        if online:
            text = online_user_message(ticket_no, queue_position)
        else:
            text = offline_user_message(reason_enum, ticket_no)

        outcome = HandoffOutcome(
            text=text,
            ticket_no=ticket_no,
            status=TicketStatus.OPEN,
            priority=priority_enum,
            assignee=(group.supervisor or None) if online and group else None,
            group_id=group.group_id if group else None,
            group_name=group.group_name if group else "",
            queue_position=queue_position,
            estimated_wait_seconds=estimated_wait,
            offline=not online,
            idempotent_hit=False,
            user_message=text,
            created_at=utcnow(),
            context_complete=card.is_complete(),
            merchant_ticket_no=merchant_ticket_no,
            degraded=degraded,
        )
        # 7) 写幂等缓存（DB 唯一约束仍是最终保障）
        self._write_idempotent(session_id, outcome)
        _ = started  # 预留：如需记录整体耗时可用
        return outcome

    # -- 幂等 ---------------------------------------------------------------

    @staticmethod
    def _idem_key(session_id: str) -> str:
        """工单幂等 key（契约口径 `ticket:{session_id}`）。"""
        return f"{TICKET_KEY_PREFIX}{session_id}"

    def _read_idempotent(self, session_id: str) -> HandoffOutcome | None:
        """读幂等缓存；未命中或缓存不可用时返回 None。"""
        key = self._idem_key(session_id)
        try:
            payload = run_maybe_async(self.cache.get_json(key))
        except Exception:  # noqa: BLE001 - 缓存故障不该阻断建单
            return None
        if not isinstance(payload, dict) or not payload.get("ticket_no"):
            return None
        return HandoffOutcome(
            text=str(payload.get("user_message") or ""),
            ticket_no=payload.get("ticket_no"),
            status=self._to_status(payload.get("status")),
            priority=self._to_priority(payload.get("priority")),
            assignee=payload.get("assignee"),
            group_id=payload.get("group_id"),
            group_name=str(payload.get("group_name") or ""),
            queue_position=payload.get("queue_position"),
            estimated_wait_seconds=payload.get("estimated_wait_seconds"),
            offline=bool(payload.get("offline")),
            idempotent_hit=True,
            user_message=str(payload.get("user_message") or ""),
            created_at=self._to_dt(payload.get("created_at")),
            context_complete=bool(payload.get("context_complete", True)),
            merchant_ticket_no=payload.get("merchant_ticket_no"),
        )

    def _write_idempotent(self, session_id: str, outcome: HandoffOutcome) -> None:
        """写幂等缓存（失败忽略：DB 唯一约束是最终保障）。"""
        key = self._idem_key(session_id)
        try:
            run_maybe_async(
                self.cache.set_json(
                    key, outcome.to_dict(), ttl=TICKET_IDEMPOTENCY_TTL
                )
            )
        except Exception:  # noqa: BLE001
            pass

    # -- 上下文卡片 ---------------------------------------------------------

    def _build_card(
        self,
        *,
        context_card: TicketContextCard | dict[str, Any] | None,
        session_id: str,
        buyer_id: str,
        channel: Channel | str | None,
        summary: str,
        entities: dict[str, Any],
        tried_actions: Sequence[str] | None,
        reason: HandoffReason,
        turn_count: int,
        intent: dict[str, Any] | None,
    ) -> tuple[TicketContextCard, list[str]]:
        """归一上下文卡片并补齐七要素，返回 (卡片, 提交时缺失项)。"""
        card = self._coerce_card(
            context_card,
            session_id=session_id,
            buyer_id=buyer_id,
            channel=channel,
            summary=summary,
            entities=entities,
            tried_actions=tried_actions,
            reason=reason,
            turn_count=turn_count,
            intent=intent,
        )
        missing = card.missing_fields()

        # 补齐：verified 由实体推导；实在没有就写「显式空」
        if not card.verified:
            derived = [
                {"type": "entity", "key": str(k), "value": str(v)}
                for k, v in entities.items()
                if v not in (None, "")
            ]
            card.verified = derived or [
                {"type": "none", "note": "本轮未调用实时接口核实"}
            ]
        if not card.tried_actions:
            names = [str(a) for a in (tried_actions or []) if str(a).strip()]
            card.tried_actions = (
                [{"action": n, "result": "未完成"} for n in names]
                or [{"action": "none", "note": "本轮未尝试自动动作"}]
            )
        if not card.emotion:
            card.emotion = {"level": "NEUTRAL", "intensity": "low"}
        if card.turn_count <= 0:
            card.turn_count = max(int(turn_count or 0), 1)
        if not card.summary.strip():
            card.summary = (summary or "用户请求人工处理")[:500]
        if not card.unmet_demand.strip():
            card.unmet_demand = card.summary[:500]
        if not card.suggested_action.strip():
            card.suggested_action = f"人工跟进：{REASON_CN.get(reason, reason.value)}"
        if not card.buyer_id:
            card.buyer_id = buyer_id
        return card, missing

    @staticmethod
    def _coerce_card(
        raw: TicketContextCard | dict[str, Any] | None,
        *,
        session_id: str,
        buyer_id: str,
        channel: Channel | str | None,
        summary: str,
        entities: dict[str, Any],
        tried_actions: Sequence[str] | None,
        reason: HandoffReason,
        turn_count: int,
        intent: dict[str, Any] | None,
    ) -> TicketContextCard:
        """把 dict / 模型 / None 统一成 `TicketContextCard`。"""
        if isinstance(raw, TicketContextCard):
            card = raw.model_copy(deep=True)
        else:
            data = dict(raw or {})
            allowed = set(TicketContextCard.model_fields)
            kwargs = {k: v for k, v in data.items() if k in allowed}
            emotion = kwargs.get("emotion")
            if isinstance(emotion, str):
                kwargs["emotion"] = {"level": emotion, "intensity": "medium"}
            # 编排层传的是 `list[str]`（工具名 / 已核实项），契约要求
            # `list[dict]`；在这里归一，避免每个调用方都记一遍字段形状。
            kwargs["verified"] = HandoffService._as_dict_list(
                kwargs.get("verified"), key="text"
            )
            kwargs["tried_actions"] = HandoffService._as_dict_list(
                kwargs.get("tried_actions"), key="action"
            )
            kwargs.setdefault("session_id", session_id)
            kwargs.setdefault("buyer_id", buyer_id)
            kwargs.setdefault("summary", summary)
            kwargs.setdefault("turn_count", turn_count)
            card = TicketContextCard(**kwargs)

        if card.session_id != session_id:
            card.session_id = session_id
        if not card.buyer_id:
            card.buyer_id = buyer_id
        if card.channel is None:
            card.channel = HandoffService._norm_channel(channel)
        # 卡片没给 verified 时，用实体先垫上（后面 _build_card 还会补显式空）
        if not card.verified and entities:
            card.verified = [
                {"type": "entity", "key": str(k), "value": str(v)}
                for k, v in entities.items()
                if v not in (None, "")
            ]
        if not card.tried_actions and tried_actions:
            card.tried_actions = [
                {"action": str(a), "result": "未完成"}
                for a in tried_actions
                if str(a).strip()
            ]
        if not card.summary.strip():
            card.summary = (summary or "")[:500]
        if card.turn_count <= 0 and turn_count:
            card.turn_count = int(turn_count)
        _ = reason, intent  # 预留：后续可按意图补 suggested_action
        return card

    @staticmethod
    def _as_dict_list(value: Any, *, key: str) -> list[dict[str, Any]]:
        """把 `list[str]` / `list[dict]` / None 统一成 `list[dict]`。

        Args:
            value: 原始值。
            key: 字符串项被包装成 `{key: 原文}`。

        Returns:
            字典列表；None 或空返回 `[]`。
        """
        if not value:
            return []
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
            elif item is not None and str(item).strip():
                out.append({key: str(item)})
        return out

    # -- 落库 ---------------------------------------------------------------

    def _insert_ticket(
        self,
        *,
        ticket_no: str,
        session_id: str,
        buyer_id: str,
        channel: Channel | str | None,
        reason: HandoffReason,
        priority: Priority,
        card: TicketContextCard,
        card_dict: dict[str, Any],
        entities: dict[str, Any],
        transcript: list[dict[str, str]],
        group: GroupInfo | None,
        queue_position: int | None,
        estimated_wait: int | None,
        online: bool,
        context_complete: bool,
    ) -> bool | None:
        """落库工单 + 事件 + 审计。唯一约束冲突时返回 None。"""
        try:
            from sqlalchemy.exc import IntegrityError

            from app.db.engine import session_scope
            from app.db.models.conversation import AuditLog
            from app.db.models.ticket import Ticket, TicketEvent

            with session_scope() as session:
                ticket = Ticket(
                    ticket_no=ticket_no,
                    session_id=session_id,
                    buyer_id=buyer_id or "",
                    channel=(HandoffService._norm_channel(channel) or Channel.TAOBAO).value,
                    reason=reason.value,
                    summary=card.summary[:500],
                    entities=entities,
                    tried_actions=card.tried_actions,
                    priority=priority.value,
                    status=TicketStatus.OPEN.value,
                    assignee=(group.supervisor if online and group else None),
                    group_id=group.group_id if group else None,
                    queue_position=queue_position,
                    estimated_wait_seconds=estimated_wait,
                    transcript=transcript,
                    context_card=card_dict,
                    context_complete=context_complete,
                    offline=not online,
                )
                session.add(ticket)
                session.flush()
                session.add(
                    TicketEvent(
                        ticket_no=ticket_no,
                        event="CREATED",
                        attempt=0,
                        detail={
                            "reason": reason.value,
                            "priority": priority.value,
                            "group_id": group.group_id if group else None,
                            "offline": not online,
                        },
                    )
                )
                session.add(
                    AuditLog(
                        actor="bot",
                        action="TICKET_CREATE",
                        target_type="ticket",
                        target_id=ticket_no,
                        detail={
                            "session_id": session_id,
                            "reason": reason.value,
                            "priority": priority.value,
                            "offline": not online,
                        },
                    )
                )
            return True
        except IntegrityError:
            # session_id 唯一约束：并发重复提交，交给调用方回读
            return None
        except Exception as exc:  # noqa: BLE001 - 落库失败不应让用户拿不到话术
            from app.core.logging import get_logger

            get_logger(__name__).warning("工单落库失败（继续返回话术）: %s", exc)
            return False

    def _load_existing_ticket(self, session_id: str) -> Any:
        """按 `session_id` 查已有工单（幂等的 DB 兜底）。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket

            with session_scope() as session:
                return (
                    session.query(Ticket)
                    .filter(Ticket.session_id == session_id)
                    .one_or_none()
                )
        except Exception:  # noqa: BLE001
            return None

    def _outcome_from_row(self, row: Any, *, idempotent_hit: bool) -> HandoffOutcome:
        """把 `Ticket` 行还原成 `HandoffOutcome`。"""
        reason = self._norm_reason(getattr(row, "reason", None))
        offline = bool(getattr(row, "offline", False))
        ticket_no = getattr(row, "ticket_no", None)
        text = (
            offline_user_message(reason, ticket_no)
            if offline
            else online_user_message(ticket_no, getattr(row, "queue_position", None))
        )
        return HandoffOutcome(
            text=text,
            ticket_no=ticket_no,
            status=self._to_status(getattr(row, "status", None)),
            priority=self._to_priority(getattr(row, "priority", None)),
            assignee=getattr(row, "assignee", None),
            group_id=getattr(row, "group_id", None),
            group_name="",
            queue_position=getattr(row, "queue_position", None),
            estimated_wait_seconds=getattr(row, "estimated_wait_seconds", None),
            offline=offline,
            idempotent_hit=idempotent_hit,
            user_message=text,
            created_at=getattr(row, "created_at", None),
            context_complete=bool(getattr(row, "context_complete", True)),
            merchant_ticket_no=getattr(row, "merchant_ticket_no", None),
        )

    # -- 商家同步 -----------------------------------------------------------

    def _sync_to_merchant(
        self,
        *,
        ticket_no: str,
        session_id: str,
        reason: HandoffReason,
        priority: Priority,
        card: TicketContextCard,
        channel: Channel | str | None,
    ) -> tuple[str | None, list[str]]:
        """调商家 5 号接口同步工单；返回 (商家工单号, 降级标记)。"""
        degraded: list[str] = []

        # 令牌桶：超限不报错，进本地队列
        try:
            run_maybe_async(self.limiter.acquire("ticket_create"))
        except RateLimitExceeded:
            degraded.append("TICKET_RATE_LIMITED")
            self._append_event(ticket_no, "RATE_LIMITED", detail={"reason": "qps"})
            return None, degraded

        payload = {
            "session_id": session_id,
            "ticket_no": ticket_no,
            "reason": reason.value,
            "priority": priority.value,
            "channel": (self._norm_channel(channel) or Channel.TAOBAO).value,
            "summary": card.summary[:500],
            "buyer_id": card.buyer_id,
            "entities": {
                v.get("key"): v.get("value")
                for v in card.verified
                if isinstance(v, dict) and v.get("type") == "entity"
            },
            "tried_actions": card.tried_actions,
            "transcript": [],
        }
        try:
            result: MerchantTicketResult = self.client.create_ticket(payload)
        except Exception as exc:  # noqa: BLE001
            degraded.append("TICKET_SYNC_FAILED")
            self._append_event(
                ticket_no,
                "FAILED",
                detail={"error": mask_text(str(exc))[:200]},
            )
            return None, degraded

        if not result.ok:
            degraded.append("TICKET_SYNC_FAILED")
            self._append_event(
                ticket_no,
                "FAILED",
                detail={
                    "error_code": result.error_code,
                    "message": result.message[:200],
                },
            )
            return None, degraded

        self._mark_synced(ticket_no, result.ticket_no)
        return result.ticket_no, degraded

    def _append_event(
        self, ticket_no: str, event: str, *, detail: dict[str, Any] | None = None
    ) -> None:
        """追加一条工单事件（失败忽略，不影响主流程）。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket, TicketEvent

            with session_scope() as session:
                ticket = (
                    session.query(Ticket)
                    .filter(Ticket.ticket_no == ticket_no)
                    .one_or_none()
                )
                attempt = 0
                if ticket is not None:
                    ticket.retry_count = int(ticket.retry_count or 0) + 1
                    attempt = int(ticket.retry_count)
                session.add(
                    TicketEvent(
                        ticket_no=ticket_no,
                        event=event,
                        attempt=attempt,
                        detail=detail or {},
                    )
                )
        except Exception:  # noqa: BLE001
            pass

    def _mark_synced(self, ticket_no: str, merchant_ticket_no: str | None) -> None:
        """回写商家工单号与同步时间，并记 SYNCED 事件。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket, TicketEvent

            with session_scope() as session:
                ticket = (
                    session.query(Ticket)
                    .filter(Ticket.ticket_no == ticket_no)
                    .one_or_none()
                )
                if ticket is None:
                    return
                ticket.merchant_ticket_no = merchant_ticket_no
                ticket.synced_at = utcnow()
                session.add(
                    TicketEvent(
                        ticket_no=ticket_no,
                        event="SYNCED",
                        attempt=int(ticket.retry_count or 0),
                        detail={"merchant_ticket_no": merchant_ticket_no},
                    )
                )
        except Exception:  # noqa: BLE001
            pass

    # -- 补同步（本地队列） -------------------------------------------------

    def sync_pending(self, limit: int = 50) -> dict[str, int]:
        """补同步本地队列里未同步的工单（按 `ticket_retry_backoff` 退避）。

        Returns:
            `{"scanned": n, "synced": n, "failed": n, "skipped": n}`。
        """
        from app.core.config import get_settings

        backoff = list(get_settings().ticket_retry_backoff or [1, 2, 4, 8, 16])
        stats = {"scanned": 0, "synced": 0, "failed": 0, "skipped": 0}
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket

            with session_scope() as session:
                rows = (
                    session.query(Ticket)
                    .filter(
                        Ticket.merchant_ticket_no.is_(None),
                        Ticket.status != TicketStatus.CLOSED.value,
                    )
                    .order_by(Ticket.created_at)
                    .limit(limit)
                    .all()
                )
                pending = [
                    {
                        "ticket_no": r.ticket_no,
                        "session_id": r.session_id,
                        "reason": r.reason,
                        "priority": r.priority,
                        "retry_count": int(r.retry_count or 0),
                        "summary": r.summary or "",
                        "buyer_id": r.buyer_id or "",
                        "channel": r.channel,
                        "tried_actions": r.tried_actions or [],
                    }
                    for r in rows
                ]
        except Exception:  # noqa: BLE001
            return stats

        for row in pending:
            stats["scanned"] += 1
            retry = row["retry_count"]
            if retry >= len(backoff):
                stats["skipped"] += 1
                self._append_event(
                    row["ticket_no"], "FALLBACK_LOCAL", detail={"retry": retry}
                )
                continue
            try:
                run_maybe_async(self.limiter.acquire("ticket_create"))
            except RateLimitExceeded:
                stats["skipped"] += 1
                continue
            payload = {
                "session_id": row["session_id"],
                "ticket_no": row["ticket_no"],
                "reason": row["reason"],
                "priority": row["priority"],
                "channel": row["channel"],
                "summary": row["summary"],
                "buyer_id": row["buyer_id"],
                "entities": {},
                "tried_actions": row["tried_actions"],
                "transcript": [],
            }
            try:
                result = self.client.create_ticket(payload)
            except Exception:  # noqa: BLE001
                result = MerchantTicketResult(ok=False, message="调用异常")
            if result.ok:
                self._mark_synced(row["ticket_no"], result.ticket_no)
                stats["synced"] += 1
            else:
                self._append_event(
                    row["ticket_no"],
                    "RETRY",
                    detail={"error_code": result.error_code, "retry": retry + 1},
                )
                stats["failed"] += 1
        return stats

    # -- 查询（坐席工作台） -------------------------------------------------

    def get_ticket(self, ticket_no: str) -> dict[str, Any] | None:
        """按工单号取详情（含事件时间线）。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket

            with session_scope() as session:
                row = (
                    session.query(Ticket)
                    .filter(Ticket.ticket_no == ticket_no)
                    .one_or_none()
                )
                if row is None:
                    return None
                return {
                    "ticket_no": row.ticket_no,
                    "session_id": row.session_id,
                    "buyer_id": row.buyer_id,
                    "channel": row.channel,
                    "reason": row.reason,
                    "reason_cn": REASON_CN.get(
                        self._norm_reason(row.reason), row.reason
                    ),
                    "summary": row.summary,
                    "entities": row.entities,
                    "tried_actions": row.tried_actions,
                    "priority": row.priority,
                    "status": row.status,
                    "assignee": row.assignee,
                    "group_id": row.group_id,
                    "queue_position": row.queue_position,
                    "estimated_wait_seconds": row.estimated_wait_seconds,
                    "transcript": row.transcript,
                    "context_card": row.context_card,
                    "context_complete": row.context_complete,
                    "offline": row.offline,
                    "merchant_ticket_no": row.merchant_ticket_no,
                    "retry_count": row.retry_count,
                    "synced_at": row.synced_at.isoformat() if row.synced_at else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "events": [
                        {
                            "event": e.event,
                            "attempt": e.attempt,
                            "detail": e.detail,
                            "created_at": e.created_at.isoformat()
                            if e.created_at
                            else None,
                        }
                        for e in sorted(row.events, key=lambda x: x.created_at or utcnow())
                    ],
                }
        except Exception:  # noqa: BLE001
            return None

    def list_tickets(
        self,
        *,
        status: str | None = None,
        group_id: str | None = None,
        priority: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """列出工单（坐席工作台），返回 (items, total)。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket

            with session_scope() as session:
                query = session.query(Ticket)
                if status:
                    query = query.filter(Ticket.status == status)
                if group_id:
                    query = query.filter(Ticket.group_id == group_id)
                if priority:
                    query = query.filter(Ticket.priority == priority)
                total = int(query.count())
                rows = (
                    query.order_by(Ticket.created_at.desc())
                    .offset(max(offset, 0))
                    .limit(max(limit, 1))
                    .all()
                )
                items = [
                    {
                        "ticket_no": r.ticket_no,
                        "session_id": r.session_id,
                        "buyer_id": r.buyer_id,
                        "channel": r.channel,
                        "reason": r.reason,
                        "priority": r.priority,
                        "status": r.status,
                        "assignee": r.assignee,
                        "group_id": r.group_id,
                        "queue_position": r.queue_position,
                        "offline": r.offline,
                        "summary": r.summary,
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                    }
                    for r in rows
                ]
                return items, total
        except Exception:  # noqa: BLE001
            return [], 0

    # -- 归一工具 -----------------------------------------------------------

    @staticmethod
    def _norm_reason(value: HandoffReason | str | None) -> HandoffReason:
        """归一 `reason`；未知值保守归 `UNRESOLVED`。"""
        if isinstance(value, HandoffReason):
            return value
        try:
            return HandoffReason(str(value))
        except ValueError:
            return HandoffReason.UNRESOLVED

    @staticmethod
    def _norm_priority(
        value: Priority | str | None, reason: HandoffReason
    ) -> Priority:
        """归一优先级；None 时按 `reason` 推导。"""
        if value is None:
            return REASON_DEFAULT_PRIORITY.get(reason, Priority.NORMAL)
        if isinstance(value, Priority):
            return value
        try:
            return Priority(str(value))
        except ValueError:
            return REASON_DEFAULT_PRIORITY.get(reason, Priority.NORMAL)

    @staticmethod
    def _norm_channel(value: Channel | str | None) -> Channel | None:
        """归一渠道；未知值返回 None。"""
        if value is None:
            return None
        if isinstance(value, Channel):
            return value
        try:
            return Channel(str(value))
        except ValueError:
            return None

    @staticmethod
    def _to_status(value: Any) -> TicketStatus:
        """归一工单状态。"""
        try:
            return TicketStatus(str(value))
        except ValueError:
            return TicketStatus.OPEN

    @staticmethod
    def _to_priority(value: Any) -> Priority:
        """归一优先级。"""
        try:
            return Priority(str(value))
        except ValueError:
            return Priority.NORMAL

    @staticmethod
    def _to_dt(value: Any) -> datetime | None:
        """把 ISO 字符串还原成 datetime。"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _norm_transcript(
        transcript: Sequence[TranscriptTurn | dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        """归一对话摘录并截断到最近 20 轮（C-04 未确认前的保守值）。"""
        rows: list[dict[str, str]] = []
        for item in list(transcript or [])[-20:]:
            if isinstance(item, TranscriptTurn):
                rows.append({"role": item.role, "content": item.content})
            elif isinstance(item, dict):
                rows.append(
                    {
                        "role": str(item.get("role") or "user"),
                        "content": str(item.get("content") or ""),
                    }
                )
        return rows


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_SERVICE: HandoffService | None = None


def get_handoff_service() -> HandoffService:
    """进程内转人工服务单例。"""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = HandoffService()
    return _SERVICE


def set_handoff_service(service: HandoffService | None) -> None:
    """替换转人工服务单例（单测注入用；传 None 复位）。"""
    global _SERVICE
    _SERVICE = service


__all__ = [
    "HandoffOutcome",
    "HandoffService",
    "TICKET_IDEMPOTENCY_TTL",
    "TICKET_KEY_PREFIX",
    "WORKBENCH_TICKET_PATH",
    "get_handoff_service",
    "set_handoff_service",
]
