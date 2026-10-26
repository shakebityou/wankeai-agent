"""SQLAlchemy 声明式基类与通用 Mixin。

所有 ORM 模型继承 `Base`；时间戳与软删除字段由 Mixin 统一提供，避免
每张表各写一遍 `created_at` / `updated_at` 导致口径不一致。

**命名约定**（`naming_convention`）不是可选项：Alembic 的 autogenerate 与
`DROP CONSTRAINT` 都依赖约束名可预测。不设约定时 SQL Server 与 PostgreSQL
生成的默认名完全不同，迁移脚本无法跨库复用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import dt_col

#: 约束命名约定（跨方言可预测，Alembic autogenerate 依赖它）
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """当前 UTC 时间（**统一入口**，禁止业务代码直接调 `datetime.now()`）。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """创建 / 更新时间戳（UTC）。"""

    created_at: Mapped[datetime] = mapped_column(
        dt_col(),
        default=utcnow,
        nullable=False,
        doc="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        dt_col(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        doc="最后更新时间（UTC）",
    )


class SoftDeleteMixin:
    """软删除标记。

    **为什么用软删除而不是物理删除**：合规要求「可删除可追溯」（HC-2）。
    物理删除无法证明「删了什么、谁删的、什么时候删的」；软删除 + 操作日志
    既能满足用户删除诉求（对外不可见），又保留审计链路。
    真正需要物理清除时由 `purge_expired()` 按留存期执行。
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        dt_col(),
        default=None,
        nullable=True,
        doc="软删除时间（UTC），None 表示未删除",
    )
    deleted_by: Mapped[str | None] = mapped_column(
        default=None,
        nullable=True,
        doc="删除操作人（坐席工号 / system）",
    )


__all__ = ["Base", "NAMING_CONVENTION", "SoftDeleteMixin", "TimestampMixin", "utcnow"]
