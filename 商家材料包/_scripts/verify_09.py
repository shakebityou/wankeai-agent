# -*- coding: utf-8 -*-
"""S-02 商品图片素材库（爬取真实商品照片 + 按类目分文件夹）验收。

验收口径：
  1. 目录结构为「类目 / SKU / 图片」两级，5 个类目目录齐全
  2. 74 个 SKU 目录，目录名与主数据 SKU 编码完全一致
  3. 每个 SKU 恰好 4 张图（白底/细节/场景/包装）
  4. 索引 296 行，路径与磁盘实际文件一一对应
  5. 索引中的类目、SKU 与主数据一致，无空值
  6. 图片来源与署名清单齐全（合规要求）
  7. 抽样图片为 800x800，且非空白图（有实际内容）
  8. 同一 SKU 的 4 张图不完全相同（视图确实做了差异化处理）
"""
import os
import sys
from collections import Counter

import pandas as pd
from PIL import Image, ImageStat

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
IMGDIR = os.path.join(DATA, "商品图片素材库")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  {} {}  {}".format("PASS" if cond else "FAIL", name,
                               "" if cond else "-> {}".format(detail)))


CATS = ["毛绒玩偶", "挂件", "抱枕", "搪胶潮玩", "盲盒"]
VIEWS = ["主图1_白底", "主图2_细节", "主图3_场景", "主图4_包装"]

print("=" * 70)
print("S-02 商品图片素材库验收（爬取版）")
print("=" * 70)

# ── 1. 目录结构 ──
print("\n[1] 类目目录结构")
top = sorted(d for d in os.listdir(IMGDIR) if os.path.isdir(os.path.join(IMGDIR, d)))
check("顶层为 5 个类目目录", set(top) == set(CATS), top)
check("无多余目录（如 _smoketest / 旧扁平结构）",
      not [d for d in top if d.startswith("_") or d.startswith("WK-")], top)

# ── 2. SKU 目录 ──
print("\n[2] SKU 目录")
sku = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"), encoding="utf-8-sig", dtype=str).fillna("")
sku_codes = set(sku["SKU编码"])
disk = {}
for c in top:
    p = os.path.join(IMGDIR, c)
    for d in os.listdir(p):
        if os.path.isdir(os.path.join(p, d)):
            disk[d] = c
check("SKU 目录数 == 74", len(disk) == 74, len(disk))
check("目录名与主数据 SKU 编码完全一致", set(disk) == sku_codes,
      sorted(set(disk) ^ sku_codes)[:5])

# ── 3. 每个 SKU 归属正确类目 + 恰好 4 张 ──
print("\n[3] 归属类目与图片数量")
cat_of = dict(zip(sku["SKU编码"], sku["类目"]))
wrong = [(k, disk[k], cat_of.get(k)) for k in disk if disk.get(k) != cat_of.get(k)]
check("每个 SKU 落在自己的类目文件夹内", not wrong, wrong[:5])
counts = {k: len([f for f in os.listdir(os.path.join(IMGDIR, v, k))
                  if os.path.isfile(os.path.join(IMGDIR, v, k, f))]) for k, v in disk.items()}
check("每个 SKU 恰好 4 张图", set(counts.values()) == {4}, sorted(set(counts.values())))
dist = Counter(disk.values())
print("     类目分布:", dict(dist))
check("类目分布与主数据一致",
      dist == Counter(sku["类目"].value_counts().to_dict()), dist)

# ── 4. 文件名规范 ──
print("\n[4] 文件命名")
badname = []
for k, v in disk.items():
    for kind in VIEWS:
        fn = "{}_{}.jpg".format(k, kind)
        if not os.path.exists(os.path.join(IMGDIR, v, k, fn)):
            badname.append((k, fn))
check("每个 SKU 的 4 个标准文件名齐全", not badname, badname[:5])

# ── 5. 索引文件 ──
print("\n[5] 索引与署名")
idx = pd.read_csv(os.path.join(DATA, "商品图片索引.csv"), encoding="utf-8-sig", dtype=str).fillna("")
check("索引行数 == 296", idx.shape[0] == 296, idx.shape[0])
check("索引无空值", not idx.isna().any().any() and not (idx == "").any().any(),
      list(idx.columns[(idx == "").any()]))
