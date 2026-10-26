"""Second-round capability probe, fixing the two failures from round 1.

  1. SQL Server full-text needs a unique NON-CLUSTERED, non-nullable, single-column key.
  2. Redis VSET FILTER with a Chinese attribute value returned {} - need to find why
     (encoding of the filter expression vs the attribute value).
"""
from __future__ import annotations

import json
import time

SEP = "=" * 72


def sqlserver_fulltext_v2() -> None:
    print(SEP)
    print("[SQL SERVER FULL-TEXT v2: proper unique nonclustered key]")
    print(SEP)
    import pyodbc

    conn = pyodbc.connect(
        "Driver={ODBC Driver 17 for SQL Server};Server=localhost;"
        "Database=wkagent_probe;Trusted_Connection=yes;",
        timeout=15,
        autocommit=True,
    )
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS dbo.ft_chunks")
        cur.execute(
            """
            CREATE TABLE dbo.ft_chunks (
                chunk_id  NVARCHAR(64) NOT NULL,
                content   NVARCHAR(MAX) NOT NULL,
                category  NVARCHAR(64) NULL,
                CONSTRAINT PK_ft_chunks PRIMARY KEY NONCLUSTERED (chunk_id)
            )
            """
        )
        rows = [
            ("k1", "奶油小熊 20cm 毛绒玩偶 采用水晶超柔面料 PP棉填充 手洗30度以下温水轻揉 不可漂白 阴凉处平铺晾干", "毛绒玩偶"),
            ("k2", "订单发货后 物流单号可在订单详情查看 江浙沪次日达 偏远地区3-5天 如超时未更新可申请查询", "订单服务"),
            ("k3", "预售商品 预售期15天 付款后按预售时间发货 不支持无理由退货 质量问题可退换", "售后服务"),
            ("k4", "奶油色 抱枕 40cm 可拆洗 拉链设计 内胆独立 适合沙发装饰", "抱枕"),
        ]
        cur.executemany(
            "INSERT INTO dbo.ft_chunks (chunk_id, content, category) VALUES (?,?,?)", rows
        )
        print(f"  inserted {len(rows)} rows")

        cur.execute(
            "IF EXISTS (SELECT 1 FROM sys.fulltext_indexes WHERE object_id=OBJECT_ID('dbo.ft_chunks')) "
            "DROP FULLTEXT INDEX ON dbo.ft_chunks"
        )
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name='wk_ft_cat') "
            "CREATE FULLTEXT CATALOG wk_ft_cat AS DEFAULT"
        )
        cur.execute(
            "CREATE FULLTEXT INDEX ON dbo.ft_chunks (content LANGUAGE 2052) "
            "KEY INDEX PK_ft_chunks ON wk_ft_cat WITH CHANGE_TRACKING AUTO"
        )
        print("  fulltext index created (LANGUAGE 2052 Simplified Chinese)")

        cur.execute("ALTER FULLTEXT INDEX ON dbo.ft_chunks START FULL POPULATION")
        n = 0
        for _ in range(15):
            time.sleep(1)
            cur.execute("SELECT FULLTEXTCATALOGPROPERTY('wk_ft_cat','ItemCount')")
            n = cur.fetchone()[0] or 0
            if n:
                break
        print(f"  catalog ItemCount = {n}")

        for term in ["奶油", "毛绒", "玩偶", "FORMSOF(INFLECTIONAL, 奶油)", "预售"]:
            try:
                cur.execute(
                    f"SELECT chunk_id FROM dbo.ft_chunks WHERE CONTAINS(content, ?)", f'"{term}"'
                )
                hits = [r[0] for r in cur.fetchall()]
                print(f"  CONTAINS {term!r:32} -> {hits}")
            except Exception as exc:  # noqa: BLE001
                print(f"  CONTAINS {term!r} FAILED: {str(exc)[:150]}")

        # Ranked search = the BM25 route for BE-05
        try:
            cur.execute(
                """
                SELECT ft.[KEY], ft.[RANK], c.chunk_id, c.category
                FROM CONTAINSTABLE(dbo.ft_chunks, content, N'"奶油" OR "玩偶"') AS ft
                INNER JOIN dbo.ft_chunks c ON c.chunk_id = ft.[KEY]
                ORDER BY ft.[RANK] DESC
                """
            )
            print("  CONTAINSTABLE ranked (the BM25/稀疏路 substitute):")
            for r in cur.fetchall():
                print(f"      rank={r[1]:>6}  {r[2]}  {r[3]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  CONTAINSTABLE FAILED: {str(exc)[:200]}")

        # Can fulltext combine with structured filters? (needed: category + sku filters)
        cur.execute(
            """
            SELECT c.chunk_id FROM dbo.ft_chunks c
            INNER JOIN CONTAINSTABLE(dbo.ft_chunks, content, N'"奶油"') AS ft
              ON c.chunk_id = ft.[KEY]
            WHERE c.category = N'毛绒玩偶'
            """
        )
        print(f"  fulltext + category filter -> {[r[0] for r in cur.fetchall()]}")

        # Chinese word-break introspection
        try:
            cur.execute("SELECT display_term FROM sys.dm_fts_parser('\"奶油小熊 毛绒玩偶\"', 2052, 0, 0)")
            print(f"  dm_fts_parser zh-CN terms -> {[r[0] for r in cur.fetchall()]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  dm_fts_parser FAILED: {str(exc)[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        conn.close()


def redis_filter_debug() -> None:
    print(SEP)
    print("[REDIS VSET FILTER: why did Chinese attr filter return empty?]")
    print(SEP)
    import redis as redis_lib

    c = redis_lib.Redis(host="127.0.0.1", port=6379, socket_timeout=8, decode_responses=True)
    key = "__wk_vs_filter__"
    dim = 8
    try:
        c.execute_command("DEL", key)
        v = ["1"] + ["0"] * (dim - 1)
        c.execute_command("VADD", key, "VALUES", dim, *v, "plush")
        c.execute_command("VADD", key, "VALUES", dim, *v, "order")

        c.execute_command("VSETATTR", key, "plush", json.dumps({"category": "毛绒玩偶"}))
        c.execute_command("VSETATTR", key, "order", json.dumps({"category": "订单服务"}))
        got = c.execute_command("VGETATTR", key, "plush")
        print(f"  stored attr plush -> {got!r}")

        variants = [
            '.category == "毛绒玩偶"',
            ".category == '毛绒玩偶'",
            ".category == 毛绒玩偶",
            '.category=="毛绒玩偶"',
            '.category == "订单服务"',
            '.category == "plush"',
        ]
        for expr in variants:
            try:
                res = c.execute_command(
                    "VSIM", key, "VALUES", dim, *v, "FILTER", expr, "COUNT", "3"
                )
                print(f"  FILTER {expr!r:34} -> {res}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FILTER {expr!r:34} ERR {type(exc).__name__}: {str(exc)[:120]}")

        # Also test non-Chinese attribute value to isolate encoding vs syntax
        c.execute_command("VSETATTR", key, "plush", json.dumps({"cat": "plush", "n": 5}))
        for expr in ['.cat == "plush"', ".n == 5", ".n >= 4"]:
            try:
                res = c.execute_command(
                    "VSIM", key, "VALUES", dim, *v, "FILTER", expr, "COUNT", "3"
                )
                print(f"  FILTER {expr!r:34} -> {res}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FILTER {expr!r:34} ERR {type(exc).__name__}: {str(exc)[:120]}")

        # IN / array membership test (needed for sku_ids ARRAY_CONTAINS_ANY)
        c.execute_command("VSETATTR", key, "plush", json.dumps({"sku_ids": ["WK-BEAR-20", "WK-BEAR-30"]}))
        for expr in ['.sku_ids == "WK-BEAR-20"', '.sku_ids contains "WK-BEAR-20"']:
            try:
                res = c.execute_command(
                    "VSIM", key, "VALUES", dim, *v, "FILTER", expr, "COUNT", "3"
                )
                print(f"  FILTER {expr!r:36} -> {res}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FILTER {expr!r:36} ERR {type(exc).__name__}: {str(exc)[:120]}")
        c.execute_command("DEL", key)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    sqlserver_fulltext_v2()
    redis_filter_debug()
    print(SEP)
