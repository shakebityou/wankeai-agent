# -*- coding: utf-8 -*-
"""验收第 3 步产出：data/FAQ知识库.csv、data/商品知识卡.csv、data/FAQ知识库.xlsx

对应商家资料索取清单 S-04（FAQ 话术知识库）与 S-12（商品知识卡）。
"""
import csv
import os
import re
import sys
from collections import Counter

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

FAQ_HEADERS = ["standard_q", "similar_q", "answer", "category", "authority",
               "review_status", "reviewer"]
CATEGORIES = {"洗护保养", "尺码规格", "材质成分", "发货时效", "退换货政策", "价格优惠"}
CARD_HEADERS = ["SKU编码", "商品名", "类目", "尺寸", "材质", "填充物", "适用年龄",
                "洗护方式", "包装形式", "上市时间", "是否IP联名", "吊牌价", "现价",
                "库存状态", "核心卖点", "知识文本", "更新日期"]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("{}  {}  {}".format("PASS" if ok else "FAIL", name, "-> " + str(detail) if detail else ""))


# ────────── 读取 ──────────
faq_path = os.path.join(DATA, "FAQ知识库.csv")
card_path = os.path.join(DATA, "商品知识卡.csv")
xlsx_path = os.path.join(DATA, "FAQ知识库.xlsx")
for p in (faq_path, card_path, xlsx_path):
    if not os.path.exists(p):
        print("缺少文件:", p)
        sys.exit(1)

faq = pd.read_csv(faq_path, encoding="utf-8-sig", dtype=str).fillna("")
card = pd.read_csv(card_path, encoding="utf-8-sig", dtype=str).fillna("")
sku = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"), encoding="utf-8-sig", dtype=str).fillna("")

# ────────── FAQ 检查 ──────────
check("FAQ 列名逐字一致", list(faq.columns) == FAQ_HEADERS, list(faq.columns))
check("FAQ 条数 >= 120", len(faq) >= 120, len(faq))
check("standard_q 无重复", faq["standard_q"].duplicated().sum() == 0,
      "重复 {} 条".format(faq["standard_q"].duplicated().sum()))
check("FAQ 无空值", (faq[FAQ_HEADERS] == "").sum().sum() == 0,
      (faq[FAQ_HEADERS] == "").sum().sum())

cats = set(faq["category"].unique())
check("覆盖 6 个知识分类", cats == CATEGORIES, sorted(cats))
check("每个分类都有条目", faq["category"].value_counts().min() >= 15,
      dict(Counter(faq["category"])))

status_ok = set(faq["review_status"].unique()) <= {"APPROVED", "PENDING_REVIEW"}
check("review_status 取值合法", status_ok, dict(Counter(faq["review_status"])))

# similar_q 必须多个用 | 分隔，且每个标准问至少 2 个相似问
sim_counts = faq["similar_q"].apply(lambda s: len(s.split("|")) if s else 0)
check("similar_q 均用 | 分隔且 >= 2 个", sim_counts.min() >= 2,
      "最少 {} 个，平均 {:.1f} 个，合计 {} 个".format(
          sim_counts.min(), sim_counts.mean(), sim_counts.sum()))

# answer 不得含占位符 / 待办词
bad_ph = faq[faq["answer"].str.contains(r"[{}]|TODO|待补充|XXX|xx元|待定", regex=True, na=False)]
check("answer 无占位符", len(bad_ph) == 0, bad_ph["standard_q"].tolist()[:3])

# answer 长度合理（可直接对外话术）
check("answer 长度 >= 40 字", faq["answer"].str.len().min() >= 40,
      "最短 {} 字，平均 {:.0f} 字".format(faq["answer"].str.len().min(), faq["answer"].str.len().mean()))

# authority 必须指向可追溯来源
auth_bad = faq[~faq["authority"].str.contains(
    r"商品主数据表|售后政策|物流发货SOP|营销活动规则|品牌与IP资料", regex=True, na=False)]
check("authority 均可追溯", len(auth_bad) == 0, auth_bad["standard_q"].tolist()[:3])

# 引用条款编号的 FAQ 数量（供第 4 步对齐）
ref = faq[faq["authority"].str.contains(r"第|\d+\.\d+ 条|条$", regex=True, na=False)]
check("存在引用条款编号的条目", len(ref) > 0, "{} 条引用 docs/ 条款".format(len(ref)))

reviewer_ok = (faq["reviewer"].str.len() >= 3).all()
check("reviewer 均已填写", bool(reviewer_ok), faq["reviewer"].nunique())

# ────────── 商品知识卡检查 ──────────
check("知识卡列名逐字一致", list(card.columns) == CARD_HEADERS, list(card.columns))
check("知识卡一款一条", len(card) == len(sku),
      "知识卡 {} 条 / SKU {} 条".format(len(card), len(sku)))
check("知识卡 SKU 与主数据完全一致",
      set(card["SKU编码"]) == set(sku["SKU编码"]),
      "差集 {}".format(set(card["SKU编码"]) ^ set(sku["SKU编码"])))
check("知识卡无空值", (card[CARD_HEADERS] == "").sum().sum() == 0,
      (card[CARD_HEADERS] == "").sum().sum())

# 知识卡前 14 列必须与主数据逐行一致
merged = card.merge(sku, on="SKU编码", suffixes=("_卡", "_主"))
diff_cols = []
for c in ["商品名", "类目", "尺寸", "材质", "填充物", "适用年龄", "洗护方式",
          "包装形式", "上市时间", "是否IP联名", "吊牌价", "现价", "库存状态"]:
    if not (merged[c + "_卡"] == merged[c + "_主"]).all():
        diff_cols.append(c)
check("知识卡字段与主数据一致", len(diff_cols) == 0, diff_cols)

check("知识文本长度 >= 80 字", card["知识文本"].str.len().min() >= 80,
      "最短 {} 字，平均 {:.0f} 字".format(card["知识文本"].str.len().min(),
                                          card["知识文本"].str.len().mean()))
check("知识文本含 SKU 编码", card.apply(lambda r: r["SKU编码"] in r["知识文本"], axis=1).all())
check("知识文本含洗护方式", card.apply(lambda r: r["洗护方式"] in r["知识文本"], axis=1).all())

# ────────── XLSX 检查 ──────────
xl = pd.ExcelFile(xlsx_path)
check("XLSX 含 3 个 Sheet", len(xl.sheet_names) == 3, xl.sheet_names)

x_faq = pd.read_excel(xlsx_path, sheet_name="FAQ知识库", dtype=str).fillna("")
x_card = pd.read_excel(xlsx_path, sheet_name="商品知识卡", dtype=str).fillna("")
check("XLSX FAQ Sheet 与 CSV 一致",
      x_faq.shape == faq.shape and list(x_faq.columns) == FAQ_HEADERS,
      "{} vs {}".format(x_faq.shape, faq.shape))
check("XLSX 知识卡 Sheet 与 CSV 一致",
      x_card.shape == card.shape and list(x_card.columns) == CARD_HEADERS,
      "{} vs {}".format(x_card.shape, card.shape))

# ────────── 汇总 ──────────
print()
print("FAQ 分类分布：")
print(faq["category"].value_counts().to_string())
print()
print("商品知识卡类目分布：")
print(card["类目"].value_counts().to_string())
print()
failed = [r for r in results if not r[1]]
print("RESULT:", "ALL PASS" if not failed else "{} 项 FAIL".format(len(failed)))
sys.exit(0 if not failed else 1)