need_cols = ["SKU编码", "商品名", "类目", "图片文件", "图片类型", "尺寸",
             "生成方式", "是否占位图", "来源平台", "来源页面", "作者", "授权协议", "原图地址", "备注"]
check("索引列齐全", set(need_cols).issubset(set(idx.columns)),
      sorted(set(need_cols) - set(idx.columns)))

# 路径格式：商品图片素材库/{类目}/{SKU}/{文件}
badfmt = []
for _, r in idx.iterrows():
    exp = "商品图片素材库/{}/{}/{}".format(r["类目"], r["SKU编码"],
                                          "{}_{}.jpg".format(r["SKU编码"],
                                                             r["图片类型"].replace("白底图", "主图1_白底")
                                                             .replace("细节图", "主图2_细节")
                                                             .replace("场景图", "主图3_场景")
                                                             .replace("包装图", "主图4_包装")))
    if r["图片文件"] != exp:
        badfmt.append((r["图片文件"], exp))
check("索引路径为「类目/SKU/文件」三级格式", not badfmt, badfmt[:3])

missing = [r["图片文件"] for _, r in idx.iterrows()
           if not os.path.exists(os.path.join(DATA, r["图片文件"].replace("/", os.sep)))]
check("索引路径全部真实存在", not missing, missing[:5])
check("索引 SKU 均属主数据", set(idx["SKU编码"]).issubset(sku_codes))
check("索引类目与主数据一致",
      all(cat_of.get(r["SKU编码"]) == r["类目"] for _, r in idx.iterrows()))
check("索引含占位图标注", (idx["是否占位图"] == "是").all())
check("生成方式标为爬取", (idx["生成方式"] == "爬取相似参考图").all(),
      idx["生成方式"].value_counts().to_dict())
check("每个 SKU 在索引中恰好 4 行", (idx.groupby("SKU编码").size() == 4).all())

attr_p = os.path.join(DATA, "商品图片来源与署名.csv")
check("署名清单存在", os.path.exists(attr_p))
if os.path.exists(attr_p):
    attr = pd.read_csv(attr_p, encoding="utf-8-sig", dtype=str).fillna("")
    n_uniq = idx["原图地址"].nunique()
    check("署名清单行数 == 去重来源数", attr.shape[0] == n_uniq,
          "{} vs {}".format(attr.shape[0], n_uniq))
    check("署名清单含来源页面与授权协议",
          {"来源页面", "授权协议", "原图地址"}.issubset(set(attr.columns)))

# ── 6. 图片本身 ──
print("\n[6] 图片内容")
files = []
for k, v in disk.items():
    for f in os.listdir(os.path.join(IMGDIR, v, k)):
        files.append(os.path.join(IMGDIR, v, k, f))
check("图片文件实际存在 296 个", len(files) == 296, len(files))

sample = files[::7][:44]
bad_size, blank = [], []
for f in sample:
    with Image.open(f) as im:
        if im.size != (800, 800):
            bad_size.append((os.path.basename(f), im.size))
        g = im.convert("L")
        st = ImageStat.Stat(g)
        # 标准差过小 = 纯色/空白图
        if st.stddev[0] < 12:
            blank.append((os.path.basename(f), round(st.stddev[0], 1)))
check("抽样图片尺寸均为 800x800", not bad_size, bad_size[:5])
check("抽样图片非空白图（灰度标准差 >= 12）", not blank, blank[:5])

# 同一 SKU 4 张图不完全相同
same = []
for k, v in list(disk.items())[:30]:
    hs = set()
    for kind in VIEWS:
        p = os.path.join(IMGDIR, v, k, "{}_{}.jpg".format(k, kind))
        if os.path.exists(p):
            hs.add(os.path.getsize(p))
    if len(hs) < 4:
        same.append((k, len(hs)))
check("同 SKU 四视图差异化（文件大小互不相同）", not same, same[:5])

print("\n" + "=" * 70)
print("PASS: {}    FAIL: {}".format(len(PASS), len(FAIL)))
if FAIL:
    print("\n失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("RESULT: ALL PASS")
