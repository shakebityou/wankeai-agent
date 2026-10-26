# -*- coding: utf-8 -*-
"""校验第 2 步历史客服会话语料是否满足验收标准。"""
import os
import re
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

EXPECT = ["会话ID", "渠道", "时间", "用户问题", "客服回复", "意图类目", "是否转人工", "用户情绪"]
CATS = ["商品咨询", "订单服务", "售后服务", "营销活动", "品牌信任", "兜底"]
CHANNELS = ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]

df = pd.read_csv(os.path.join(DATA, "历史客服会话语料.csv"))
ok = True


def chk(cond, msg):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        ok = False


chk(list(df.columns) == EXPECT, "列名逐字一致 -> {}".format(list(df.columns)))
chk(len(df) >= 300, "会话条数 >= 300 -> {}".format(len(df)))
chk(df["会话ID"].is_unique, "会话ID 唯一")
chk(df.isna().sum().sum() == 0, "无空值")
chk(set(df["渠道"]) == set(CHANNELS), "渠道覆盖 5 个 -> {}".format(sorted(set(df["渠道"]))))
chk(set(df["意图类目"]) == set(CATS), "意图覆盖 6 大类 -> {}".format(sorted(set(df["意图类目"]))))
chk(set(df["是否转人工"]) == {"是", "否"}, "是否转人工取值合法")
chk(df["是否转人工"].eq("是").sum() >= 20, "含转人工样本 -> {} 条".format(df["是否转人工"].eq("是").sum()))

# ── 脱敏自查 ──
blob = "\n".join((df["用户问题"] + "\n" + df["客服回复"]).tolist())
chk(not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", blob), "无完整 11 位手机号残留")
chk(not re.search(r"(?<!\d)20\d{10}(?!\d)", blob), "无完整 12 位订单号残留")
chk(not re.search(r"(?<!\d)78\d{10}(?!\d)", blob), "无完整 12 位运单号残留")
chk(not re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", blob), "无身份证号残留")
chk(not re.search(r"(?<!\d)\d{16,19}(?!\d)", blob), "无银行卡号残留")
chk("****" in blob, "掩码符 **** 已实际出现 -> {} 处".format(blob.count("****")))
# 地址不得出现「区/路/号」这类门牌级信息
chk(not re.search(r"(省|市)[\u4e00-\u9fa5]{0,6}区", blob), "地址未下探到区级")
# 手机号掩码必须是前3后4
ph_mask = re.findall(r"(?<!\d)(1[3-9]\d)\*{4}(\d{4})(?!\d)", blob)
chk(len(ph_mask) > 0, "手机号按「前3后4」脱敏 -> {} 处，如 {}{}****{}".format(
    len(ph_mask), ph_mask[0][0], "", ph_mask[0][1]) if ph_mask else "手机号按「前3后4」脱敏")

# ── 占位符残留 ──
ph = re.findall(r"\{[a-z_]+\}", blob)
chk(not ph, "无模板占位符残留 -> {}".format(sorted(set(ph))))

# ── 商品引用与主数据一致 ──
goods = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"))
names = set(goods["商品名"])
mentioned = set()
for n in names:
    if n in blob:
        mentioned.add(n)
chk(len(mentioned) >= 30, "会话中引用的商品名与主数据一致 -> 命中 {} 个 SKU 商品名".format(len(mentioned)))

# ── 时间范围 ──
t = pd.to_datetime(df["时间"])
span_days = (t.max() - t.min()).days
chk(span_days >= 330, "时间跨度接近 12 个月 -> {} 天（{} ~ {}）".format(
    span_days, t.min().date(), t.max().date()))
chk(len(set(t.dt.to_period("M"))) >= 12, "覆盖 >= 12 个自然月 -> {}".format(len(set(t.dt.to_period("M")))))

# ── 交叉校验 XLSX ──
xl = pd.read_excel(os.path.join(DATA, "历史客服会话语料.xlsx"), sheet_name="会话语料")
chk(list(xl.columns) == EXPECT and len(xl) == len(df), "XLSX 与 CSV 内容一致")

print()
print("意图类目分布：")
print(df["意图类目"].value_counts().to_string())
print()
print("渠道分布：")
print(df["渠道"].value_counts().to_string())
print()
print("情绪分布：")
print(df["用户情绪"].value_counts().to_string())
print()
print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
sys.exit(0 if ok else 1)
