"""kb_feedback 补坐席视角字段（FE-08 纠错闭环）

给 `kb_feedback` 加 6 列，对应前端契约 `FeedbackItem` 里后端原缺的字段：
`message_id`（幂等键）、`error_type`（坐席错误类型下拉框）、`chunk_ids`
（一次回答可能拼了多条知识）、`assignee`、`resolved_chunk_id`、`resolution`。

**为什么不复用已有列**：`reason` 是运营口径（WRONG_FACT/OUTDATED/...），
`error_type` 是坐席下拉框口径，两者取值集不同，合并会让运营侧报表失真；
`handled_by` 是「谁处理的」，`assignee` 是「分配给谁」，处理前就要有值。

Revision ID: d4f8b2c6e103
Revises: c7d2e5a91b04
Create Date: 2026-10-21 21:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8b2c6e103"
down_revision: Union[str, None] = "c7d2e5a91b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "kb_feedback",
        sa.Column("message_id", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "kb_feedback",
        sa.Column("error_type", sa.String(32), nullable=False, server_default="OTHER"),
    )
    # JSON 列在 SQLite 用 TEXT、MSSQL 用 NVARCHAR(MAX)，统一按 Text 加，
    # 由 ORM 侧 json_col() 负责序列化/反序列化。
    op.add_column("kb_feedback", sa.Column("chunk_ids", sa.Text(), nullable=True))
    op.add_column(
        "kb_feedback",
        sa.Column("assignee", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "kb_feedback", sa.Column("resolved_chunk_id", sa.String(64), nullable=True)
    )
    op.add_column("kb_feedback", sa.Column("resolution", sa.Text(), nullable=True))
    op.add_column("kb_feedback", sa.Column("comment", sa.Text(), nullable=True))
    op.create_index(
        "ix_kb_feedback_message", "kb_feedback", ["message_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_kb_feedback_message", table_name="kb_feedback")
    op.drop_column("kb_feedback", "comment")
    op.drop_column("kb_feedback", "resolution")
    op.drop_column("kb_feedback", "resolved_chunk_id")
    op.drop_column("kb_feedback", "assignee")
    op.drop_column("kb_feedback", "chunk_ids")
    op.drop_column("kb_feedback", "error_type")
    op.drop_column("kb_feedback", "message_id")
