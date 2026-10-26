"""账号与登录态模型（第 31 条 BFF 账号体系）。

三张表：
* `roles` —— 角色字典（`AGENT` / `SUPERVISOR` / `KB_OPERATOR` / `ADMIN`）
* `users` —— 账号主体（口令只存 scrypt 哈希）
* `auth_sessions` —— 登录态（库里只存 token 的 sha256）

**为什么表结构与前端 `frontend/mock/db/schema.sql` 对齐**：前端坐席台已经按
那套字段写好了页面与权限矩阵（`src/permission/matrix.ts`），后端另造一套字段
名会让联调时每一处都要翻译一次。字段名、状态枚举、口令哈希格式全部照抄，
后端只把 SQLite 的 TEXT 时间戳换成 `UTCDateTime`。

**为什么复用 `agent_groups` 而不新建 `groups` 表**：转人工路由（BE-14）已经
在 `agent_groups` 里维护了 G1–G6 的分组、渠道、在线时段；账号体系再建一张
`groups` 会出现「两处都能改分组」的双写问题。账号只外键引用 `agent_groups`，
分组信息以它为准。

**为什么 `username` 统一存小写**：前端要求「账号全局唯一且大小写不敏感」
（`idx_users_username_nocase`）。SQLite 的 `COLLATE NOCASE`、PostgreSQL 的
`citext`、SQL Server 的排序规则三套写法互不通用；**入库前统一转小写**是唯一
跨库一致的方案，代价是原始大小写不保留——账号本身不承载展示信息，
展示用 `display_name`。

**`status` 为什么不用 CheckConstraint**：三种状态（ACTIVE/DISABLED/LOCKED）由
`app/services/auth/service.py` 集中校验，加数据库约束会让「锁定到期自动解锁」
这类批量更新在跨库时多一层方言差异（SQLite 的 CHECK 与 MSSQL 的约束名规则不同），
收益不抵成本。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.db.types import dt_col, str_col

#: 账号状态（与前端 `UserRow.status` 一致）
STATUS_ACTIVE = "ACTIVE"
STATUS_DISABLED = "DISABLED"
STATUS_LOCKED = "LOCKED"
USER_STATUSES = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_LOCKED)

#: 角色代码（与前端 `src/permission/matrix.ts` 的 `UserRole` 一一对应）
ROLE_AGENT = "AGENT"
ROLE_SUPERVISOR = "SUPERVISOR"
ROLE_KB_OPERATOR = "KB_OPERATOR"
ROLE_ADMIN = "ADMIN"
ROLE_CODES = (ROLE_AGENT, ROLE_SUPERVISOR, ROLE_KB_OPERATOR, ROLE_ADMIN)

#: 口令哈希参数（RFC 7914 scrypt；与前端 `mock/db/password.ts` 完全一致）
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LEN = 64
SCRYPT_SALT_LEN = 16
#: 演示环境统一口令（生产环境必须改为随机初始密码 + 首登强制修改）
DEMO_PASSWORD = "Wk@2026demo"


class Role(Base):
    """角色字典。

    单独建表而不是硬编码枚举：将来加「质检员」这类角色时不用动 `users` 表，
    也不用发一次带数据迁移的版本。

    Attributes:
        role_code: 角色代码（主键），全大写。
        role_name: 中文名（「坐席」「客服主管」…）。
        landing: 登录后的默认落地页；空表示由前端按权限矩阵决定。
        sort_order: 展示顺序。
    """

    __tablename__ = "roles"

    role_code: Mapped[str] = mapped_column(str_col(32), primary_key=True)
    role_name: Mapped[str] = mapped_column(str_col(64), nullable=False)
    landing: Mapped[str] = mapped_column(str_col(128), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)

    users: Mapped[list[User]] = relationship(back_populates="role")

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"<Role {self.role_code} {self.role_name}>"


class User(TimestampMixin, Base):
    """账号主体。

    Attributes:
        user_id: 业务主键，格式 `U0001`。
        username: 登录账号（**入库前已转小写**），唯一。
        display_name: 姓名（展示用）。
        role_code: 外键 `roles.role_code`。
        group_id: 外键 `agent_groups.group_id`；`KB_OPERATOR`/`ADMIN` 可为空。
        password_hash: `scrypt$N$r$p$saltB64$hashB64`，**永不返回给前端**。
        status: `ACTIVE` / `DISABLED` / `LOCKED`。
        failed_count: 连续失败次数，达 5 次置 `LOCKED` 并写 `locked_until`。
        locked_until: 锁定到期时间；到期后登录时自动解锁。
        must_change: 是否强制下次登录改密（新建账号、管理员重置后为真）。
        last_login_at / last_login_ip: 最近一次成功登录的痕迹。
        created_by: 创建人（`system` 表示种子数据）。
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_username", "username", unique=True),
        Index("ix_users_role", "role_code"),
        Index("ix_users_group", "group_id"),
    )

    user_id: Mapped[str] = mapped_column(str_col(16), primary_key=True)
    username: Mapped[str] = mapped_column(str_col(64), nullable=False)
    display_name: Mapped[str] = mapped_column(str_col(64), nullable=False)
    role_code: Mapped[str] = mapped_column(
        str_col(32),
        ForeignKey("roles.role_code"),
        nullable=False,
    )
    group_id: Mapped[str | None] = mapped_column(
        str_col(16),
        ForeignKey("agent_groups.group_id"),
        nullable=True,
    )
    password_hash: Mapped[str] = mapped_column(str_col(255), nullable=False)
    status: Mapped[str] = mapped_column(str_col(16), default=STATUS_ACTIVE, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    must_change: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    last_login_ip: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(str_col(64), default="system", nullable=False)

    role: Mapped[Role] = relationship(back_populates="users")
    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"<User {self.user_id} {self.username} {self.role_code}>"


class AuthSession(Base):
    """登录态。

    **库里只存 `sha256(token)`**：token 本身是 32 字节随机数，无需慢哈希；
    sha256 的唯一目的是「库文件泄露后不能直接冒用会话」。选随机 token + 服务端
    表，而不是 JWT，是因为运营要「禁用账号立刻生效」——JWT 签发后无法吊销。

    Attributes:
        token_hash: `sha256(token)` 十六进制（主键）。
        user_id: 外键 `users.user_id`，级联删除。
        issued_at: 签发时间。
        expires_at: 到期时间（默认 12 小时，够一个班次 + 余量）。
        revoked_at: 吊销时间；非空即失效（登出、改密、禁用、重置口令都会写）。
        user_agent: 签发时的 UA 摘要，便于排障「这条会话是谁的」。
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user", "user_id"),)

    token_hash: Mapped[str] = mapped_column(str_col(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        str_col(16),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_at: Mapped[datetime] = mapped_column(dt_col(), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(dt_col(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    user_agent: Mapped[str] = mapped_column(str_col(255), default="", nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"<AuthSession {self.token_hash[:8]}… user={self.user_id}>"


__all__ = [
    "DEMO_PASSWORD",
    "ROLE_ADMIN",
    "ROLE_AGENT",
    "ROLE_CODES",
    "ROLE_KB_OPERATOR",
    "ROLE_SUPERVISOR",
    "SCRYPT_KEY_LEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SCRYPT_SALT_LEN",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_LOCKED",
    "USER_STATUSES",
    "AuthSession",
    "Role",
    "User",
]
