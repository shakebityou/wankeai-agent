"""Verify SQL Server full-text + Redis vector-set can actually serve this project.

Questions to answer:
  1. Does SQL Server have a Chinese-capable full-text word breaker (needed for BM25 route)?
  2. Can SQL Server hold 1024-dim float arrays + JSON metadata with filtering?
  3. Can Redis 8.2 VSET hold 1024-dim vectors and filter by metadata (Milvus replacement)?
"""
from __future__ import annotations

import json
import random
import sys

SEP = "=" * 72


def sqlserver_fulltext() -> None:
    print(SEP)
    print("[SQL SERVER FULL-TEXT: Chinese word breaker?]")
    print(SEP)
    import pyodbc

    conn = pyodbc.connect(
        "Driver={ODBC Driver 17 for SQL Server};Server=localhost;"
        "Database=master;Trusted_Connection=yes;",
        timeout=10,
        autocommit=True,
    )
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT lcid, name FROM sys.fulltext_languages ORDER BY lcid"
        )
        langs = cur.fetchall()
        print(f"  fulltext languages ({len(langs)}):")
        interesting = {2052: "zh-CN", 1028: "zh-TW", 3076: "zh-HK", 1033: "en-US", 1041: "ja-JP", 1042: "ko-KR"}
        for lcid, name in langs:
            if lcid in interesting:
                print(f"    *** {lcid:>6}  {name}   <-- {interesting[lcid]}")
        zh = [l for l in langs if l[0] in (2052, 1028, 3076)]
        print(f"  Chinese word breakers present: {len(zh) > 0} -> {[(l[0], l[1]) for l in zh]}")

        cur.execute(
            "SELECT name, is_installed, is_default FROM sys.fulltext_components WHERE component_type=1"
        )
        print(f"  word breakers: {[(r[0], r[1], r[2]) for r in cur.fetchall()]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        conn.close()


def sqlserver_capabilities() -> None:
    print(SEP)
    print("[SQL SERVER: create schema, 1024-dim vector as VARBINARY, JSON filter]")
    print(SEP)
    import pyodbc

    conn = pyodbc.connect(
        "Driver={ODBC Driver 17 for SQL Server};Server=localhost;"
        "Database=master;Trusted_Connection=yes;",
        timeout=15,
        autocommit=True,
    )
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sys.databases WHERE name='wkagent_probe'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE wkagent_probe")
            print("  created database wkagent_probe")
        cur.execute("USE wkagent_probe")
        cur.execute("DROP TABLE IF EXISTS dbo.probe_chunks")
        cur.execute(
            """
            CREATE TABLE dbo.probe_chunks (
                chunk_id     NVARCHAR(64)  NOT NULL PRIMARY KEY,
                content      NVARCHAR(MAX) NOT NULL,
                embedding    VARBINARY(MAX) NULL,
                metadata     NVARCHAR(MAX) NULL,
                updated_at   DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
            )
            """
        )
        print("  created table probe_chunks (embedding VARBINARY(MAX), metadata NVARCHAR(MAX))")

        # 1024-dim float32 = 4096 bytes
        import struct

        vec = [random.random() for _ in range(1024)]
        blob = struct.pack(f"<{len(vec)}f", *vec)
        print(f"  1024-dim float32 blob size = {len(blob)} bytes (expect 4096)")

        cur.execute(
            "INSERT INTO dbo.probe_chunks (chunk_id, content, embedding, metadata) VALUES (?,?,?,?)",
            "c1",
            "奶油小熊 20cm 毛绒玩偶 手洗 30 度以下温水",
            blob,
            json.dumps({"category": "毛绒玩偶", "sku_ids": ["WK-BEAR-20"], "authority": 5}, ensure_ascii=False),
        )
        cur.execute("SELECT chunk_id, LEN(content), DATALENGTH(embedding), metadata FROM dbo.probe_chunks")
        row = cur.fetchone()
        print(f"  round-trip: id={row[0]} content_len={row[1]} blob_bytes={row[2]}")
        print(f"              metadata={row[3]}")

        # JSON filtering + reading a scalar out of metadata
        cur.execute(
            """
            SELECT chunk_id,
                   JSON_VALUE(metadata, '$.category') AS cat,
                   JSON_VALUE(metadata, '$.authority') AS auth
            FROM dbo.probe_chunks
            WHERE JSON_VALUE(metadata, '$.category') = N'毛绒玩偶'
            """
        )
        print(f"  JSON_VALUE filter -> {cur.fetchall()}")

        # Full-text index creation on Chinese content
        try:
            cur.execute(
                "SELECT FULLTEXTCATALOGPROPERTY('probe_ft', 'ItemCount')"
            )
        except Exception:
            pass
        try:
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name='probe_ft') "
                "CREATE FULLTEXT CATALOG probe_ft AS DEFAULT"
            )
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM sys.fulltext_indexes WHERE object_id=OBJECT_ID('dbo.probe_chunks')) "
                "CREATE FULLTEXT INDEX ON dbo.probe_chunks (content LANGUAGE 2052) "
                "KEY INDEX PK__probe_chunks ON probe_ft WITH CHANGE_TRACKING AUTO"
            )
            print("  fulltext index created with LANGUAGE 2052 (zh-CN)")
            cur.execute(
                "SELECT COLUMN_NAME, TYPE_COLUMN_NAME, LANGUAGE_ID "
                "FROM sys.fulltext_index_columns WHERE object_id=OBJECT_ID('dbo.probe_chunks')"
            )
            print(f"  ft columns: {cur.fetchall()}")
            cur.execute("ALTER FULLTEXT INDEX ON dbo.probe_chunks START FULL POPULATION")
            import time

            for _ in range(10):
                time.sleep(1)
                cur.execute(
                    "SELECT FULLTEXTCATALOGPROPERTY('probe_ft','ItemCount')"
                )
                n = cur.fetchone()[0]
                if n:
                    break
            print(f"  ft catalog item count = {n}")
            # CONTAINS with Chinese term
            cur.execute(
                "SELECT chunk_id FROM dbo.probe_chunks WHERE CONTAINS(content, N'\"奶油\"')"
            )
            print(f"  CONTAINS('奶油') -> {cur.fetchall()}")
            cur.execute(
                "SELECT chunk_id, ft.[RANK] FROM dbo.probe_chunks "
                "INNER JOIN CONTAINSTABLE(dbo.probe_chunks, content, N'\"奶油\"') AS ft "
                "ON dbo.probe_chunks.chunk_id = ft.[KEY]"
            )
            print(f"  CONTAINSTABLE -> {cur.fetchall()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FULLTEXT FAILED: {type(exc).__name__}: {str(exc)[:300]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        conn.close()


def redis_vectorset() -> None:
    print(SEP)
    print("[REDIS 8.2 VECTORSET: 1024-dim + metadata filter?]")
    print(SEP)
    import redis as redis_lib

    c = redis_lib.Redis(host="127.0.0.1", port=6379, socket_timeout=8, decode_responses=True)
    key = "__wk_vs_probe__"
    try:
        c.execute_command("DEL", key)

        # 1024-dim vector, no quantization, float32
        dim = 1024
        v1 = [0.0] * dim
        v1[0] = 1.0
        v2 = [0.0] * dim
        v2[1] = 1.0
        c.execute_command("VADD", key, "VALUES", dim, *[str(x) for x in v1], "doc_plush")
        c.execute_command("VADD", key, "VALUES", dim, *[str(x) for x in v2], "doc_order")
        print(f"  VADD 1024-dim x2 -> OK")

        # Attributes for metadata filtering
        c.execute_command("VSETATTR", key, "doc_plush", json.dumps({"category": "毛绒玩偶", "sku": "WK-BEAR-20"}))
        c.execute_command("VSETATTR", key, "doc_order", json.dumps({"category": "订单服务", "sku": "NONE"}))
        print("  VSETATTR -> OK")

        res = c.execute_command("VSIM", key, "VALUES", dim, *[str(x) for x in v1], "COUNT", "2", "WITHSCORES")
        print(f"  VSIM top2 -> {res}")

        # FILTER on attribute (the metadata-filtering requirement)
        res = c.execute_command(
            "VSIM", key, "VALUES", dim, *[str(x) for x in v1],
            "FILTER", ".category == \"毛绒玩偶\"", "COUNT", "2", "WITHSCORES",
        )
        print(f"  VSIM FILTER .category=='毛绒玩偶' -> {res}")

        res = c.execute_command("VSIM", key, "VALUES", dim, *[str(x) for x in v1], "FILTER", ".sku == \"WK-BEAR-20\"", "COUNT", "2")
        print(f"  VSIM FILTER .sku=='WK-BEAR-20' -> {res}")

        info = c.execute_command("VINFO", key)
        print(f"  VINFO -> {info}")
        c.execute_command("DEL", key)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:400]}")


def main() -> int:
    sqlserver_fulltext()
    sqlserver_capabilities()
    redis_vectorset()
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
