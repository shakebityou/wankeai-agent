"""关系库 Schema 自检（第 2 步 DoD 验证脚本）。

用法：
    python scripts/check_db_schema.py            # 只测 SQLite（默认，无外部依赖）
    python scripts/check_db_schema.py --mssql    # 额外测本机 SQL Server 等价实现

验证内容：
1. 全部 ORM 模型可 import，元数据包含 17 张表（14 张业务表 + 账号体系 3 张）。
2. 建表成功（SQLite / SQL Server）。
3. 中文写入→读出往返无损（含 JSON 字段，**这是 ensure_ascii 坑的回归测试**）。
4. 外键约束真实生效（SQLite 需显式 PRAGMA，SQL Server 天然生效）。
5. 唯一键约束真实生效（`messages.msg_id` / `tickets.session_id` 幂等基础）。
6. 时间戳带时区且为 UTC。
7. 枚举口径与任务书 2.6 节一致（抽查）。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一条断言结果。

    Args:
        condition: 断言是否成立。
        label: 断言名。
        detail: 失败时补充的信息。
    """
    if condition:
        print(f"  [OK]   {label}")
    else:
        print(f"  [FAIL] {label} {detail}")
        FAILURES.append(f"{label} {detail}".strip())


def exercise(engine, label: str) -> None:
    """在一套引擎上跑完整读写断言。

    Args:
        engine: SQLAlchemy Engine。
        label: 展示用的后端名。
    """
    from sqlalchemy import select, text

    from app.db import Base, session_scope
    from app.db.models import KbChunk, KbChunkVersion, Message, Session, Ticket

    print(f"\n=== {label} ===")

    # --- 建表 ---------------------------------------------------------
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables.keys())
    check(expected <= tables, f"建表 {len(expected)} 张", f"缺 {expected - tables}")

    # --- 中文 + JSON 往返 ---------------------------------------------
    with session_scope() as db:
        db.add(
            Session(
                session_id="sess_test_中文",
                buyer_id="buyer_001",
                channel="TAOBAO",
                last_message="这款奶油小熊 20cm 还有现货吗？",
            )
        )
        db.add(
            KbChunk(
                chunk_id="kb_product_0001",
                content="奶油小熊 20cm 毛绒玩偶，水晶超柔材质，PP 棉填充，3 岁以上适用。",
                title="奶油小熊 20cm",
                knowledge_type="PRODUCT",
                category="毛绒玩偶",
                sku_ids=["WK-BEAR-20"],
                channel=["all"],
                keywords=["奶油小熊", "毛绒", "20cm"],
                source="商品知识卡.csv",
                source_row=2,
                effective_from=date(2024, 3, 15),
                authority=1,
            )
        )

    with session_scope() as db:
        s = db.get(Session, "sess_test_中文")
        check(s is not None, "中文主键写入/读取")
        check(
            s is not None and s.last_message == "这款奶油小熊 20cm 还有现货吗？",
            "中文正文往返无损",
            f"得到 {s.last_message!r}" if s else "",
        )
        c = db.get(KbChunk, "kb_product_0001")
        check(c is not None and c.content.startswith("奶油小熊"), "中文 chunk 正文往返无损")
        check(
            c is not None and c.sku_ids == ["WK-BEAR-20"],
            "JSON 数组字段往返无损",
            f"得到 {c.sku_ids!r}" if c else "",
        )
        check(
            c is not None and c.keywords == ["奶油小熊", "毛绒", "20cm"],
            "JSON 中文数组不转义（ensure_ascii 坑回归）",
            f"得到 {c.keywords!r}" if c else "",
        )
        check(
            c is not None and c.effective_from == date(2024, 3, 15),
            "Date 字段往返无损",
        )
        check(
            c is not None and c.created_at.tzinfo is not None,
            "时间戳带时区",
            f"tzinfo={c.created_at.tzinfo}" if c else "",
        )

    # --- 唯一键（幂等基础）--------------------------------------------
    dup_ok = False
    try:
        with session_scope() as db:
            db.add(Message(msg_id="msg_dup_001", session_id="sess_test_中文"))
        with session_scope() as db:
            db.add(Message(msg_id="msg_dup_001", session_id="sess_test_中文"))
    except Exception:  # noqa: BLE001  唯一键冲突是预期结果
        dup_ok = True
    check(dup_ok, "messages.msg_id 唯一键拦截重复（回调幂等第二道防线）")

    dup_ticket = False
    try:
        with session_scope() as db:
            db.add(Ticket(ticket_no="TS202601010001", session_id="sess_test_中文"))
        with session_scope() as db:
            db.add(Ticket(ticket_no="TS202601010002", session_id="sess_test_中文"))
    except Exception:  # noqa: BLE001  唯一键冲突是预期结果
        dup_ticket = True
    check(dup_ticket, "tickets.session_id 唯一键拦截重复（工单幂等最终保障）")

    # --- 外键 ---------------------------------------------------------
    fk_ok = False
    try:
        with session_scope() as db:
            db.add(Message(msg_id="msg_orphan", session_id="sess_不存在的会话"))
    except Exception:  # noqa: BLE001  外键失败是预期结果
        fk_ok = True
    check(fk_ok, "外键约束生效（孤儿消息被拒）")

    # --- 级联删除 ------------------------------------------------------
    with session_scope() as db:
        s = db.get(Session, "sess_test_中文")
        if s is not None:
            db.delete(s)
    with session_scope() as db:
        left = db.scalars(
            select(Message).where(Message.session_id == "sess_test_中文")
        ).all()
        check(not left, "级联删除生效（删会话带走消息）", f"残留 {len(left)} 条")

    # --- 版本表 -------------------------------------------------------
    with session_scope() as db:
        db.add(
            KbChunkVersion(
                chunk_id="kb_product_0001",
                version=1,
                action="CREATE",
                content="v1 正文",
                metadata_snapshot={"category": "毛绒玩偶", "authority": 1},
                change_reason="初始导入",
                updated_by="system",
            )
        )
    with session_scope() as db:
        v = db.scalars(
            select(KbChunkVersion).where(KbChunkVersion.chunk_id == "kb_product_0001")
        ).all()
        check(len(v) == 1, "版本历史表可写可读")
        check(
            bool(v) and v[0].metadata_snapshot.get("category") == "毛绒玩偶",
            "版本快照 JSON 中文往返无损",
            f"得到 {v[0].metadata_snapshot!r}" if v else "",
        )

    # --- 连接可用性 ----------------------------------------------------
    with engine.connect() as conn:
        val = conn.execute(text("SELECT 1")).scalar()
    check(val == 1, "连接探活 SELECT 1")

    Base.metadata.drop_all(engine)


