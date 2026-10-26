"""统计历史会话语料与商品数据的取值分布，供 Mock 生成器对齐真实口径。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

OUT = Path(r"D:\learn\homework\agent\frontend\mock\data")

sessions = json.loads((OUT / "history-sessions.json").read_text(encoding="utf-8"))
print("=== 历史会话语料 ===")
print("总行数:", len(sessions))
for col in sessions[0].keys():
    values = Counter(str(r.get(col, "")) for r in sessions)
    print(f"\n[{col}] 唯一值 {len(values)}")
    for val, cnt in values.most_common(12):
        shown = val if len(val) <= 60 else val[:60] + "…"
        print(f"    {cnt:>4}  {shown}")

products = json.loads((OUT / "products.json").read_text(encoding="utf-8"))
print("\n=== 商品知识卡 ===")
for col in ("category", "stock_status", "is_ip", "age_range"):
    values = Counter(str(r.get(col, "")) for r in products)
    print(f"[{col}] {dict(values.most_common(10))}")

print("\n=== 示例商品 ===")
print(json.dumps(products[0], ensure_ascii=False, indent=2)[:900])

faqs = json.loads((OUT / "faqs.json").read_text(encoding="utf-8"))
print("\n=== FAQ ===")
print("[category]", dict(Counter(f["category"] for f in faqs).most_common(15)))
print("[authority]", dict(Counter(f["authority"] for f in faqs).most_common(15)))
print("[review_status]", dict(Counter(f["review_status"] for f in faqs)))

groups = json.loads((OUT / "agent-groups.json").read_text(encoding="utf-8"))
print("\n=== 坐席分组 ===")
print(json.dumps(groups, ensure_ascii=False, indent=2)[:1400])
