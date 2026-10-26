"""Alembic 迁移脚本模板。

新增迁移时用 `alembic revision --autogenerate -m "描述"` 生成，本模板保证：

* `downgrade()` 不留空实现（DoD 要求「版本回滚演练通过」）
* 预先 import 了 `app.db.types` —— autogenerate 会引用 `JSONType` / `UTCDateTime`
  这类自定义类型，但**不会生成对应 import**，漏掉时迁移文件直接 `NameError`。
  这个坑每生成一个新迁移都会踩一次，所以在模板里就带上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  （autogenerate 引用的自定义类型）

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """升级。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚（必须实现，否则无法演练回滚）。"""
    ${downgrades if downgrades else "pass"}
