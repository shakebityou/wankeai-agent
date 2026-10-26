# -*- coding: utf-8 -*-
"""第 6 步验收：README 总入口 + 新增表格/文档。

检查项：
  1. README.md 存在且含 22 个索取编号 S-01 ~ S-22
  2. README 中出现的每个 `data/...` 或 `docs/...` 路径都真实存在
  3. README 含交付批次表（第 1/2/3/4 批）
  4. README 含脱敏与安全说明、交接清单、一句话结论
  5. 新增文档存在且含四要素（文档编号/生效日期/适用渠道/例外情形）
  6. 新增 CSV/XLSX 可被 pandas 读取，且无空值
  7. 图片素材库：74 个 SKU 目录、每 SKU 4 张、索引与主数据一致
"""
import os
import re
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
DOCS = os.path.join(BASE, "docs")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(detail)) if detail else ""))


print("=" * 70)
print("第 6 步验收：README 总入口与新增资料")
print("=" * 70)

readme_path = os.path.join(BASE, "README.md")
check("README.md 存在", os.path.exists(readme_path))
readme = open(readme_path, encoding="utf-8").read()

# ── 1. 22 个索取编号全覆盖 ──
print("\n[1] S-01 ~ S-22 覆盖")
missing = [f"S-{i:02d}" for i in range(1, 23) if f"S-{i:02d}" not in readme]
check("README 覆盖 S-01 ~ S-22 全部 22 项", not missing, missing)

# ── 2. README 中引用的路径真实存在 ──
print("\n[2] README 引用路径真实性")
paths = set(re.findall(r"`((?:data|docs|_scripts|_checks)/[^`]+?)`", readme))
# 通配符写法（如 _scripts/verify_*.py）不是具体路径，按 glob 展开校验
import glob as _glob
bad = []
for p in sorted(paths):
    if "*" in p or "?" in p:
        if not _glob.glob(os.path.join(BASE, p)):
            bad.append(p + "（通配符未匹配到文件）")
        continue
    p2 = p.rstrip("/")
    full = os.path.join(BASE, p2)
    if not os.path.exists(full):
        bad.append(p2)
check("README 引用的文件/目录全部存在（{} 个）".format(len(paths)), not bad, bad)

# ── 3. 交付批次表 ──
print("\n[3] 交付批次表")
for kw in ["第 1 批", "第 2 批", "第 3 批", "第 4 批"]:
    check("含「{}」".format(kw), kw in readme)
check("含责任方列", "责任方" in readme)
check("含期望时间列", "期望" in readme)

# ── 4. 必需章节 ──
print("\n[4] 必需章节")
check("含一句话结论", "一句话结论" in readme)
check("含脱敏与安全说明", "脱敏" in readme and "安全" in readme)
check("含交接清单", "交接清单" in readme)
check("含无法提供资料的替代方案", "替代方案" in readme)
check("含目录结构", "目录结构" in readme)
check("含数据质量自查结果", "验收" in readme or "自查" in readme)

# ── 5. 新增文档四要素 ──
print("\n[5] 新增文档四要素")
new_docs = {
    "现有知识库资产说明.md": "WK-DOC-LEGACY-2026-01",
    "渠道接入资质说明.md": "WK-DOC-CHANNEL-2026-01",
    "服务器与部署环境说明.md": "WK-DOC-INFRA-2026-01",
    "数据合规要求说明.md": "WK-DOC-COMPLIANCE-2026-01",
    "预算与成本说明.md": "WK-DOC-BUDGET-2026-01",
}
for fn, doc_no in new_docs.items():
    p = os.path.join(DOCS, fn)
    if not os.path.exists(p):
        check("文档存在：" + fn, False, "文件不存在")
        continue
    t = open(p, encoding="utf-8").read()
    check("{} 含文档编号 {}".format(fn, doc_no), doc_no in t)
    check("{} 含生效日期".format(fn), "生效日期" in t)
    check("{} 含适用渠道".format(fn), "适用渠道" in t)
    check("{} 含例外情形章节".format(fn), "例外情形" in t)