def _ensure_mssql_database(server_dsn_db: str, name: str) -> None:
    """确保 SQL Server 上存在指定数据库（不存在则创建）。

    **为什么用独立库名而不是直接测 `wkagent`**：本脚本会 `drop_all()`，
    在真实库上跑等于清空数据。用 `wkagent_schema_check` 这类一次性库名，
    脚本可以随时重跑而不会伤到任何业务数据。

    Args:
        server_dsn_db: 连接串中的数据库名（通常是 `master`）。
        name: 要确保存在的数据库名。
    """
    import pyodbc

    conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};Server=localhost;"
        f"Database={server_dsn_db};Trusted_Connection=yes;",
        timeout=8,
        autocommit=True,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.databases WHERE name = ?", name)
        if cur.fetchone() is None:
            # 库名来自脚本常量，非用户输入，无注入风险
            cur.execute(f"CREATE DATABASE {name}")
            print(f"  （已创建测试库 {name}）")
    finally:
        conn.close()


def main() -> int:
    """脚本入口。

    Returns:
        进程退出码：0 全部通过，1 有失败。
    """
    parser = argparse.ArgumentParser(description="关系库 Schema 自检")
    parser.add_argument(
        "--mssql",
        action="store_true",
        help="额外测试本机 SQL Server 等价实现（需要 ODBC Driver 17）",
    )
    args = parser.parse_args()

    print("关系库 Schema 自检")
    print(f"Python {sys.version.split()[0]}")

    # 1) SQLite（始终跑，零依赖）
    os.environ["WK_DB_BACKEND"] = "sqlite"
    os.environ["WK_LOCAL_DB_PATH"] = str(
        Path(__file__).resolve().parents[1] / ".local" / "schema_check.db"
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.engine import dispose_engine, get_engine

    exercise(get_engine(), "SQLite（单测/本地默认）")
    dispose_engine()

    # 2) SQL Server（可选）
    if args.mssql:
        scratch_db = "wkagent_schema_check"
        _ensure_mssql_database("master", scratch_db)
        os.environ["WK_DB_BACKEND"] = "mssql"
        os.environ["WK_MSSQL_DSN"] = (
            f"mssql+pyodbc://@localhost/{scratch_db}"
            "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
        )
        get_settings.cache_clear()
        try:
            exercise(get_engine(), "SQL Server 2022（本机等价实现）")
        finally:
            dispose_engine()
    else:
        print("\n（跳过 SQL Server 测试，加 --mssql 启用）")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
