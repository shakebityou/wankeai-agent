"""Alembic 迁移环境。

**关键设计：连接串从应用配置读，不写死在 `alembic.ini`。**

原因有两个：
1. 密码不能进版本库（商家强制要求，S-20 / 2.3.2 节）。
2. 同一个迁移脚本要在三种后端上跑（PostgreSQL 生产 / SQL Server 本机等价 /
   SQLite 单测），写死连接串就无法切换。

**跨方言迁移的取舍**：`render_as_batch=True` 仅对 SQLite 生效，因为 SQLite
不支持大部分 `ALTER TABLE`（改列类型、加约束都要「建新表→拷数据→换名」）。
SQL Server 与 PostgreSQL 走原生 ALTER。

**autogenerate 的已知盲区**（生成后必须人工复核）：
* 表/列注释变更不会生成（需手写 `op.create_table(comment=...)`）
* 索引排序、`INCLUDE` 列不会生成
* SQL Server 的 `NVARCHAR(MAX)` ↔ `NVARCHAR(n)` 变更常被判为 no-op
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import app.*（backend/ 加入 sys.path）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402

# 必须 import 模型包，否则 Base.metadata 里没有表定义（autogenerate 会生成空迁移）
import app.db.models  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: autogenerate 的比对目标
target_metadata = Base.metadata


def get_url() -> str:
    """解析数据库连接串。

    Returns:
        SQLAlchemy 连接串（由 `Settings.resolved_db_url` 决定）。
    """
    return get_settings().resolved_db_url


def is_sqlite() -> bool:
    """当前是否 SQLite 后端（决定是否用 batch 模式）。"""
    return get_url().startswith("sqlite")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库（用于产出 DBA 审核用的 DDL）。"""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=is_sqlite(),
        # SQL Server 不支持事务性 DDL 的部分语句，交由驱动处理
        transactional_ddl=not is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 类型变更与默认值变更也要被检测到（默认关闭，会漏掉 ALTER COLUMN）
            compare_type=True,
            compare_server_default=True,
            # SQLite 必须开，否则改列/加约束的迁移会直接失败
            render_as_batch=is_sqlite(),
            include_schemas=False,
            # 迁移时保留 DDL 事务（SQL Server 的部分语句会自己提交，无副作用）
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
