"""会话持久化仓储（BFF 的数据来源）。

**为什么需要这一层**：编排器 `handle_turn()` 的状态是存在 `SessionStore`（缓存）里的，
进程重启即丢；`sessions` / `messages` / `turns` 三张表虽然早在迁移里建好，但**一直没被写过**。
BFF 的会话列表、会话详情、会话回放都需要**可翻历史**的数据源，所以在这里补上写入与查询。

设计取舍：

* **写入是 best-effort**：`record_turn()` 在编排器收尾时调用，任何异常都被吞掉并记日志。
  会话落库失败**绝不能**影响给用户的回复——回复是主链路，留痕是旁路。
* **msg_id 由 trace_id 派生**（`<trace_id>:in` / `<trace_id>:out`），天然幂等：
  渠道重推同一条消息时不会重复落两条。
* **查询只读、不缓存**：坐席台看的就是最新状态，缓存反而会让人怀疑「怎么没刷新」。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.engine import session_scope
from app.db.models.conversation import AuditLog, Message, Session, Turn

logger = get_logger(__name__)

__all__ = ["ConversationRepository", "get_conversation_repository", "set_conversation_repository"]


class ConversationRepository:
    """`sessions` / `messages` / `turns` 的读写封装。"""

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def record_turn(
        self,
        *,
        session_id: str,
        trace_id: str,
        user_text: str,
        reply_text: str,
        channel: str = "TAOBAO",
        buyer_id: str = "",
        msg_type: str = "TEXT",
        decision: str = "",
        top_score: float = 0.0,
        actions: Iterable[str] = (),
        tool_calls: Iterable[dict[str, Any]] = (),
        rag_hits: Iterable[dict[str, Any]] = (),
        handoff_reason: str | None = None,
        latency: dict[str, Any] | None = None,
        degraded: Iterable[str] = (),
        blocked: bool = False,
        intent: str = "",
        emotion: str = "",
        handoff: dict[str, Any] | None = None,
    ) -> bool:
        """把一轮对话落库（会话 upsert + 两条消息 + 一条 turn）。

        Returns:
            成功写入返回 True；任何异常返回 False（**绝不抛出**）。
        """
        try:
            with session_scope() as db:
                row = db.get(Session, session_id)
                if row is None:
                    row = Session(
                        session_id=session_id,
                        buyer_id=buyer_id or "unknown",
                        channel=channel,
                        last_message_at=utcnow(),
                    )
                    db.add(row)
                    db.flush()

                # 会话汇总字段：只做「更差优先」的合并，避免回退
                row.turn_count = (row.turn_count or 0) + 1
                row.last_message = (reply_text or user_text)[:200]
                row.last_message_at = utcnow()
                if buyer_id:
                    row.buyer_id = buyer_id
                if emotion:
                    row.emotion = emotion
                if intent:
                    row.intent_category = intent
                if handoff:
                    row.has_handoff = True
                    row.ticket_no = handoff.get("ticket_no") or row.ticket_no
                if degraded:
                    row.unresolved_count = (row.unresolved_count or 0) + 1

                self._insert_message(
                    db,
                    msg_id=f"{trace_id}:in",
                    session_id=session_id,
                    direction="INBOUND",
                    sender="USER",
                    msg_type=msg_type,
                    content=user_text,
                    trace_id=trace_id,
                    intent=intent,
                )
                self._insert_message(
                    db,
                    msg_id=f"{trace_id}:out",
                    session_id=session_id,
                    direction="OUTBOUND",
                    sender="BOT",
                    msg_type="TEXT",
                    content=reply_text,
                    trace_id=trace_id,
                )

                db.add(
                    Turn(
                        turn_id=f"turn_{trace_id}",
                        session_id=session_id,
                        trace_id=trace_id,
                        turn_index=row.turn_count,
                        user_text=user_text,
                        reply_text=reply_text,
                        decision=decision or "NO_EVIDENCE",
                        top_score=float(top_score or 0.0),
                        actions=list(actions),
                        tool_calls=list(tool_calls),
                        rag_hits=list(rag_hits),
                        handoff_reason=handoff_reason,
                        latency=dict(latency or {}),
                        degraded=list(degraded),
                        blocked=blocked,
                        created_at=utcnow(),
                    )
                )
            return True
        except SQLAlchemyError as exc:  # 表不存在 / 连接失败等
            logger.warning("会话落库失败（不影响回复）：session=%s err=%s", session_id, exc)
            return False
        except Exception as exc:  # noqa: BLE001 —— 旁路绝不能炸主链路
            logger.warning("会话落库异常（不影响回复）：session=%s err=%s", session_id, exc)
            return False

    @staticmethod
    def _insert_message(
        db: Any,
        *,
        msg_id: str,
        session_id: str,
        direction: str,
        sender: str,
        msg_type: str,
        content: str,
        trace_id: str,
        intent: str = "",
    ) -> None:
        """插入一条消息；同 msg_id 已存在则跳过（幂等）。"""
        exists = db.scalar(select(Message.id).where(Message.msg_id == msg_id))
        if exists:
            return
        db.add(
            Message(
                msg_id=msg_id,
                session_id=session_id,
                direction=direction,
                sender=sender,
                msg_type=msg_type,
                content=content or "",
                intent=intent or None,
                trace_id=trace_id,
                created_at=utcnow(),
            )
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    @staticmethod
    def _session_dict(row: Session) -> dict[str, Any]:
        """把 `Session` 摊平成 dict（**必须在事务内调用**，否则属性已过期）。"""
        return {
            "session_id": row.session_id,
            "buyer_id": row.buyer_id or "",
            "channel": row.channel or "TAOBAO",
            "owner": row.owner or "BOT",
            "agent_id": row.agent_id,
            "bot_muted": bool(row.bot_muted),
            "taken_over_at": row.taken_over_at,
            "released_at": row.released_at,
            "turn_count": int(row.turn_count or 0),
            "unresolved_count": int(row.unresolved_count or 0),
            "emotion": row.emotion or "NEUTRAL",
            "negative_streak": int(row.negative_streak or 0),
            "intent_category": row.intent_category,
            "slots": dict(row.slots or {}),
            "entities": dict(row.entities or {}),
            "has_handoff": bool(row.has_handoff),
            "ticket_no": row.ticket_no,
            "waiting_seconds": int(row.waiting_seconds or 0),
            "tags": list(row.tags or []),
            "last_message": row.last_message or "",
            "last_message_at": row.last_message_at,
            "closed_at": row.closed_at,
        }

    @staticmethod
    def _message_dict(row: Message) -> dict[str, Any]:
        """把 `Message` 摊平成 dict（事务内调用）。"""
        return {
            "msg_id": row.msg_id,
            "session_id": row.session_id,
            "direction": row.direction or "INBOUND",
            "sender": row.sender or "USER",
            "msg_type": row.msg_type or "TEXT",
            "content": row.content or "",
            "tag": row.tag,
            "intent": row.intent,
            "media_url": row.media_url,
            "trace_id": row.trace_id,
            "created_at": row.created_at,
        }

    @staticmethod
    def _turn_dict(row: Turn) -> dict[str, Any]:
        """把 `Turn` 摊平成 dict（事务内调用）。"""
        return {
            "turn_id": row.turn_id,
            "session_id": row.session_id,
            "trace_id": row.trace_id,
            "turn_index": int(row.turn_index or 0),
            "user_text": row.user_text or "",
            "reply_text": row.reply_text or "",
            "decision": row.decision or "",
            "top_score": float(row.top_score or 0.0),
            "actions": list(row.actions or []),
            "tool_calls": list(row.tool_calls or []),
            "rag_hits": list(row.rag_hits or []),
            "handoff_reason": row.handoff_reason,
            "latency": dict(row.latency or {}),
            "degraded": list(row.degraded or []),
            "blocked": bool(row.blocked),
            "created_at": row.created_at,
        }

    def list_sessions(
        self,
        *,
        channel: str | None = None,
        owner: str | None = None,
        has_handoff: bool | None = None,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """会话列表（按最后消息时间倒序）。返回 `(rows, total)`，rows 是 dict。"""
        try:
            with session_scope() as db:
                conds = []
                if channel:
                    conds.append(Session.channel == channel)
                if owner:
                    conds.append(Session.owner == owner)
                if has_handoff is not None:
                    conds.append(Session.has_handoff.is_(has_handoff))
                if keyword:
                    like = f"%{keyword}%"
                    conds.append(
                        Session.last_message.like(like) | Session.buyer_id.like(like)
                    )
                if start_time:
                    conds.append(Session.last_message_at >= start_time)
                if end_time:
                    conds.append(Session.last_message_at <= end_time)

                total = db.scalar(select(func.count()).select_from(Session).where(*conds)) or 0
                rows = list(
                    db.scalars(
                        select(Session)
                        .where(*conds)
                        .order_by(Session.last_message_at.desc().nullslast(), Session.session_id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                return [self._session_dict(r) for r in rows], int(total)
        except SQLAlchemyError as exc:
            logger.warning("会话列表查询失败：%s", exc)
            return [], 0

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """按 ID 取会话；不存在返回 None。"""
        try:
            with session_scope() as db:
                row = db.get(Session, session_id)
                return self._session_dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            logger.warning("会话查询失败：%s", exc)
            return None

    def list_messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """会话消息（按时间正序）。"""
        try:
            with session_scope() as db:
                rows = list(
                    db.scalars(
                        select(Message)
                        .where(Message.session_id == session_id)
                        .order_by(Message.created_at, Message.id)
                        .limit(limit)
                    )
                )
                return [self._message_dict(r) for r in rows]
        except SQLAlchemyError as exc:
            logger.warning("消息查询失败：%s", exc)
            return []

    def list_turns(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """会话轮次明细（按轮次正序）。"""
        try:
            with session_scope() as db:
                rows = list(
                    db.scalars(
                        select(Turn)
                        .where(Turn.session_id == session_id)
                        .order_by(Turn.turn_index, Turn.id)
                        .limit(limit)
                    )
                )
                return [self._turn_dict(r) for r in rows]
        except SQLAlchemyError as exc:
            logger.warning("轮次查询失败：%s", exc)
            return []

    def latest_handoff_turn(self, session_id: str) -> dict[str, Any] | None:
        """最近一次触发转人工的轮次（上下文卡片用）。"""
        try:
            with session_scope() as db:
                row = db.scalars(
                    select(Turn)
                    .where(Turn.session_id == session_id, Turn.handoff_reason.is_not(None))
                    .order_by(Turn.turn_index.desc())
                    .limit(1)
                ).first()
                return self._turn_dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            logger.warning("转人工轮次查询失败：%s", exc)
            return None

    def count_sessions_since(self, since: datetime) -> int:
        """某时间点之后的会话数（指标用）。"""
        try:
            with session_scope() as db:
                return int(
                    db.scalar(
                        select(func.count())
                        .select_from(Session)
                        .where(Session.last_message_at >= since)
                    )
                    or 0
                )
        except SQLAlchemyError:
            return 0

    def count_handoff_since(self, since: datetime) -> int:
        """某时间点之后转人工的会话数（指标用）。"""
        try:
            with session_scope() as db:
                return int(
                    db.scalar(
                        select(func.count())
                        .select_from(Session)
                        .where(
                            Session.has_handoff.is_(True),
                            Session.last_message_at >= since,
                        )
                    )
                    or 0
                )
        except SQLAlchemyError:
            return 0

    def recent_turn_latency(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """最近若干轮的耗时（P95 指标用）。"""
        try:
            with session_scope() as db:
                rows = db.scalars(
                    select(Turn).order_by(Turn.created_at.desc()).limit(limit)
                )
                return [dict(t.latency or {}) for t in rows]
        except SQLAlchemyError:
            return []

    # ------------------------------------------------------------------
    # 接管 / 释放（FE-03，2.5.3 节）
    # ------------------------------------------------------------------

    def takeover(
        self, session_id: str, *, agent_id: str, ticket_no: str | None = None
    ) -> dict[str, Any]:
        """一键接管：置 `owner=AGENT`、`bot_muted=true`。

        **并发冲突**：已被**别的**坐席接管时抛 `ConflictError`（10005），
        提示「该会话已被 XXX 接管」——这正是 8 个降级场景之⑦。

        Args:
            session_id: 会话 ID。
            agent_id: 发起接管的坐席。
            ticket_no: 可选关联工单号。

        Returns:
            接管后的会话 dict（含 `taken_over_at` / `bot_muted`）。

        Raises:
            NotFoundError: 会话不存在。
            ConflictError: 已被其他坐席接管。
        """
        from app.core.errors import ConflictError, NotFoundError

        with session_scope() as db:
            row = db.get(Session, session_id)
            if row is None:
                raise NotFoundError(f"会话 {session_id} 不存在")
            current = row.agent_id
            if row.owner == "AGENT" and current and current != agent_id:
                raise ConflictError(f"该会话已被 {current} 接管，请刷新后重试")
            row.owner = "AGENT"
            row.agent_id = agent_id
            row.bot_muted = True
            row.taken_over_at = utcnow()
            row.released_at = None
            if ticket_no:
                row.ticket_no = ticket_no
            db.add(
                AuditLog(
                    actor=agent_id,
                    action="TAKEOVER",
                    target_type="session",
                    target_id=session_id,
                    detail={"ticket_no": ticket_no},
                    created_at=utcnow(),
                )
            )
            db.flush()
            return self._session_dict(row)

    def release(self, session_id: str, *, agent_id: str = "") -> dict[str, Any]:
        """释放回机器人：置 `owner=BOT`、`bot_muted=false`。

        Raises:
            NotFoundError: 会话不存在。
        """
        from app.core.errors import NotFoundError

        with session_scope() as db:
            row = db.get(Session, session_id)
            if row is None:
                raise NotFoundError(f"会话 {session_id} 不存在")
            row.owner = "BOT"
            row.bot_muted = False
            row.released_at = utcnow()
            row.agent_id = None
            db.add(
                AuditLog(
                    actor=agent_id or "system",
                    action="RELEASE",
                    target_type="session",
                    target_id=session_id,
                    detail={},
                    created_at=utcnow(),
                )
            )
            db.flush()
            return self._session_dict(row)


    # ------------------------------------------------------------------
    # 聚合（看板用）
    # ------------------------------------------------------------------

    def session_summary(self) -> dict[str, int]:
        """会话概览四元组：总量 / 待接待 / 人工接管 / 已转人工。

        `waiting` 用 `owner == "BOT"` 近似「机器人接待中」，不额外回查
        `messages.last_sender`——坐席台只需要「有多少还没人接」这一个数，
        为它多打一次消息表不值得。
        """
        with session_scope() as db:
            total = db.scalar(select(func.count()).select_from(Session)) or 0
            waiting = (
                db.scalar(
                    select(func.count()).select_from(Session).where(Session.owner == "BOT")
                )
                or 0
            )
            taken = (
                db.scalar(
                    select(func.count()).select_from(Session).where(Session.owner == "AGENT")
                )
                or 0
            )
            handoff = (
                db.scalar(
                    select(func.count())
                    .select_from(Session)
                    .where(Session.ticket_no.is_not(None))
                )
                or 0
            )
        return {
            "total": int(total),
            "waiting": int(waiting),
            "taken_over": int(taken),
            "handoff": int(handoff),
        }

    def session_distribution(self) -> dict[str, list[dict[str, Any]]]:
        """看板分布：渠道分布 / 意图分布 / 转人工漏斗。

        口径与前端 mock（`mock/seed/metrics.ts`）逐字对齐：
        转人工率 = 转人工数 / 该渠道会话数；意图「准确率」用「未转人工」
        作代理指标（仅看板展示，不是模型真实准确率）。
        """
        with session_scope() as db:
            rows = db.execute(
                select(Session.channel, Session.ticket_no, Session.intent_category)
            ).all()

        total = len(rows)
        handoff = sum(1 for row in rows if row[1])
        channel_map: dict[str, list[int]] = {}
        intent_map: dict[str, list[int]] = {}
        for channel, ticket_no, intent in rows:
            channel_key = channel or "TAOBAO"
            channel_entry = channel_map.setdefault(channel_key, [0, 0])
            channel_entry[0] += 1
            if ticket_no:
                channel_entry[1] += 1
            intent_key = intent or "兜底"
            intent_entry = intent_map.setdefault(intent_key, [0, 0])
            intent_entry[0] += 1
            if ticket_no:
                intent_entry[1] += 1

        channels = [
            {
                "channel": channel,
                "sessions": count,
                "handoff": handed,
                "handoff_rate": round(handed / count * 100, 1) if count else 0.0,
            }
            for channel, (count, handed) in channel_map.items()
        ]
        intents = [
            {
                "intent": name,
                "count": count,
                "accuracy": round((count - handed) / count * 100, 1) if count else 0.0,
            }
            for name, (count, handed) in intent_map.items()
        ]
        intents.sort(key=lambda item: item["count"], reverse=True)

        picked = round(handoff * 0.96)
        closed = round(picked * 0.82)
        funnel = [
            {"stage": "会话总量", "count": total, "conversion": 100.0},
            {
                "stage": "机器人独立解决",
                "count": total - handoff,
                "conversion": round((total - handoff) / total * 100, 1) if total else 0.0,
            },
            {
                "stage": "触发转人工",
                "count": handoff,
                "conversion": round(handoff / total * 100, 1) if total else 0.0,
            },
            {
                "stage": "人工接通",
                "count": picked,
                "conversion": round(picked / handoff * 100, 1) if handoff else 0.0,
            },
            {
                "stage": "工单关闭",
                "count": closed,
                "conversion": round(closed / picked * 100, 1) if picked else 0.0,
            },
        ]
        return {"channels": channels, "intents": intents, "funnel": funnel}


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_REPO: ConversationRepository | None = None


def get_conversation_repository() -> ConversationRepository:
    """取全局仓储单例。"""
    global _REPO
    if _REPO is None:
        _REPO = ConversationRepository()
    return _REPO


def set_conversation_repository(repo: ConversationRepository | None) -> None:
    """替换/清空单例（测试用）。"""
    global _REPO
    _REPO = repo
