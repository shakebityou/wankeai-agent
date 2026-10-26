"""Local stack probe (ASCII output to avoid console codepage issues).

Checks what is ACTUALLY available on this machine before deciding the
deployment route: SQL Server / Redis(+vector set) / Docker / PG / Milvus / ES.
"""
from __future__ import annotations

import socket
import sys

SEP = "=" * 72


def tcp_open(host: str, port: int, timeout: float = 2.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def probe_ports() -> None:
    print(SEP)
    print("[PORTS]")
    print(SEP)
    for name, port in [
        ("SQL Server TCP", 1433),
        ("MySQL", 3306),
        ("PostgreSQL", 5432),
        ("Redis/Memurai", 6379),
        ("Elasticsearch", 9200),
        ("Milvus gRPC", 19530),
        ("Docker daemon", 2375),
    ]:
        print(f"  {'OPEN ' if tcp_open('127.0.0.1', port) else 'closed'} {port:>5}  {name}")


def probe_sqlserver() -> None:
    print(SEP)
    print("[SQL SERVER]")
    print(SEP)
    try:
        import pyodbc
    except ImportError:
        print("  pyodbc NOT installed")
        return

    drv = [d for d in pyodbc.drivers() if "SQL Server" in d]
    print(f"  ODBC drivers: {drv}")
    conn_str = (
        "Driver={ODBC Driver 17 for SQL Server};Server=localhost;"
        "Database=master;Trusted_Connection=yes;"
    )
    try:
        conn = pyodbc.connect(conn_str, timeout=8)
    except Exception as exc:  # noqa: BLE001
        print(f"  CONNECT FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return

    cur = conn.cursor()
    try:
        cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(64))")
        print(f"  product_version : {cur.fetchone()[0]}")
        cur.execute("SELECT CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))")
        print(f"  edition         : {cur.fetchone()[0]}")
        cur.execute("SELECT CAST(SERVERPROPERTY('Collation') AS NVARCHAR(128))")
        print(f"  collation       : {cur.fetchone()[0]}")
        cur.execute("SELECT CAST(SERVERPROPERTY('IsFullTextInstalled') AS INT)")
        print(f"  fulltext        : {cur.fetchone()[0]}")
        cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin')")
        print(f"  is_sysadmin     : {cur.fetchone()[0]}")
        cur.execute("SELECT name FROM sys.databases ORDER BY database_id")
        print(f"  databases       : {[r[0] for r in cur.fetchall()]}")
        # JSON support (SQL Server 2016+): JSON_VALUE
        cur.execute("""SELECT JSON_VALUE('{"sku":"WK-BEAR-20"}', '$.sku')""")
        print(f"  json_value      : {cur.fetchone()[0]}")
        # Unicode round-trip for Chinese text
        cur.execute("SELECT N'玩可爱 奶油小熊'")
        print(f"  nvarchar round  : {cur.fetchone()[0]}")
        # Any existing tables in rag_demo?
        cur.execute(
            "SELECT COUNT(*) FROM sys.databases WHERE name='rag_demo'"
        )
        if cur.fetchone()[0]:
            cur.execute(
                "USE rag_demo; SELECT COUNT(*) FROM sys.tables"
            )
            print(f"  rag_demo tables : {cur.fetchone()[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  QUERY FAILED: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        conn.close()


def probe_sqlalchemy() -> None:
    print(SEP)
    print("[SQLALCHEMY -> SQL SERVER]")
    print(SEP)
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("  sqlalchemy NOT installed")
        return
    url = (
        "mssql+pyodbc://@localhost/master"
        "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
    )
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            print(f"  dialect  : {engine.dialect.name}")
            print(f"  driver   : {engine.dialect.driver}")
            print(f"  server   : {conn.execute(text('SELECT @@SERVERNAME')).scalar()}")
            print(f"  version  : {conn.execute(text('SELECT @@VERSION')).scalar().splitlines()[0][:70]}")
            print(f"  unicode  : {conn.execute(text(chr(83) + 'ELECT N' + chr(39) + '玩可爱' + chr(39))).scalar()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")


def probe_redis() -> None:
    print(SEP)
    print("[REDIS / MEMURAI]")
    print(SEP)
    if not tcp_open("127.0.0.1", 6379):
        print("  6379 not listening")
        return
    try:
        import redis as redis_lib

        c = redis_lib.Redis(host="127.0.0.1", port=6379, socket_timeout=4, decode_responses=True)
        info = c.info("server")
        print(f"  ping            : {c.ping()}")
        print(f"  version         : {info.get('redis_version')} (memurai {info.get('memurai_version', 'n/a')})")
        print(f"  os              : {info.get('os')}")
        mods = c.module_list()
        print(f"  modules         : {[m.get('name') for m in mods]}")
        # Vector set capability (Redis 8.2 native VSET) - candidate vector store
        try:
            c.execute_command("DEL", "__vs_probe__")
            c.execute_command("VADD", "__vs_probe__", "VALUES", "3", "1.0", "0.0", "0.0", "doc1")
            c.execute_command("VADD", "__vs_probe__", "VALUES", "3", "0.0", "1.0", "0.0", "doc2")
            res = c.execute_command("VSIM", "__vs_probe__", "VALUES", "3", "1.0", "0.0", "0.0", "COUNT", "1", "WITHSCORES")
            print(f"  VSIM works      : {res}")
            c.execute_command("DEL", "__vs_probe__")
        except Exception as exc:  # noqa: BLE001
            print(f"  VSIM FAILED     : {type(exc).__name__}: {str(exc)[:160]}")
        # RedisJSON?
        try:
            c.json().set("__json_probe__", "$", {"a": 1})
            print(f"  RedisJSON works : {c.json().get('__json_probe__')}")
            c.delete("__json_probe__")
        except Exception as exc:  # noqa: BLE001
            print(f"  RedisJSON       : unavailable ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:200]}")


def probe_absent() -> None:
    print(SEP)
    print("[DOCKER / PG / MILVUS / ES]")
    print(SEP)
    import os
    import shutil

    print(f"  docker on PATH      : {shutil.which('docker')}")
    for p in (
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        r"C:\Program Files\PostgreSQL",
        r"C:\Program Files\Elasticsearch",
        r"C:\Program Files\Milvus",
    ):
        print(f"  exists {os.path.exists(p)!s:<5} {p}")


def main() -> int:
    print(f"Python {sys.version.split()[0]}  |  exe {sys.executable}")
    probe_ports()
    probe_sqlserver()
    probe_sqlalchemy()
    probe_redis()
    probe_absent()
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
