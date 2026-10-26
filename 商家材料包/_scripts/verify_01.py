# -*- coding: utf-8 -*-
"""校验第 1 步商品主数据表是否满足验收标准。"""
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

EXPECT = ["SKU编码", "商品名", "类目", "尺寸", "材质", "填充物", "适用年龄",
          "洗护方式", "包装形式", "上市时间", "是否IP联名", "吊牌价", "现价", "库存状态"]

df = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"))
ok = True

def chk(cond, msg):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        ok = False

chk(list(df.columns) == EXPECT, "列名逐字一致 -> {}".format(list(df.columns)))
chk(len(df) >= 60, "SKU 总数 >= 60 -> {}".format(len(df)))
chk(df["SKU编码"].is_unique, "SKU 编码全局唯一")
chk(df.isna().sum().sum() == 0, "无空值")
cats = sorted(df["类目"].unique())
chk(len(cats) == 5, "覆盖 5 个类目 -> {}".format(cats))
chk(df["SKU编码"].str.match(r"^WK-[A-Z]+-[A-Z0-9]+$").all(), "SKU 编码格式 WK-XXX-NN")
chk((df["现价"] <= df["吊牌价"]).all(), "现价 <= 吊牌价")

# 交叉校验 XLSX
xl = pd.read_excel(os.path.join(DATA, "商品主数据表.xlsx"), sheet_name="商品主数据")
chk(list(xl.columns) == EXPECT and len(xl) == len(df), "XLSX 与 CSV 内容一致")

print()
print("类目分布：")
print(df["类目"].value_counts().to_string())
print()
print("库存状态分布：")
print(df["库存状态"].value_counts().to_string())
print()
print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
sys.exit(0 if ok else 1)
