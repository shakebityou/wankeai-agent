"""账号服务 —— 登录、登出、会话解析、改密、账号管理、操作日志。

**业务口径与前端 `frontend/mock/services/auth-service.ts` 逐条对齐**（前端已有
页面与交互，后端换个口径就会让同一份前端代码在两套环境下表现不同）：

* 连续 5 次密码错误 → 锁定 10 分钟，期间登录返回 429/20001（前端据此倒计时）；
* 账号不存在也跑一次等价的哈希校验，让响应时间与「密码错」一致；
* 停用账号返回「该账号已停用，请联系管理员」；
* 登录态 12 小时，库里只存 token 的 sha256；
* 改密 / 重置 / 停用都吊销该账号全部会话（否则改完密码旧标签页还能用）；
* 坐席只能看本组渠道，主管/管理员/知识库运营看全渠道。

**为什么每个方法自己开 `session_scope()`**：这些方法要么被路由直接调用，要么被
`verify_agent` 调用，调用方没有现成的 DB 会话；仓储层（`app/db/repository.py`）
也是这个模式。代价是跨方法无法共享事务——账号操作都是单行写入，不构成问题。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from app.core.errors import (
    AppError,
    BffErrorCode,
    NotFoundError,
    TooManyRequestsError,
    ValidationError,
)
from app.db.engine import session_scope
from app.db.models.auth import (
    ROLE_AGENT,
    ROLE_CODES,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_LOCKED,
    USER_STATUSES,
    AuthSession,
    User,
)
from app.db.models.conversation import AuditLog
from app.db.models.ticket import AgentGroup
from app.services.auth.password import (
    DECOY_HASH,
    generate_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from app.services.auth.seed import ensure_seeded

logger = logging.getLogger(__name__)

#: 连续失败多少次后锁定（与前端 `MAX_FAILED_ATTEMPTS` 一致）
MAX_FAILED_ATTEMPTS = 5

#: 锁定时长（分钟）
LOCK_MINUTES = 10

#: 登录态有效期（小时）
SESSION_TTL_HOURS = 12

#: 全渠道（主管/管理员/知识库运营可见）
ALL_CHANNELS: tuple[str, ...] = ("TAOBAO", "TMALL", "PDD", "DOUYIN", "WECHAT")

#: 账号格式：3–32 位字母/数字/点/下划线/短横线
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


def _cn_to_code() -> dict[str, str]:
    """中文渠道名 → 渠道枚举值（`agent_groups.channels` 存的是中文）。

    BE-14 的坐席路由用中文渠道名（`淘宝`/`天猫`…）匹配分组，而前端权限矩阵
    用的是枚举值（`TAOBAO`/`TMALL`…）。BFF 返回前必须换回枚举值，否则前端
    「按渠道筛选」永远筛不到东西。
    """
    from app.services.handoff.routing import CHANNEL_CN  # noqa: PLC0415

    return {cn: channel.value for channel, cn in CHANNEL_CN.items()}


def _now() -> datetime:
    """当前 UTC 时间（库里的时间列统一 UTC）。"""
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    """时间 → ISO 8601 字符串（前端 `Date.parse` 可直接吃）。"""
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class LoginOk:
    """登录成功结果。"""

    token: str
    expires_at: datetime
    user: dict[str, Any]


class AuthService:
    """账号服务（无状态，每次调用自开会话）。"""

    # ------------------------------------------------------------------
    # 登录 / 登出 / 会话
    # ------------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str = "",
        user_agent: str = "",
    ) -> LoginOk:
        """登录。

        Args:
            username: 登录账号（大小写不敏感）。
            password: 明文口令。
            ip: 客户端 IP（写审计与 `last_login_ip`）。
            user_agent: 客户端 UA。

        Returns:
            登录成功结果（token + 过期时间 + 用户视图）。

        Raises:
            ValidationError: 账号或密码为空。
            AppError(10002): 账号不存在、密码错、账号已停用。
            TooManyRequestsError: 账号被锁定，`retry_after` 为剩余秒数。
        """
        ensure_seeded()
        name = (username or "").strip()
        if not name or not password:
            raise ValidationError("请输入账号和密码")

        # 失败时**必须把 failed_count / 锁定状态落库**，所以不能在 `with` 里直接
        # 抛异常——`session_scope()` 遇异常会 rollback，失败计数就永远不增长，
        # 账号锁死形同虚设。这里先把结论存进 `error`，正常退出提交后再抛。
        error: AppError | None = None
        outcome: LoginOk | None = None
        with session_scope() as db:
            row = db.scalar(select(User).where(User.username == name.lower()))

            # 账号不存在也跑一次等价校验，避免靠响应时间枚举账号。
            if row is None:
                verify_password(password, DECOY_HASH)
                error = AppError(BffErrorCode.UNAUTHENTICATED, "账号或密码不正确")
            elif row.status == STATUS_DISABLED:
                error = AppError(
                    BffErrorCode.UNAUTHENTICATED, "该账号已停用，请联系管理员"
                )
            else:
                now = _now()
                if row.status == STATUS_LOCKED:
                    remain = 0.0
                    if row.locked_until is not None:
                        remain = (row.locked_until - now).total_seconds()
                    if remain > 0:
                        error = TooManyRequestsError(
                            f"登录失败次数过多，请 {math.ceil(remain / 60)} 分钟后再试",
                            retry_after=math.ceil(remain),
                        )
                    else:
                        # 锁定已到期：自动解锁
                        row.status = STATUS_ACTIVE
                        row.failed_count = 0
                        row.locked_until = None

                if error is None:
                    if not verify_password(password, row.password_hash):
                        failed = (row.failed_count or 0) + 1
                        row.updated_at = now
                        if failed >= MAX_FAILED_ATTEMPTS:
                            row.failed_count = failed
                            row.status = STATUS_LOCKED
                            row.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                            self._audit(
                                db,
                                actor=row.display_name,
                                action="LOGIN_LOCKED",
                                target=row.username,
                                detail={
                                    "failed_attempts": failed,
                                    "actor_id": row.user_id,
                                },
                                ip=ip,
                                user_agent=user_agent,
                            )
                            error = TooManyRequestsError(
                                f"连续 {MAX_FAILED_ATTEMPTS} 次密码错误，账号已锁定 "
                                f"{LOCK_MINUTES} 分钟",
                                retry_after=LOCK_MINUTES * 60,
                            )
                        else:
                            row.failed_count = failed
                            error = AppError(
                                BffErrorCode.UNAUTHENTICATED,
                                f"账号或密码不正确，还可尝试 "
                                f"{MAX_FAILED_ATTEMPTS - failed} 次",
                            )
                    else:
                        # 登录成功
                        token = generate_token()
                        expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
                        row.failed_count = 0
                        row.status = STATUS_ACTIVE
                        row.locked_until = None
                        row.last_login_at = now
                        row.last_login_ip = ip
                        row.updated_at = now
                        db.add(
                            AuthSession(
                                token_hash=hash_token(token),
                                user_id=row.user_id,
                                issued_at=now,
                                expires_at=expires_at,
                                revoked_at=None,
                                user_agent=(user_agent or "")[:255],
                            )
                        )
                        self._audit(
                            db,
                            actor=row.display_name,
                            action="LOGIN",
                            target=row.username,
                            detail={"actor_id": row.user_id},
                            ip=ip,
                            user_agent=user_agent,
                        )
                        outcome = LoginOk(
                            token=token,
                            expires_at=expires_at,
                            user=self._to_view(db, row),
                        )

        if error is not None:
            raise error
        assert outcome is not None  # noqa: S101 - 逻辑上必然已赋值
        return outcome

    def logout(self, token: str, *, ip: str = "") -> None:
        """登出（把登录态置为已吊销）。

        找不到 token 也静默成功——前端登出不该因为「会话早就过期」而报错。
        """
        if not token:
            return
        with session_scope() as db:
            row = db.get(AuthSession, hash_token(token))
            if row is None or row.revoked_at is not None:
                return
            row.revoked_at = _now()
            user = db.get(User, row.user_id)
            self._audit(
                db,
                actor=user.display_name if user else "unknown",
                action="LOGOUT",
                target=user.username if user else row.user_id,
                detail={"actor_id": row.user_id},
                ip=ip,
            )

    def resolve_token(self, token: str) -> dict[str, Any] | None:
        """解析登录态 → 用户视图；无效返回 None。

        三种失效情形都返回 None：已吊销、已过期、账号已非 ACTIVE。
        """
        if not token:
            return None
        with session_scope() as db:
            row = db.get(AuthSession, hash_token(token))
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is None or row.expires_at <= _now():
                return None
            user = db.get(User, row.user_id)
            if user is None or user.status != STATUS_ACTIVE:
                return None
            return self._to_view(db, user)

    def me(self, user_id: str) -> dict[str, Any]:
        """当前登录用户视图。"""
        with session_scope() as db:
            row = db.get(User, user_id)
            if row is None:
                raise AppError(BffErrorCode.UNAUTHENTICATED, "账号不存在")
            return self._to_view(db, row)

    # ------------------------------------------------------------------
    # 修改自己的密码
    # ------------------------------------------------------------------

    def change_password(
        self,
        user_id: str,
        old_password: str,
        new_password: str,
        *,
        ip: str = "",
    ) -> None:
        """修改自己的密码（成功后吊销该账号全部会话）。"""
        with session_scope() as db:
            row = db.get(User, user_id)
            if row is None:
                raise ValidationError("账号不存在")
            if not verify_password(old_password, row.password_hash):
                raise ValidationError("原密码不正确")
            if old_password == new_password:
                raise ValidationError("新密码不能与原密码相同")
            ok, message = validate_password_strength(new_password)
            if not ok:
                raise ValidationError(message)

            now = _now()
            row.password_hash = hash_password(new_password)
            row.must_change = False
            row.updated_at = now
            self._revoke_sessions(db, row.user_id, now=now)
            self._audit(
                db,
                actor=row.display_name,
                action="CHANGE_PASSWORD",
                target=row.username,
                detail={"actor_id": row.user_id},
                ip=ip,
            )

    # ------------------------------------------------------------------
    # 账号管理（仅管理员）
    # ------------------------------------------------------------------

    def list_users(
        self,
        *,
        keyword: str = "",
        role: str = "",
        status: str = "",
        group_id: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        """账号列表 + 全量统计。

        Returns:
            `(当前页视图列表, 过滤后总数, 全量统计)`。
        """
        with session_scope() as db:
            rows = list(db.scalars(select(User).order_by(User.user_id)))
            groups = self._groups(db)
            views = [self._to_view(db, row, groups=groups) for row in rows]

            summary = {
                "total": len(views),
                "active": sum(1 for v in views if v["status"] == STATUS_ACTIVE),
                "disabled": sum(1 for v in views if v["status"] == STATUS_DISABLED),
                "locked": sum(1 for v in views if v["status"] == STATUS_LOCKED),
                "by_role": {code: sum(1 for v in views if v["role"] == code) for code in ROLE_CODES},
            }

        items = views
        key = (keyword or "").strip().lower()
        if key:
            items = [
                v
                for v in items
                if key in v["username"].lower() or key in v["display_name"].lower()
            ]
        if role:
            items = [v for v in items if v["role"] == role]
        if status:
            items = [v for v in items if v["status"] == status]
        if group_id:
            items = [v for v in items if v["group_id"] == group_id]

        total = len(items)
        size = max(1, min(50, int(page_size or 20)))
        start = max(0, (max(1, int(page or 1)) - 1) * size)
        return items[start : start + size], total, summary

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        role: str = ROLE_AGENT,
        group_id: str | None = None,
        password: str = "",
        actor_id: str = "",
        actor_name: str = "",
        ip: str = "",
    ) -> dict[str, Any]:
        """新建账号。"""
        from app.db.models.auth import DEMO_PASSWORD  # noqa: PLC0415

        name = (username or "").strip()
        display = (display_name or "").strip()
        if not name:
            raise ValidationError("登录账号不能为空")
        if not _USERNAME_RE.match(name):
            raise ValidationError("账号需 3–32 位，只允许字母、数字、点、下划线、短横线")
        if not display:
            raise ValidationError("姓名不能为空")

        with session_scope() as db:
            dup = db.scalar(select(User).where(User.username == name.lower()))
            if dup is not None:
                raise ValidationError(f"账号「{name}」已存在")
            if role not in ROLE_CODES:
                raise ValidationError(f"未知角色 {role}")
            if group_id and db.get(AgentGroup, group_id) is None:
                raise ValidationError(f"未知分组 {group_id}")

            raw_password = password or DEMO_PASSWORD
            ok, message = validate_password_strength(raw_password)
            if not ok:
                raise ValidationError(message)

            now = _now()
            user = User(
                user_id=self._next_user_id(db),
                username=name.lower(),
                display_name=display,
                role_code=role,
                group_id=group_id or None,
                password_hash=hash_password(raw_password),
                status=STATUS_ACTIVE,
                failed_count=0,
                must_change=True,
                last_login_ip="",
                created_by=actor_name or actor_id or "system",
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            db.flush()
            self._audit(
                db,
                actor=actor_name or actor_id or "system",
                action="USER_CREATE",
                target=user.username,
                detail={
                    "actor_id": actor_id,
                    "user_id": user.user_id,
                    "role": role,
                    "group_id": group_id,
                },
                ip=ip,
            )
            return self._to_view(db, user)

    def update_user(
        self,
        user_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str = "",
        actor_name: str = "",
        ip: str = "",
    ) -> dict[str, Any]:
        """编辑账号（姓名 / 角色 / 分组 / 状态）。"""
        with session_scope() as db:
            row = db.get(User, user_id)
            if row is None:
                raise ValidationError("账号不存在")

            changes: list[str] = []
            now = _now()

            if "display_name" in payload and payload["display_name"] is not None:
                display = str(payload["display_name"]).strip()
                if not display:
                    raise ValidationError("姓名不能为空")
                if display != row.display_name:
                    row.display_name = display
                    changes.append("display_name")

            if "role" in payload and payload["role"] is not None:
                role = str(payload["role"])
                if role not in ROLE_CODES:
                    raise ValidationError(f"未知角色 {role}")
                if role != row.role_code:
                    if user_id == actor_id:
                        raise ValidationError("不能修改自己的角色，请让另一位管理员操作")
                    row.role_code = role
                    changes.append("role")

            if "group_id" in payload:
                group_id = payload["group_id"]
                if group_id:
                    if db.get(AgentGroup, str(group_id)) is None:
                        raise ValidationError(f"未知分组 {group_id}")
                    group_id = str(group_id)
                if (group_id or None) != row.group_id:
                    row.group_id = group_id or None
                    changes.append("group_id")

            if "status" in payload and payload["status"] is not None:
                status = str(payload["status"])
                if status not in USER_STATUSES:
                    raise ValidationError(f"未知状态 {status}")
                if status != row.status:
                    if status == STATUS_DISABLED and user_id == actor_id:
                        raise ValidationError("不能禁用自己")
                    row.status = status
                    if status == STATUS_ACTIVE:
                        row.failed_count = 0
                        row.locked_until = None
                    else:
                        self._revoke_sessions(db, row.user_id, now=now)
                    changes.append("status")

            if not changes:
                raise ValidationError("没有需要修改的字段")

            row.updated_at = now
            self._audit(
                db,
                actor=actor_name or actor_id or "system",
                action="USER_UPDATE",
                target=row.username,
                detail={
                    "actor_id": actor_id,
                    "user_id": row.user_id,
                    "changes": changes,
                },
                ip=ip,
            )
            return self._to_view(db, row)

    def reset_password(
        self,
        user_id: str,
        new_password: str,
        *,
        actor_id: str = "",
        actor_name: str = "",
        ip: str = "",
    ) -> str:
        """管理员重置他人密码，返回账号名（前端提示用）。"""
        with session_scope() as db:
            row = db.get(User, user_id)
            if row is None:
                raise ValidationError("账号不存在")
            ok, message = validate_password_strength(new_password)
            if not ok:
                raise ValidationError(message)

            now = _now()
            row.password_hash = hash_password(new_password)
            row.must_change = True
            row.status = STATUS_ACTIVE
            row.failed_count = 0
            row.locked_until = None
            row.updated_at = now
            self._revoke_sessions(db, row.user_id, now=now)
            self._audit(
                db,
                actor=actor_name or actor_id or "system",
                action="USER_RESET_PASSWORD",
                target=row.username,
                detail={"actor_id": actor_id, "user_id": row.user_id},
                ip=ip,
            )
            return row.username

    # ------------------------------------------------------------------
    # 字典与日志
    # ------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, Any]]:
        """分组字典（登录页与账号表单的下拉用）。"""
        with session_scope() as db:
            return list(self._groups(db).values())

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        """操作日志（倒序，最多 500 条）。"""
        size = max(1, min(500, int(limit or 100)))
        with session_scope() as db:
            rows = db.scalars(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(size)
            )
            return [
                {
                    "id": row.id,
                    "at": _iso(row.created_at),
                    "actor_name": row.actor,
                    "action": row.action,
                    "target": row.target_id or "",
                    "detail": row.detail,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _groups(self, db: Any) -> dict[str, dict[str, Any]]:
        """分组字典：group_id → 视图（渠道已换成枚举值）。"""
        cn_to_code = _cn_to_code()
        result: dict[str, dict[str, Any]] = {}
        for group in db.scalars(select(AgentGroup).order_by(AgentGroup.group_id)):
            result[group.group_id] = {
                "group_id": group.group_id,
                "group_name": group.group_name,
                "leader": group.supervisor,
                "headcount": group.agent_count,
                "channels": [cn_to_code.get(str(c), str(c)) for c in (group.channels or [])],
            }
        return result

    def _to_view(
        self,
        db: Any,
        user: User,
        *,
        groups: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """ORM 用户 → 前端视图（**绝不含 password_hash**）。"""
        group_map = groups if groups is not None else self._groups(db)
        group = group_map.get(user.group_id or "")
        if user.role_code == ROLE_AGENT:
            channels = list(group["channels"]) if group else []
            if not channels:
                channels = list(ALL_CHANNELS)
        else:
            channels = list(ALL_CHANNELS)
        return {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role_code,
            "group_id": user.group_id,
            "group_name": group["group_name"] if group else None,
            "channels": channels,
            "status": user.status,
            "must_change": bool(user.must_change),
            "last_login_at": _iso(user.last_login_at),
            "created_at": _iso(user.created_at),
            "updated_at": _iso(user.updated_at),
            "created_by": user.created_by,
        }

    @staticmethod
    def _revoke_sessions(db: Any, user_id: str, *, now: datetime) -> None:
        """吊销该账号全部未失效会话。"""
        for row in db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        ):
            row.revoked_at = now

    @staticmethod
    def _next_user_id(db: Any) -> str:
        """下一个账号 ID（`U` + 4 位序号）。

        取现有最大序号 +1 而不是「条数 +1」——删过账号后条数会重复。
        """
        ids = list(db.scalars(select(User.user_id)))
        max_no = 0
        for raw in ids:
            digits = raw[1:] if raw[:1].upper() == "U" else raw
            if digits.isdigit():
                max_no = max(max_no, int(digits))
        return f"U{max_no + 1:04d}"

    @staticmethod
    def _audit(
        db: Any,
        *,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any] | None = None,
        ip: str = "",
        user_agent: str = "",
    ) -> None:
        """写操作日志（账号变更是审计重点）。"""
        db.add(
            AuditLog(
                actor=actor,
                action=action,
                target_type="user",
                target_id=target,
                detail=detail or {},
                ip=ip,
                user_agent=(user_agent or "")[:255],
                created_at=_now(),
            )
        )


#: 全局单例（无状态，可直接复用）
_SERVICE: AuthService | None = None


def get_auth_service() -> AuthService:
    """取账号服务单例。"""
    global _SERVICE  # noqa: PLW0603
    if _SERVICE is None:
        _SERVICE = AuthService()
    return _SERVICE


def set_auth_service(service: AuthService | None) -> None:
    """替换/清空单例（测试用）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = service


__all__ = [
    "ALL_CHANNELS",
    "LOCK_MINUTES",
    "MAX_FAILED_ATTEMPTS",
    "SESSION_TTL_HOURS",
    "AuthService",
    "LoginOk",
    "get_auth_service",
    "set_auth_service",
]
