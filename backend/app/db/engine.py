"""数据库引擎与会话工厂。

**设计要点**

1. **引擎单例 + 惰性创建**：模块 import 时连库会让「只跑单测」也依赖数据库。
   `get_engine()` 第一次调用才建连接池。
2. **SQLite 单独处理**：`check_same_thread=False`（FastAPI 线程池跨线程用连接）、
   开启 WAL（读写不互斥）、开启外键约束（SQLite 默认关闭，会让 `ON DELETE`
   静默失效——这类 bug 在切到 PG 后才暴露）。
3. **`pool_pre_ping` + `pool_recycle`**：商家测试环境在 VPN 内网，链路空闲会被
   中间设备掐断；不探活就会拿到死连接，报「Connection is closed」。
4. **`session_scope()` 是唯一推荐入口**：自动 commit / rollback / close。
   业务代码不要手工 `SessionLocal()`，否则异常路径必然漏 close。

**SQL Server 特有注意**：`mssql+pyodbc` 的 `fast_executemany` 在批量导入
（BE-06 一次 1000 条）时能把耗时从分钟级压到秒级；但 `NVARCHAR(MAX)` 列
在旧驱动下有兼容问题，故只在非 MAX 列批量写时启用。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.base import Base

logger = logging.getLogger(__name__)

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def _json_serializer(value: Any) -> str:
    """引擎级 JSON 序列化器：**不转义中文**。

    SQLAlchemy 的 `JSON` 类型默认用 `json.dumps(value)`（`ensure_ascii=True`），
    会把「毛绒」写成 `\\u6bdb\\u7ed2` 字面量。存读都没问题，但
    `LIKE '%毛绒%'` 这类人工排查、以及写原生 SQL 查 JSON 字段时全部失配。
    PG 的 `JSONB` 不受影响（它存的是二进制结构），但为保持两套库的
    存储语义一致，这里统一注入。

    Args:
        value: 待序列化的 Python 对象。

    Returns:
        保留中文的 JSON 字符串。
    """
    import json as _json  # noqa: PLC0415

    return _json.dumps(value, ensure_ascii=False, default=str)


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """按后端类型组装 `create_engine` 参数。

    Args:
        settings: 全局配置。

    Returns:
        传给 `create_engine` 的关键字参数（不含 url）。
    """
    url = settings.resolved_db_url
    kwargs: dict[str, Any] = {
        "echo": settings.postgres_echo,
        "future": True,
        # 中文 JSON 不转义（见 _json_serializer）
        "json_serializer": _json_serializer,
    }
    if url.startswith("sqlite"):
        # SQLite 用默认的 SingletonThreadPool/NullPool 语义即可；
        # 跨线程访问由 check_same_thread=False 放开。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        return kwargs

    kwargs.update(
        {
            "pool_size": settings.postgres_pool_size,
            "max_overflow": settings.postgres_max_overflow,
            "pool_pre_ping": settings.db_pool_pre_ping,
            "pool_recycle": settings.db_pool_recycle_seconds,
        }
    )
    return kwargs


def _configure_sqlite(dbapi_conn: Any, _record: Any) -> None:
    """SQLite 连接建立后立即执行的 PRAGMA。

    * `foreign_keys=ON` —— SQLite 默认**不**强制外键，静默忽略 `ON DELETE`。
    * `journal_mode=WAL` —— 允许读写并发（默认 rollback journal 会整库锁）。
    * `synchronous=NORMAL` —— WAL 下的安全/性能折中。

    Args:
        dbapi_conn: DBAPI 连接对象。
        _record: SQLAlchemy 传入的连接记录（未使用）。
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _patch_json_bind_processors() -> None:
    """给 `JSON`/`JSONB` 类型打补丁：序列化时不转义中文。

    **为什么需要这个补丁（踩过的坑，值得记下来）**

    `JSONType.process_bind_param` 里曾经用
    `json.dumps(value, ensure_ascii=False)` 做「归一化」，实测**完全无效**：
    底层 `sqlalchemy.JSON` 的 bind_processor 拿到我们返回的 **Python 对象**
    （dict/list）之后，会用它自己的序列化器再序列化一次，默认参数就是
    `ensure_ascii=True`。于是「奶油小熊」照样存成
    `["\u5976\u6cb9\u5c0f\u718a"]` 字面量——读回来值是对的，但用
    `LIKE '%毛绒%'` 或 `JSON_VALUE` 排查时全部失配。

    引擎级 `create_engine(json_serializer=...)` 只在**类型实例化时**被读取，
    且 `sqlalchemy.JSON` 的默认 `bind_processor` 走的是模块级默认序列化器。
    最可靠的注入点是**类型类自己的 `bind_processor`**——所以这里在
    `get_engine()` 首次创建引擎前调用一次（进程内幂等）。

    `JSONB`（PostgreSQL）存的是二进制结构、由服务端解析，不受
    `ensure_ascii` 影响；这个补丁对它是无害的 no-op。

    Returns:
        None。
    """
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB

    def _dumps(value: Any) -> str:
        """`ensure_ascii=False` 的序列化，保留中文可读。"""
        return json.dumps(value, ensure_ascii=False, default=str)

    for type_cls in (JSON, JSONB):
        original = type_cls.bind_processor

        def patched(self: Any, dialect: Any, _orig: Any = original) -> Any:
            """包一层原始 processor，但序列化用保留中文的版本。

            Args:
                self: 类型实例。
                dialect: 方言。
                _orig: 原始 `bind_processor`（闭包捕获，避免递归）。

            Returns:
                绑参处理器，或 None（该方言不需要绑参处理）。
            """
            if _orig is patched:  # 防御：重复 patch 时直接短路
                return None
            processor = _orig(self, dialect)
            if processor is None:
                return None

            def _bind(value: Any) -> Any:
                """字符串原样透传，其余走保中文序列化。

                Args:
                    value: 待绑定的值。

                Returns:
                    可交给 DBAPI 的值。
                """
                if value is None or isinstance(value, (str, bytes)):
                    return value
                return _dumps(value)

            return _bind

        type_cls.bind_processor = patched  # type: ignore[method-assign]


