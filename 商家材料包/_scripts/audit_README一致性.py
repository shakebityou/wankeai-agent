# -*- coding: utf-8 -*-
"""README 自述 vs 磁盘事实 的差异检查。"""
import os
import re

D = r"D:\learn\homework\agent\商家材料包"
DATA = os.path.join(D, "data")
DOCS = os.path.join(D, "docs")
README = os.path.join(D, "README.md")

t = open(README, encoding="utf-8").read()

print("=" * 88)
print("README 自述 vs 磁盘事实")
print("=" * 88)

# 文件计数
top = [f for f in os.listdir(DATA) if os.path.isfile(os.path.join(DATA, f))]
docs = [f for f in os.listdir(DOCS) if f.endswith(".md")]
imgs = sum(len([f for f in fs if f.lower().endswith((".jpg", ".png"))])
           for _r, _d, fs in os.walk(os.path.join(DATA, "商品图片素材库")))
print("\n[事实] data 顶层 {} 个文件 / docs {} 份文档 / 图片 {} 张".format(
    len(top), len(docs), imgs))

# README 里的数字自述
for pat, label in [
        (r"共 \*\*(\d+) 个结构化数据文件", "结构化数据文件数"),
        (r"(\d+) 份文档", "文档数"),
        (r"(\d+) 张商品参考图", "图片数"),
        (r"(\d+) 张同类目相似参考图", "图片数(替代方案)"),
        (r"(\d+) 个 SKU", "SKU 数"),
        (r"(\d+) 个来源", "来源数")]:
    for m in re.finditer(pat, t):
        print("[自述] {} = {}".format(label, m.group(1)))

# README 里提到的所有相对路径是否存在
print("\n[路径校验] README 中提到的文件路径")
paths = set(re.findall(r"`([^`]+\.(?:csv|xlsx|md|json|py|jpg))`", t))
paths |= set(re.findall(r"`(data/[^`]+|docs/[^`]+|_scripts/[^`]+|_checks/[^`]+)`", t))
bad, globbed = [], 0
for p in sorted(paths):
    if "*" in p or "?" in p:
        import glob
        globbed += 1
        continue
    if not os.path.exists(os.path.join(D, p)):
        bad.append(p)
print("    检查 {} 个路径（含 {} 个通配符），不存在 {}".format(
    len(paths), globbed, len(bad)))
for p in bad:
    print("      ❌", p)

# 目录结构块里的每一项
print("\n[目录树校验] README 第 2 节目录树中列出的文件")
m = re.search(r"## 2\. 目录结构\s*```(.*?)```", t, re.S)
if m:
    tree = m.group(1)
    names = re.findall(r"[├└]──\s*([^\s←（]+)", tree)
    miss = []
    for n in names:
        n = n.strip().rstrip("/")
        if not n:
            continue
        # 在 data/docs/_scripts/_checks 下找
        hit = any(os.path.exists(os.path.join(D, sub, n))
                  for sub in ("data", "docs", "_scripts", "_checks", ""))
        # 允许 .csv / .xlsx 简写为「A.csv / .xlsx」
        if not hit:
            base = n.split(".")[0]
            hit = any(fn.startswith(base)
                      for sub in ("data", "docs", "_scripts")
                      if os.path.isdir(os.path.join(D, sub))
                      for fn in os.listdir(os.path.join(D, sub)))
        # 图片目录树：在 商品图片素材库 下按路径找
        if not hit:
            hit = os.path.exists(os.path.join(D, "data", "商品图片素材库", n))
        if not hit:
            parts = n.split("/")
            hit = all(os.path.exists(os.path.join(D, "data", "商品图片素材库", *parts[:i + 1]))
                      for i in range(len(parts)))
        if not hit:
            miss.append(n)
    print("    列出 {} 项，未找到 {} {}".format(len(names), len(miss), miss if miss else "✅"))

print("\n" + "=" * 88)
