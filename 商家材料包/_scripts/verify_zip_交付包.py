# -*- coding: utf-8 -*-
"""解压验证：把交付包解到临时目录，逐项确认可用性。"""
import os
import re
import shutil
import sys
import tempfile
import zipfile

import pandas as pd

ZIP = r"D:\learn\homework\agent\玩可爱商家材料包_20260101.zip"
TOP = "商家材料包"

tmp = tempfile.mkdtemp(prefix="verify_zip_")
print("解压到:", tmp)
try:
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(tmp)
    root = os.path.join(tmp, TOP)
    print("顶层目录存在:", os.path.isdir(root))

    # 1. 中文文件名是否正确
    print("\n[1] 中文文件名")
    for n in ["data/商品主数据表.xlsx", "data/历史客服会话语料.csv",
              "docs/售后政策.md", "交付包说明.md",
              "data/商品图片素材库/毛绒玩偶/WK-BEAR-20/WK-BEAR-20_主图1_白底.jpg"]:
        print("    {} {}".format("✅" if os.path.exists(os.path.join(root, n)) else "❌", n))

    # 2. 所有 CSV 可被 pandas 读取且无空值
    print("\n[2] CSV 可读性")
    ddir = os.path.join(root, "data")
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith(".csv"):
            continue
        df = pd.read_csv(os.path.join(ddir, fn), encoding="utf-8-sig")
        nulls = int(df.isna().sum().sum())
        print("    {:<24} {:>4} 行 × {:>2} 列  空值 {}".format(fn, df.shape[0], df.shape[1], nulls))

    # 3. 所有 XLSX 可打开、Sheet 数
    print("\n[3] XLSX 可读性")
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith(".xlsx"):
            continue
        xl = pd.ExcelFile(os.path.join(ddir, fn))
        print("    {:<24} {} 个 Sheet: {}".format(fn, len(xl.sheet_names), xl.sheet_names))

    # 4. 图片完整可解码
    print("\n[4] 图片可解码性")
    from PIL import Image
    idir = os.path.join(ddir, "商品图片素材库")
    n_ok = n_bad = 0
    sizes = set()
    for r, _dd, fs in os.walk(idir):
        for f in fs:
            if not f.lower().endswith(".jpg"):
                continue
            try:
                with Image.open(os.path.join(r, f)) as im:
                    im.verify()
                with Image.open(os.path.join(r, f)) as im:
                    sizes.add(im.size)
                n_ok += 1
            except Exception as e:
                n_bad += 1
                if n_bad <= 3:
                    print("    ❌ {} {}".format(f, e))
    print("    可解码 {} 张，损坏 {} 张，尺寸集合 {}".format(n_ok, n_bad, sizes))

    # 5. 脱敏零残留（对解压后的文件复检）
    print("\n[5] 脱敏复检（解压后）")
    PAT = {"手机号": r"(?<!\d)1[3-9]\d{9}(?!\d)",
           "订单号": r"(?<!\d)20\d{10}(?!\d)",
           "运单号": r"(?<!\d)78\d{10}(?!\d)",
           "身份证": r"(?<!\d)\d{17}[\dXx](?!\d)"}
    for fn in ["历史客服会话语料.csv", "多轮客服对话语料.csv"]:
        df = pd.read_csv(os.path.join(ddir, fn), encoding="utf-8-sig", dtype=str)
        blob = "\n".join("\n".join(df[c].fillna("").astype(str))
                         for c in df.columns if df[c].dtype == object)
        bad = {k: len(re.findall(v, blob)) for k, v in PAT.items()}
        bad = {k: v for k, v in bad.items() if v}
        print("    {:<24} {}".format(fn, bad if bad else "零残留 ✅"))

    # 6. docs 可读
    print("\n[6] 文档可读性")
    ddir2 = os.path.join(root, "docs")
    for fn in sorted(os.listdir(ddir2)):
        t = open(os.path.join(ddir2, fn), encoding="utf-8").read()
        print("    {:<28} {} 字符".format(fn, len(t)))

    # 7. 确认未包含内部文件
    print("\n[7] 内部文件应被排除")
    for n in ["_scripts", "_checks", "_img_cache", ".keep"]:
        p = os.path.join(root, n)
        print("    {} {} {}".format("✅" if not os.path.exists(p) else "❌",
                                    n, "已排除" if not os.path.exists(p) else "仍在包内"))

    print("\n" + "=" * 80)
    print("解压验证完成")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    print("临时目录已清理")
