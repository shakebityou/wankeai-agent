"""Alembic 迁移往返自检（SQLite + SQL Server）。

为什么要专门写这个脚本
----------------------
单看「`upgrade head` 能跑」是不够的。真实的迁移事故几乎都发生在两个
时刻，而这两个时刻 alembic 命令本身不会替你验证：

1. **在已有数据的表上加非空列**：库里已经有 336 条 chunk 时
   `ALTER TABLE ADD COLUMN deleted_by NVARCHAR(64) NOT NULL` 会失败——
   存量行填什么？只要没写 `server_default`，PG / SQL Server 直接报错中断。
   空库上试永远发现不了。
2. **`downgrade` 其实跑不通**：autogenerate 生成的 downgrade 经常漏掉
   索引 / 约束 / 默认值的还原，回滚演练时才炸。DoD 要求「版本回滚演练
   通过」，所以必须真跑一遍 `downgrade base` 再 `upgrade head`。

因此本脚本刻意做：**先升到上一版 → 灌真实行数的存量数据 → 再升级**
这条路径，而不是在空库上一路 `upgrade head`。

用法::

    python scripts/check_migration_roundtrip.py            # 仅 SQLite
    python scripts/check_migration_roundtrip.py --mssql    # 追加 SQL Server
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    """记录一条断言。"""
    if condition:
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
        return True
    msg = f"{label}" + (f"  ({detail})" if detail else "")
    print(f"  [FAIL] {msg}")
    FAILURES.append(msg)
    return False


def _alembic(args: list[str], env_overrides: dict[str, str]) -> tuple[bool, str]:
    """在子进程里跑 alembic（隔离环境变量，避免污染当前进程的 Settings 缓存）。"""
    env = {**os.environ, **env_overrides, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def _seed_legacy_rows(env_overrides: dict[str, str], count: int) -> int:
    """在「上一版」结构上灌存量知识块（模拟真实环境的 336 条）。"""
    from app.db.engine import get_engine
    from sqlalchemy import text

    from datetime import datetime, timezone

    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for i in range(count):
            conn.execute(
                text(
                    "INSERT INTO kb_chunks "
                    "(chunk_id, content, title, knowledge_type, category, "
                    " sub_category, sku_ids, channel, authority, brand, keywords,"
                    " source, review_status, version, enabled, updated_by, extra,"
                    " created_at, updated_at) "
                    "VALUES (:cid, :content, :title, :kt, :cat, "
                    " '', '[]', '[\"all\"]', 2, '玩可爱', '[]', 'legacy.csv', "
                    " 'APPROVED', 1, 1, 'seed', '{}', :now, :now)"
                ),
                {
                    "cid": f"legacy:{i}",
                    "content": f"存量知识块 {i}：七天无理由退货，商品完好不影响二次销售。",
                    "title": f"存量条款 {i}",
                    "kt": "POLICY",
                    "cat": "售后政策",
                    "now": now,
                },
            )
    return count


def run_roundtrip(label: str, env_overrides: dict[str, str]) -> None:
    """跑一遍完整往返：upgrade → 存量数据 → 升级 → 校验 → 回滚 → 再升级。"""
    print(f"\n=== 迁移往返：{label} ===")

    for key, value in env_overrides.items():
        os.environ[key] = value

    # 让 app 的 Settings / engine 单例按新环境重建
    from app.core.config import get_settings
    from app.db.engine import dispose_engine

    get_settings.cache_clear()
    dispose_engine()

    ok, out = _alembic(["upgrade", "0d1d5bdc3e66"], env_overrides)
    check(ok, "升级到上一版（0d1d5bdc3e66）", out.strip().splitlines()[-1] if out else "")
    if not ok:
        print(out)
        return

    # 这一步是本脚本的核心：**有数据时**再加非空列
    seeded = _seed_legacy_rows(env_overrides, 336)
    check(seeded == 336, "灌入存量知识块（模拟真实 336 条）", f"rows={seeded}")

    ok, out = _alembic(["upgrade", "head"], env_overrides)
    check(ok, "有存量数据时升级到 head（新增非空列不中断）")
    if not ok:
        print(out)
        return

    # 存量行的新列应该拿到空串而不是 NULL
    from sqlalchemy import text

    from app.db.engine import get_engine

    with get_engine().connect() as conn:
        nulls = conn.execute(
            text("SELECT COUNT(*) FROM kb_chunks WHERE deleted_by IS NULL")
        ).scalar_one()
        total = conn.execute(text("SELECT COUNT(*) FROM kb_chunks")).scalar_one()
        deleted_at_nulls = conn.execute(
            text("SELECT COUNT(*) FROM kb_chunks WHERE deleted_at IS NULL")
        ).scalar_one()
    check(total == 336, "存量行未丢", f"rows={total}")
    check(nulls == 0, "存量行 deleted_by 非 NULL（server_default 生效）", f"nulls={nulls}")
    check(
        deleted_at_nulls == 336,
        "存量行 deleted_at 为 NULL（未删过就该是空）",
        f"nulls={deleted_at_nulls}",
    )

    ok, out = _alembic(["downgrade", "base"], env_overrides)
    check(ok, "回滚到 base（downgrade 可执行）")
    if not ok:
        print(out)

    ok, out = _alembic(["upgrade", "head"], env_overrides)
    check(ok, "再次升级到 head（回滚后可重放）")
    if not ok:
        print(out)

    # 结构核对
    from sqlalchemy import inspect

    from app.db.engine import get_engine

    insp = inspect(get_engine())
    tables = set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("kb_chunks")}
    check("deleted_at" in cols and "deleted_by" in cols, "head 结构含软删两列", sorted(cols & {"deleted_at", "deleted_by"}))
    expected = {
        "agent_groups",
        "audit_logs",
        "auth_sessions",
        "channels",
        "handoff_rules",
        "kb_chunk_versions",
        "kb_chunks",
        "kb_feedback",
        "kb_import_jobs",
        "messages",
        "roles",
        "session_states",
        "sessions",
        "ticket_events",
        "tickets",
        "turns",
        "users",
    }
    missing = expected - tables
    check(not missing, "head 建出全部 17 张业务表", f"missing={sorted(missing)}")


def _ensure_mssql_database(server_dsn: str, db_name: str) -> None:
    """确保 SQL Server 上的自检库存在。"""
    import pyodbc

    conn = pyodbc.connect(server_dsn, autocommit=True)
    try:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT COUNT(*) FROM sys.databases WHERE name = ?", db_name
        ).fetchone()[0]
        if not exists:
            cur.execute(f"CREATE DATABASE [{db_name}]")
    finally:
        conn.close()


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="Alembic 迁移往返自检")
    parser.add_argument("--mssql", action="store_true", help="同时验证 SQL Server")
    args = parser.parse_args()

    sqlite_path = BACKEND_DIR / ".local" / "migration_check.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_path.exists():
        sqlite_path.unlink()

    run_roundtrip(
        "SQLite",
        {
            "WK_DB_BACKEND": "sqlite",
            # alembic 的 env.py 读的是这个变量推导出的连接串
            "WK_LOCAL_DB_PATH": str(sqlite_path),
            "WK_DB_URL": "",
        },
    )

    if args.mssql:
        db_name = "wkagent_migration_check"
        server_dsn = (
            "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;"
            "DATABASE=master;Trusted_Connection=yes;"
        )
        _ensure_mssql_database(server_dsn, db_name)
        run_roundtrip(
            f"SQL Server（{db_name}）",
            {
                "WK_DB_BACKEND": "mssql",
                "WK_MSSQL_DSN": (
                    f"mssql+pyodbc://@{db_name}"
                    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
                )
                .replace("@", "@localhost/", 1),
                "WK_DB_URL": "",
            },
        )

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
