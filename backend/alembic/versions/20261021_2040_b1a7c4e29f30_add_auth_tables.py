"""新增账号体系三张表：`roles` / `users` / `auth_sessions`。

前端坐席台已按 `frontend/mock/db/schema.sql` 实现了登录、用户管理、操作日志
（`FE-03`/`FE-11` 之外额外做的运营能力）。BFF 要成为**前端唯一入口**，就必须
自己提供这套账号能力，否则前端一切到真 BFF，登录页与用户管理页立刻 404。

**三张表而不是一张**：
* `roles` 是字典表 —— 加角色时不改 `users` 结构，也不用发带数据迁移的版本；
* `users` 存账号与口令哈希，`group_id` 外键引用**已有的** `agent_groups`
  （G1–G6 由 BE-14 维护，账号体系只引用不重建，避免「两处都能改分组」的双写）；
* `auth_sessions` 存登录态，**库里只存 token 的 sha256**。

**口令哈希格式与前端完全一致**：`scrypt$16384$8$1$saltB64$hashB64`。
前端 `mock/db/password.ts` 生成的种子哈希可以原样被后端校验，反之亦然——
联调时不用重新造账号。

**`username` 统一小写入库**（前端要求大小写不敏感唯一）：`COLLATE NOCASE` /
`citext` / 排序规则三套写法跨库不通用，转小写是唯一一致方案。迁移里对存量
数据无需清洗（本表是新建的）。

Revision ID: b1a7c4e29f30
Revises: 6889b156b3a4
Create Date: 2026-10-21 20:40
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  （autogenerate 引用的自定义类型）

# revision identifiers, used by Alembic.
revision: str = "b1a7c4e29f30"
down_revision: str | None = "6889b156b3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    op.create_table(
        "roles",
        sa.Column("role_code", sa.Unicode(length=32), nullable=False),
        sa.Column("role_name", sa.Unicode(length=64), nullable=False),
        sa.Column("landing", sa.Unicode(length=128), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("role_code", name=op.f("pk_roles")),
        comment="角色字典（第 31 条 BFF 账号体系）",
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.Unicode(length=16), nullable=False),
        sa.Column("username", sa.Unicode(length=64), nullable=False),
        sa.Column("display_name", sa.Unicode(length=64), nullable=False),
        sa.Column("role_code", sa.Unicode(length=32), nullable=False),
        sa.Column("group_id", sa.Unicode(length=16), nullable=True),
        sa.Column("password_hash", sa.Unicode(length=255), nullable=False),
        sa.Column("status", sa.Unicode(length=16), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", app.db.types.UTCDateTime(), nullable=True),
        sa.Column("must_change", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", app.db.types.UTCDateTime(), nullable=True),
        sa.Column("last_login_ip", sa.Unicode(length=64), nullable=False),
        sa.Column("created_by", sa.Unicode(length=64), nullable=False),
        sa.Column("created_at", app.db.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["agent_groups.group_id"],
            name=op.f("fk_users_group_id_agent_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["role_code"],
            ["roles.role_code"],
            name=op.f("fk_users_role_code_roles"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_users")),
        comment="账号主体（口令只存 scrypt 哈希）",
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_role", "users", ["role_code"], unique=False)
    op.create_index("ix_users_group", "users", ["group_id"], unique=False)

    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.Unicode(length=64), nullable=False),
        sa.Column("user_id", sa.Unicode(length=16), nullable=False),
        sa.Column("issued_at", app.db.types.UTCDateTime(), nullable=False),
        sa.Column("expires_at", app.db.types.UTCDateTime(), nullable=False),
        sa.Column("revoked_at", app.db.types.UTCDateTime(), nullable=True),
        sa.Column("user_agent", sa.Unicode(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_auth_sessions")),
        comment="登录态（只存 token 的 sha256）",
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"], unique=False)


def downgrade() -> None:
    """回滚（必须实现，否则无法演练回滚）。"""
    op.drop_index("ix_auth_sessions_user", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_group", table_name="users")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