# ── 6. 新增数据文件 ──
print("\n[6] 新增数据文件可读性与无空值")
new_csvs = {
    "业务对接人信息表.csv": 7,
    "客服坐席分组表.csv": 6,
    "商品分类体系表.csv": 6,
    "商品图片索引.csv": 296,
}
for fn, rows in new_csvs.items():
    p = os.path.join(DATA, fn)
    if not os.path.exists(p):
        check("CSV 存在：" + fn, False)
        continue
    df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
    check("{} 行数 == {}".format(fn, rows), df.shape[0] == rows, df.shape[0])
    check("{} 无空值".format(fn), not df.isna().any().any())

new_xlsx = {
    "业务对接人信息表.xlsx": ["业务对接人"],
    "客服组织结构表.xlsx": ["坐席分组", "在线时段", "排班表", "峰值人力"],
    "商品分类体系表.xlsx": ["类目树"],
}
for fn, sheets in new_xlsx.items():
    p = os.path.join(DATA, fn)
    if not os.path.exists(p):
        check("XLSX 存在：" + fn, False)
        continue
    xl = pd.ExcelFile(p)
    check("{} Sheet 齐全 {}".format(fn, sheets), set(sheets).issubset(set(xl.sheet_names)), xl.sheet_names)

# ── 7. 图片素材库（类目 / SKU / 图片 两级结构）──
print("\n[7] S-02 图片素材库")
sku = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"), encoding="utf-8-sig", dtype=str)
imgdir = os.path.join(DATA, "商品图片素材库")
cats = sorted(d for d in os.listdir(imgdir) if os.path.isdir(os.path.join(imgdir, d)))
check("顶层为 5 个类目目录", len(cats) == 5, cats)
disk = {}
for c in cats:
    for d in os.listdir(os.path.join(imgdir, c)):
        if os.path.isdir(os.path.join(imgdir, c, d)):
            disk[d] = c
check("SKU 目录数 == 74", len(disk) == 74, len(disk))
check("目录名与 SKU 编码完全一致", set(disk) == set(sku["SKU编码"]),
      sorted(set(disk) ^ set(sku["SKU编码"]))[:5])
counts = {d: len([f for f in os.listdir(os.path.join(imgdir, c, d))
                  if os.path.isfile(os.path.join(imgdir, c, d, f))])
          for d, c in disk.items()}
check("每个 SKU 恰好 4 张图", set(counts.values()) == {4}, sorted(set(counts.values())))
idx = pd.read_csv(os.path.join(DATA, "商品图片索引.csv"), encoding="utf-8-sig", dtype=str)
check("图片索引行数 == 296", idx.shape[0] == 296, idx.shape[0])
check("索引中 SKU 均属主数据", set(idx["SKU编码"]).issubset(set(sku["SKU编码"])))
check("索引含占位图标注", "是否占位图" in idx.columns and (idx["是否占位图"] == "是").all())
files = []
for d, c in disk.items():
    for f in os.listdir(os.path.join(imgdir, c, d)):
        p = os.path.join(imgdir, c, d, f)
        if os.path.isfile(p):
            files.append(p)
check("图片文件实际存在 296 个", len(files) == 296, len(files))
bad_size = []
from PIL import Image
for f in files[::30]:
    with Image.open(f) as im:
        if im.size != (800, 800):
            bad_size.append((f, im.size))
check("抽样图片尺寸均为 800x800", not bad_size, bad_size)
check("署名清单存在", os.path.exists(os.path.join(DATA, "商品图片来源与署名.csv")))

print("\n" + "=" * 70)
print("PASS: {}    FAIL: {}".format(len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    print("RESULT: FAILED")
    sys.exit(1)
print("RESULT: ALL PASS")
