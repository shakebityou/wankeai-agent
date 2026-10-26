"""Decisive test: is the Redis VSET Chinese-attribute filter failure an encoding bug?

Hypothesis: json.dumps() defaults to ensure_ascii=True, so the stored attribute value
became the literal escape text "\\u6bdb\\u7ed2..." instead of the Chinese characters.
If so, ensure_ascii=False fixes metadata filtering - which is a hard BE-05 requirement.
"""
from __future__ import annotations

import json

SEP = "=" * 72
DIM = 8


def main() -> int:
    import redis as redis_lib

    c = redis_lib.Redis(host="127.0.0.1", port=6379, socket_timeout=8, decode_responses=True)
    key = "__wk_vs_cn__"
    v = ["1"] + ["0"] * (DIM - 1)

    print(SEP)
    print("[A] ensure_ascii=True (what the first probe used)")
    print(SEP)
    c.execute_command("DEL", key)
    c.execute_command("VADD", key, "VALUES", DIM, *v, "plush")
    c.execute_command("VSETATTR", key, "plush", json.dumps({"category": "毛绒玩偶"}))
    print(f"  stored -> {c.execute_command('VGETATTR', key, 'plush')!r}")
    res = c.execute_command("VSIM", key, "VALUES", DIM, *v, "FILTER", '.category == "毛绒玩偶"', "COUNT", "3")
    print(f"  FILTER Chinese -> {res}   {'EMPTY (bug)' if not res else 'ok'}")

    print(SEP)
    print("[B] ensure_ascii=False")
    print(SEP)
    c.execute_command("DEL", key)
    c.execute_command("VADD", key, "VALUES", DIM, *v, "plush")
    c.execute_command("VADD", key, "VALUES", DIM, *v, "order")
    c.execute_command("VSETATTR", key, "plush", json.dumps({"category": "毛绒玩偶", "sku_ids": ["WK-BEAR-20", "WK-BEAR-30"], "authority": 5}, ensure_ascii=False))
    c.execute_command("VSETATTR", key, "order", json.dumps({"category": "订单服务", "sku_ids": [], "authority": 3}, ensure_ascii=False))
    print(f"  stored plush -> {c.execute_command('VGETATTR', key, 'plush')!r}")

    cases = [
        '.category == "毛绒玩偶"',
        '.category == "订单服务"',
        '.category == "不存在的类目"',
        '.sku_ids == "WK-BEAR-20"',
        '.authority >= 4',
        '.authority >= 5 && .category == "毛绒玩偶"',
        '.category == "毛绒玩偶" || .category == "订单服务"',
        '.category in ["毛绒玩偶", "订单服务"]',
    ]
    for expr in cases:
        try:
            res = c.execute_command("VSIM", key, "VALUES", DIM, *v, "FILTER", expr, "COUNT", "5")
            print(f"  {expr:<52} -> {res}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {expr:<52} ERR {type(exc).__name__}: {str(exc)[:110]}")

    print(SEP)
    print("[C] scale + persistence sanity")
    print(SEP)
    c.execute_command("DEL", key)
    import random

    for i in range(200):
        vec = [str(random.random()) for _ in range(DIM)]
        c.execute_command("VADD", key, "VALUES", DIM, *vec, f"doc{i}")
        c.execute_command("VSETATTR", key, f"doc{i}", json.dumps(
            {"category": "毛绒玩偶" if i % 2 == 0 else "订单服务", "authority": (i % 5) + 1}, ensure_ascii=False))
    res = c.execute_command("VSIM", key, "VALUES", DIM, *v, "FILTER", '.category == "毛绒玩偶"', "COUNT", "5", "WITHSCORES")
    print(f"  200 docs, filtered top5 -> {res}")
    print(f"  VINFO -> {c.execute_command('VINFO', key)}")
    print(f"  key type -> {c.type(key)}")
    c.execute_command("DEL", key)
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
