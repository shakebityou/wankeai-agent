# -*- coding: utf-8 -*-
"""严格核查：找出交付物中真正有水分的地方。"""
import collections
import hashlib
import os
import re
import sys

import pandas as pd

D = r"D:\learn\homework\agent\商家材料包"
DATA = os.path.join(D, "data")
IMGDIR = os.path.join(DATA, "商品图片素材库")

print("=" * 88)
print("严格核查（不信任自述，只认磁盘事实）")
print("=" * 88)

# ── 1. 图片去重：每张图内容是否真的唯一 ──
print("\n[1] 图片内容去重")
idx = pd.read_csv(os.path.join(DATA, "商品图片索引.csv"), encoding="utf-8-sig", dtype=str)
print("    索引列:", list(idx.columns))
print("    图片文件示例:", repr(idx["图片文件"].iloc[0]))
h = collections.Counter()
found = 0
for _, r in idx.iterrows():
    p = os.path.join(DATA, r["图片文件"].replace("/", os.sep))
    if os.path.exists(p):
        found += 1
        h[hashlib.md5(open(p, "rb").read()).hexdigest()] += 1
print("    索引行数 {} / 磁盘找到 {} / 唯一内容 {}".format(len(idx), found, len(h)))
dup = {k: v for k, v in h.items() if v > 1}
print("    重复内容组: {} {}".format(len(dup), list(dup.values())[:8]))
print("    结论:", "每张图内容唯一 ✅" if not dup and found == len(idx)
      else "存在复用 ⚠️")

# ── 2. 语料 PII 残留 ──
print("\n[2] 语料脱敏真实性（未脱敏 PII 残留）")
PAT = {
    "手机号(11位)": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "订单号(12位)": r"(?<!\d)20\d{10}(?!\d)",
    "运单号(12位)": r"(?<!\d)78\d{10}(?!\d)",
    "身份证(18位)": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "银行卡(16-19位)": r"(?<!\d)\d{16,19}(?!\d)",
}
for fn, cols in [("历史客服会话语料.csv", ["用户问题", "客服回复"]),
                 ("多轮客服对话语料.csv", ["内容"]),
                 ("FAQ知识库.csv", ["answer"]),
                 ("商品知识卡.csv", ["知识文本"])]:
    p = os.path.join(DATA, fn)
    if not os.path.exists(p):
        continue
    df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
    use = [c for c in cols if c in df.columns]
    blob = "\n".join("\n".join(df[c].fillna("").astype(str)) for c in use)
    hits = {k: len(re.findall(v, blob)) for k, v in PAT.items()}
    mask = blob.count("****")
    bad = {k: v for k, v in hits.items() if v}
    print("    {:<22} 掩码****={:<4} 残留={}".format(
        fn, mask, bad if bad else "无 ✅"))

# ── 3. 未真正交付的项（占位/走安全渠道）──
print("\n[3] 未随文件交付、需线下补齐的项")
for fn in ["业务对接人信息表.csv", "ERP供应商信息表.csv"]:
    p = os.path.join(DATA, fn)
    df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
    for c in df.columns:
        n = (df[c].astype(str).str.contains("走安全渠道", na=False)).sum()
        if n:
            print("    {} · {} 列：{} 行标注「走安全渠道提供」".format(fn, c, n))

# ── 4. 文档编号/生效日期/条款数 一致性 ──
print("\n[4] 文档四要素齐全性（编号 / 生效日期 / 适用渠道 / 例外情形）")
DOCS = os.path.join(D, "docs")
for fn in sorted(os.listdir(DOCS)):
    if not fn.endswith(".md"):
        continue
    t = open(os.path.join(DOCS, fn), encoding="utf-8").read()
    no = re.search(r"(WK-[A-Z0-9\-]+-\d{4}-\d{2})", t)
    eff = "生效日期" in t
    ch = "适用渠道" in t
    exc = "例外情形" in t
    clauses = len(re.findall(r"^### (\d+\.\d+) ", t, re.M))
    print("    {:<28} 编号={:<28} 生效={} 渠道={} 例外={} 条款={}".format(
        fn, no.group(1) if no else "—",
        "✅" if eff else "❌", "✅" if ch else "❌", "✅" if exc else "❌", clauses))

# ── 5. 交叉引用完整性：FAQ 引用的条款是否都存在 ──
print("\n[5] FAQ authority 引用的条款是否都能在文档中找到")
faq = pd.read_csv(os.path.join(DATA, "FAQ知识库.csv"), encoding="utf-8-sig", dtype=str)
refs = collections.defaultdict(set)
for a in faq["authority"].fillna(""):
    for doc, num in re.findall(r"(售后政策|物流发货SOP|营销活动规则|品牌与IP资料)\s*(\d+\.\d+)\s*条", a):
        refs[doc].add(num)
FILEMAP = {"售后政策": "售后政策.md", "物流发货SOP": "物流发货SOP.md",
           "营销活动规则": "营销活动规则.md", "品牌与IP资料": "品牌与IP资料.md"}
allok = True
for doc, nums in sorted(refs.items()):
    t = open(os.path.join(DOCS, FILEMAP[doc]), encoding="utf-8").read()
    have = set(re.findall(r"^### (\d+\.\d+) ", t, re.M))
    miss = nums - have
    if miss:
        allok = False
    print("    {:<16} 引用 {} 个条款，缺失 {} {}".format(
        doc, len(nums), len(miss), sorted(miss) if miss else "✅"))
print("    结论:", "全部可追溯 ✅" if allok else "存在悬空引用 ⚠️")

# ── 6. 图片是否真的「属于」该 SKU（同系列是否雷同）──
print("\n[6] 同系列 SKU 是否拿到不同照片（防止一图多用）")
ser = collections.defaultdict(list)
for _, r in idx.iterrows():
    m = re.match(r"^(WK-[A-Z]+)", r["SKU编码"])
    if m:
        ser[m.group(1)].append(r["图片文件"])
multi = {k: v for k, v in ser.items() if len(v) > 4}
bad = 0
for k, files in sorted(multi.items()):
    hs = set()
    for f in files:
        p = os.path.join(DATA, f.replace("/", os.sep))
        if os.path.exists(p):
            hs.add(hashlib.md5(open(p, "rb").read()).hexdigest())
    if len(hs) < len(files):
        bad += 1
        print("    {} 需要 {} 张，实际唯一 {} 张 ⚠️".format(k, len(files), len(hs)))
print("    多 SKU 系列 {} 个，存在复用 {} 个 {}".format(
    len(multi), bad, "✅" if bad == 0 else "⚠️"))

print("\n" + "=" * 88)
