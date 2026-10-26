"""把商家材料包里的真实数据转成前端 Mock 用的 JSON。

用途：FE-01/FE-02/FE-05/FE-06/FE-09/FE-12 的 Mock 必须基于商家真实数据
（74 SKU 商品知识卡 + 126 条 FAQ + 6 个坐席分组 + 历史会话语料），
否则切分预览、指标看板、排班网格的演示都不可信。

输出：frontend/mock/data/*.json
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(r"D:\learn\homework\agent")
SRC = ROOT / "商家材料包" / "data"
OUT = ROOT / "frontend" / "mock" / "data"
OUT.mkdir(parents=True, exist_ok=True)


def read_csv(name: str) -> list[dict[str, str]]:
    path = SRC / name
    # 商家材料为 UTF-8（带 BOM 的情况用 utf-8-sig 兜底）
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with path.open("r", encoding=enc, newline="") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"无法解码 {name}")


def dump(name: str, payload) -> None:
    target = OUT / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    size = target.stat().st_size
    count = len(payload) if isinstance(payload, list) else len(payload.keys())
    print(f"  {name:32s} {count:>5} 条  {size/1024:>7.1f} KB")


# ── 1. 商品知识卡（74 张）────────────────────────────────────
cards = read_csv("商品知识卡.csv")
products = []
for row in cards:
    sku = (row.get("SKU编码") or "").strip()
    if not sku:
        continue
    products.append(
        {
            "sku_id": sku,
            "name": (row.get("商品名") or "").strip(),
            "category": (row.get("类目") or "").strip(),
            "size": (row.get("尺寸") or "").strip(),
            "material": (row.get("材质") or "").strip(),
            "filling": (row.get("填充物") or "").strip(),
            "age_range": (row.get("适用年龄") or "").strip(),
            "care": (row.get("洗护方式") or "").strip(),
            "packaging": (row.get("包装形式") or "").strip(),
            "launch_date": (row.get("上市时间") or "").strip(),
            "is_ip": (row.get("是否IP联名") or "").strip(),
            "list_price": (row.get("吊牌价") or "").strip(),
            "price": (row.get("现价") or "").strip(),
            "stock_status": (row.get("库存状态") or "").strip(),
            "selling_points": (row.get("核心卖点") or "").strip(),
            "knowledge_text": (row.get("知识文本") or "").strip(),
            "updated_at": (row.get("更新日期") or "").strip(),
        }
    )
print(f"商品知识卡：{len(products)} 张")
dump("products.json", products)

# ── 2. FAQ 知识库（126 条）───────────────────────────────────
faqs = read_csv("FAQ知识库.csv")
faq_items = []
for idx, row in enumerate(faqs, start=1):
    q = (row.get("standard_q") or "").strip()
    if not q:
        continue
    similar = [s.strip() for s in (row.get("similar_q") or "").split("|") if s.strip()]
    faq_items.append(
        {
            "chunk_id": f"FAQ-{idx:04d}",
            "standard_q": q,
            "similar_q": similar,
            "answer": (row.get("answer") or "").strip(),
            "category": (row.get("category") or "").strip(),
            "authority": (row.get("authority") or "").strip(),
            "review_status": (row.get("review_status") or "").strip(),
            "reviewer": (row.get("reviewer") or "").strip(),
        }
    )
print(f"FAQ 知识库：{len(faq_items)} 条")
dump("faqs.json", faq_items)

# ── 3. 坐席分组（6 组）──────────────────────────────────────
groups = read_csv("客服坐席分组表.csv")
group_items = []
for row in groups:
    gid = (row.get("分组编号") or "").strip()
    if not gid:
        continue
    group_items.append(
        {
            "group_id": gid,
            "group_name": (row.get("分组名称") or "").strip(),
            "service_scope": (row.get("负责范围") or "").strip(),
            "agent_count": int(re.sub(r"\D", "", row.get("坐席人数") or "0") or 0),
            "supervisor": (row.get("主管") or "").strip(),
            "online_window": (row.get("在线时段") or "").strip(),
            "channels_raw": (row.get("服务渠道") or "").strip(),
            "daily_volume": int(re.sub(r"\D", "", row.get("日均接待量") or "0") or 0),
            "peak_volume": int(re.sub(r"\D", "", row.get("峰值接待量") or "0") or 0),
            "note": (row.get("备注") or "").strip(),
        }
    )
print(f"坐席分组：{len(group_items)} 组")
dump("agent-groups.json", group_items)

# ── 4. 商品分类体系（用于知识类型与切分单元）─────────────────
cats = read_csv("商品分类体系表.csv")
cat_items = []
for row in cats:
    lvl1 = (row.get("一级类目") or "").strip()
    if not lvl1:
        continue
    cat_items.append({k.strip(): (v or "").strip() for k, v in row.items() if k})
print(f"商品分类体系：{len(cat_items)} 行")
dump("categories.json", cat_items)

# ── 5. 业务对接人（用于运维手册与联调说明）───────────────────
contacts = read_csv("业务对接人信息表.csv")
contact_items = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in contacts]
print(f"业务对接人：{len(contact_items)} 人")
dump("contacts.json", contact_items)

# ── 6. 历史会话语料（抽样，用于回放页与指标口径）─────────────
sessions = read_csv("历史客服会话语料.csv")
dump("history-sessions.json", sessions)
print(f"历史会话语料：{len(sessions)} 行，列 = {list(sessions[0].keys()) if sessions else '空'}")

print(f"\n输出目录：{OUT}")