def get_engine(settings: Settings | None = None) -> Engine:
    """获取全局引擎单例（惰性创建）。

    Args:
        settings: 可选配置覆盖；默认取 `get_settings()`。

    Returns:
        SQLAlchemy `Engine`。
    """
    global _ENGINE
    if _ENGINE is None:
        settings = settings or get_settings()
        url = settings.resolved_db_url
        # 中文 JSON 不转义（见 _patch_json_bind_processors 的踩坑说明）
        _patch_json_bind_processors()
        engine = create_engine(url, **_engine_kwargs(settings))
        if url.startswith("sqlite"):
            event.listen(engine, "connect", _configure_sqlite)
        _ENGINE = engine
        logger.info(
            "关系库引擎已创建 backend=%s url=%s",
            _describe_url(url),
            _safe_url(url),
        )
    return _ENGINE


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """按指定 URL 新建一个**不进入全局单例**的引擎（测试与工具脚本用）。

    **为什么要有这个函数**：`create_engine(url)` 裸调会漏掉两处引擎级配置——
    `json_serializer=_json_serializer`（否则中文 JSON 存成 `\\uXXXX`）与
    SQLite 的 `PRAGMA foreign_keys=ON`（否则外键被静默忽略）。漏掉后
    单测会出现「同一个模型在业务代码里行为正确、在测试引擎上行为错误」
    的诡异现象，而根因只是测试自己造了引擎。这里的实现与 `get_engine()`
    共用 `_engine_kwargs` / `_configure_sqlite` / `_patch_json_bind_processors`，
    保证测试引擎与生产引擎**行为完全一致**。

    Args:
        url: 数据库 URL（`sqlite+pysqlite:///...` 等）。
        echo: 是否打印 SQL。

    Returns:
        新建的 `Engine`（调用方负责 `dispose()`）。
    """
    settings = get_settings()
    _patch_json_bind_processors()
    kwargs = _engine_kwargs(settings)
    kwargs["echo"] = echo
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """获取会话工厂单例。

    Args:
        settings: 可选配置覆盖。

    Returns:
        `sessionmaker`，`expire_on_commit=False` 便于 commit 后继续读属性。
    """
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(
            bind=get_engine(settings),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _SESSION_FACTORY


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域（**业务代码的唯一推荐入口**）。

    正常退出 commit，异常时 rollback 并向上抛出，最后一定 close。

    Yields:
        处于事务中的 `Session`。

    Examples:
        >>> with session_scope() as db:  # doctest: +SKIP
        ...     db.add(obj)
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """按 ORM 元数据建表（仅用于 SQLite 本地开发 / 单测）。

    **生产不用它**：生产走 Alembic 迁移（`alembic upgrade head`），
    这样才有版本可追溯、可回滚。`create_all` 不会修改已存在的表结构，
    在真库上用它会造成「模型与库结构漂移」。

    Args:
        engine: 可选引擎覆盖。
    """
    # 必须先 import 模型模块，否则 Base.metadata 里没有表定义
    import app.db.models  # noqa: F401  （副作用：注册全部模型）

    target = engine or get_engine()
    Base.metadata.create_all(target)


def dispose_engine() -> None:
    """释放引擎与连接池（测试收尾 / 进程退出时调用）。"""
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None


def reset_engine() -> None:
    """`dispose_engine()` 的别名，语义更贴近测试用法。"""
    dispose_engine()


def ping(engine: Engine | None = None) -> bool:
    """探活：执行一次最轻量的查询。

    Args:
        engine: 可选引擎覆盖。

    Returns:
        True 表示连接可用。
    """
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001  （探活必须吞掉所有异常并返回 False）
        logger.warning("关系库探活失败: %s", exc)
        return False


def _describe_url(url: str) -> str:
    """从连接串提取后端名（用于日志）。"""
    return url.split(":", 1)[0].split("+", 1)[0]


def _safe_url(url: str) -> str:
    """脱敏连接串中的密码（日志里绝不能出现明文口令）。"""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, tail = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}//{creds}@{tail}"


__all__ = [
    "create_all",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "make_engine",
    "ping",
    "reset_engine",
    "session_scope",
]
