"""渠道表补齐 FE-11 需要的运营字段

`FE-11 渠道与授权管理` 页面要展示权限包、出口 IP 白名单、抖音小店服务商认证
状态与最近操作人；这些是运营数据，必须入库才能「改完刷新还在」。

Revision ID: c7d2e5a91b04
Revises: b1a7c4e29f30
Create Date: 2026-10-21 21:05:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d2e5a91b04"
down_revision = "b1a7c4e29f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """给 `channels` 加 5 个列。"""
    op.add_column(
        "channels",
        sa.Column("permission_packages", sa.Text(), nullable=True, comment="权限包（JSON 数组）"),
    )
    op.add_column(
        "channels",
        sa.Column(
            "ip_whitelist_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="出口 IP 白名单条数",
        ),
    )
    op.add_column(
        "channels",
        sa.Column("service_provider_certified", sa.Boolean(), nullable=True, comment="服务商认证状态"),
    )
    op.add_column(
        "channels",
        sa.Column(
            "service_provider_note",
            sa.String(length=255),
            nullable=False,
            server_default="",
            comment="服务商认证备注",
        ),
    )
    op.add_column(
        "channels",
        sa.Column(
            "updated_by",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="最近操作人",
        ),
    )


def downgrade() -> None:
    """逆序删列。"""
    op.drop_column("channels", "updated_by")
    op.drop_column("channels", "service_provider_note")
    op.drop_column("channels", "service_provider_certified")
    op.drop_column("channels", "ip_whitelist_count")
    op.drop_column("channels", "permission_packages")
