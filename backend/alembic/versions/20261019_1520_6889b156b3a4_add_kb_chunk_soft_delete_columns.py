"""新增 `kb_chunks` 软删留痕列（`deleted_at` / `deleted_by`）。

`BE-17` / HC-2 要求「可删除可追溯」：软删之后必须能回答「谁在什么时候
删的」。原先只翻 `enabled=False`，审计上等于「不知道谁删的」。

⚠️ `deleted_by` 建列时**必须带 `server_default=''`**：表里已有存量 chunk
（实测 336 条），`nullable=False` 且无默认值在 PG / SQL Server 上会直接
报「不能把 NULL 插入非空列」而中断迁移。加完后去掉 server_default，
让 ORM 侧的 `default=""` 成为唯一来源，避免库里留下两套默认值。

Revision ID: 6889b156b3a4
Revises: 0d1d5bdc3e66
Create Date: 2026-10-19 15:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  （autogenerate 引用的自定义类型）

# revision identifiers, used by Alembic.
revision: str = "6889b156b3a4"
down_revision: str | None = "0d1d5bdc3e66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级。"""
    with op.batch_alter_table("kb_chunks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deleted_at", app.db.types.UTCDateTime(), nullable=True)
        )
        # 存量行先落空串，加完立刻去掉 server_default（见模块 docstring）
        batch_op.add_column(
            sa.Column(
                "deleted_by",
                sa.Unicode(length=64),
                nullable=False,
                server_default="",
            )
        )

    with op.batch_alter_table("kb_chunks", schema=None) as batch_op:
        batch_op.alter_column("deleted_by", server_default=None)


def downgrade() -> None:
    """回滚（必须实现，否则无法演练回滚）。"""
    with op.batch_alter_table("kb_chunks", schema=None) as batch_op:
        batch_op.drop_column("deleted_by")
        batch_op.drop_column("deleted_at")
