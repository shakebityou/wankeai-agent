"""SQLAlchemy 自定义类型与列工厂。

**为什么需要这一层**：任务书 3.1 锁定 PostgreSQL 14（选它的理由是 JSONB +
全文检索），但本机等价环境用 SQL Server 2022（见 `docs/偏差记录.md`）。
两套库的类型系统不同，业务代码不应该到处写 `if dialect == ...`，
因此把方言差异收敛到本模块的 `TypeDecorator` 与列工厂里。

| 语义 | PostgreSQL 14 | SQL Server 2022 | SQLite（单测） |
| --- | --- | --- | --- |
| JSON | `JSONB`（可索引） | `NVARCHAR(MAX)` + `ISJSON` 约束 | `TEXT` |
| 长文本 | `TEXT` | `NVARCHAR(MAX)` | `TEXT` |
| 短文本 | `VARCHAR(n)` | `NVARCHAR(n)` | `VARCHAR(n)` |
| 时间戳 | `TIMESTAMP WITH TIME ZONE` | `DATETIMEOFFSET` | `DATETIME` |
| 自增主键 | `BIGSERIAL` | `BIGINT IDENTITY` | `INTEGER AUTOINCREMENT` |

**中文字段一律用 `Unicode` / `UnicodeText`**：SQL Server 上 `String` 会映射成
`VARCHAR`，依赖排序规则隐式转换，跨库拷贝时容易乱码；显式 `NVARCHAR` 更安全。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Integer, JSON, Text, Unicode, UnicodeText
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class JSONType(TypeDecorator):
    """跨方言 JSON 类型。

    * PostgreSQL → `JSONB`（支持 GIN 索引与 `->>` 运算符，任务书选 PG 的理由）
    * SQL Server → `NVARCHAR(MAX)`（配合 `ISJSON()` CHECK 约束保证合法）
    * SQLite → `TEXT`

    序列化统一用 `ensure_ascii=False`。**这是踩过的坑**：本机 Redis vectorset
    探测时用默认 `ensure_ascii=True` 把中文写成 `\\u6bdb\\u7ed2` 转义字面量，
    导致中文元数据过滤静默返回空（详见 `docs/偏差记录.md`）。数据库侧同理——
    转义后的中文虽然能存能读，但 `LIKE '%毛绒%'` 这类人工排查会全部失配。

    Attributes:
        cache_ok: SQLAlchemy 2.x 要求显式声明，本类型无实例状态。
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        """按方言选择底层类型。"""
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """写入前确保是可序列化的纯 Python 对象。

        **这里只做「归一化」，不做序列化**——真正的 `json.dumps` 由底层
        `JSON` / `JSONB` 的 bind_processor 完成。

        曾经以为在这里 `json.dumps(..., ensure_ascii=False)` 就能保证中文不
        转义，实测**没用**：底层 `JSON` 类型会拿我们返回的 Python 对象再跑一次
        自己的序列化器（默认 `ensure_ascii=True`），中文照样变成 `\\u5976...`。
        绕开它的办法见 `_bind_json_serializer`：在引擎 connect 时把
        `json_serializer` 换成我们的版本——那是序列化器**唯一**的注入点。

        Args:
            value: 待写入的值（dict / list / 标量）。
            dialect: 当前方言。

        Returns:
            归一化后的纯 Python 对象（不是字符串）。
        """
        if value is None:
            return None
        # 把 datetime/Decimal 等先转成 str，避免底层序列化器 default= 报错
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        """读出后如果是字符串（SQL Server / SQLite 可能返回 str），补一次解析。"""
        if value is None or isinstance(value, (dict, list)):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        return value


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


class UTCDateTime(TypeDecorator):
    """带时区的 UTC 时间戳。

    * PostgreSQL → `TIMESTAMP WITH TIME ZONE`
    * SQL Server → `DATETIMEOFFSET`
    * SQLite → `DATETIME`（读出时补 UTC 时区）

    **写入时强制转 UTC**：避免同一张表里混入本地时间（服务器 `TZ=Asia/Shanghai`，
    容器与宿主机时区不一致时最容易出问题）。
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        """按方言选择底层类型。"""
        from sqlalchemy import DateTime

        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        """统一转成 UTC。"""
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        """SQLite 丢失时区信息，读出时补回 UTC。"""
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


# ---------------------------------------------------------------------------
# 列工厂（业务模型统一用这些，不直接写 String/Text）
# ---------------------------------------------------------------------------


def str_col(length: int) -> Unicode:
    """短文本列（`NVARCHAR(length)` / `VARCHAR(length)`）。

    Args:
        length: 最大字符数。

    Returns:
        列类型实例（未绑定列名，用于 `mapped_column`）。
    """
    return Unicode(length)


def text_col() -> UnicodeText:
    """长文本列（`NVARCHAR(MAX)` / `TEXT`）。"""
    return UnicodeText()


def json_col() -> JSONType:
    """JSON 列。"""
    return JSONType()


def dt_col() -> UTCDateTime:
    """UTC 时间戳列。"""
    return UTCDateTime()


def pk_col() -> BigInteger:
    """自增主键列。

    注意：SQLite 只有 `INTEGER PRIMARY KEY` 才是 rowid 别名（真正的自增），
    `BIGINT PRIMARY KEY` 不会自增。SQLAlchemy 的 `BigInteger().with_variant(
    Integer, "sqlite")` 正是为此。
    """
    return BigInteger().with_variant(Integer, "sqlite")


__all__ = [
    "JSONType",
    "UTCDateTime",
    "dt_col",
    "json_col",
    "pk_col",
    "str_col",
    "text_col",
]
