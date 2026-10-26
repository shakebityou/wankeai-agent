# -*- coding: utf-8 -*-
"""验收：多轮客服对话语料（第 2 步增强）。

检查项：
  1. CSV / XLSX 存在，列名逐字一致
  2. 对话段数 == 17，逐轮记录 >= 160
  3. 每段回合数 >= 4（真实多轮）
  4. 6 大意图类目全覆盖、5 渠道全覆盖
  5. 脱敏：无 11 位手机号、无完整 12 位订单号、无运单号、无身份证、无银行卡
  6. 掩码符 **** 实际出现
  7. 引用的 SKU 编码全部存在于商品主数据
  8. 角色只有「买家」「客服」，且每段买家/客服条数相等
  9. XLSX 与 CSV 一致
 10. 客服话术不得出现口径外的编造承诺（检查关键词）
"""
import os
import re
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(detail)) if detail else ""))


print("=" * 70)
print("验收：多轮客服对话语料")
print("=" * 70)

csv_p = os.path.join(DATA, "多轮客服对话语料.csv")
xlsx_p = os.path.join(DATA, "多轮客服对话语料.xlsx")
check("CSV 存在", os.path.exists(csv_p))
check("XLSX 存在", os.path.exists(xlsx_p))

df = pd.read_csv(csv_p, encoding="utf-8-sig", dtype=str).fillna("")

EXPECT = ["对话ID", "话题", "意图类目", "渠道", "轮次", "角色", "内容",
          "是否转人工", "用户情绪", "解决状态", "涉及SKU"]
check("列名逐字一致", list(df.columns) == EXPECT, list(df.columns))

n_dlg = df["对话ID"].nunique()
check("对话段数 == 17", n_dlg == 17, n_dlg)
check("逐轮记录 >= 160", df.shape[0] >= 160, df.shape[0])
check("无空值", not (df == "").any().any())

# 回合数
rounds = df.groupby("对话ID")["轮次"].max().astype(int)
check("每段回合数 >= 4", (rounds >= 4).all(), rounds.to_dict())
check("回合数合计 >= 70", rounds.sum() >= 70, int(rounds.sum()))

# 意图与渠道
INTENTS = {"商品咨询", "订单服务", "售后服务", "营销活动", "品牌信任", "兜底"}
CHANNELS = {"淘宝", "天猫", "拼多多", "抖音小店", "微信客服"}
got_i = set(df["意图类目"].unique())
got_c = set(df["渠道"].unique())
check("意图覆盖 6 大类", INTENTS.issubset(got_i), sorted(got_i))
check("渠道覆盖 5 个", CHANNELS.issubset(got_c), sorted(got_c))

# 角色
check("角色仅买家/客服", set(df["角色"].unique()) == {"买家", "客服"}, sorted(df["角色"].unique()))
pair_ok = True
for cid, g in df.groupby("对话ID"):
    if (g["角色"] == "买家").sum() != (g["角色"] == "客服").sum():
        pair_ok = False
check("每段买家与客服条数相等", pair_ok)

# 转人工
check("含转人工样本", (df["是否转人工"] == "是").any())
check("是否转人工取值合法", set(df["是否转人工"].unique()) <= {"是", "否"})

# 情绪与状态
check("情绪取值合法", set(df["用户情绪"].unique()) <= {"中性", "满意", "不满", "愤怒"},
      sorted(df["用户情绪"].unique()))
check("解决状态取值合法", set(df["解决状态"].unique()) <= {"已解决", "未解决", "转人工"},
      sorted(df["解决状态"].unique()))

# ── 脱敏 ──
print("\n[脱敏]")
blob = "\n".join(df["内容"].tolist())
check("无完整 11 位手机号残留", not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", blob))
check("无完整 12 位订单号残留", not re.search(r"(?<!\d)20\d{10}(?!\d)", blob))
check("无完整 12 位运单号残留", not re.search(r"(?<!\d)78\d{10}(?!\d)", blob))
check("无身份证号残留", not re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", blob))
check("无银行卡号残留", not re.search(r"(?<!\d)\d{16,19}(?!\d)", blob))
check("掩码符 **** 实际出现", "****" in blob, blob.count("****"))
# 手机号前3后4
m_phone = re.findall(r"1[3-9]\d\*{4}\d{4}", blob)
check("手机号按前3后4脱敏", len(m_phone) > 0, len(m_phone))
# 订单号前4后4
m_order = re.findall(r"20\d{2}\*{4}\d{4}", blob)
check("订单号按前4后4脱敏", len(m_order) > 0, len(m_order))
# 地址不下探到区级
check("地址未下探到区级", not re.search(r"(省|市)[\u4e00-\u9fa5]{0,6}区(?!\*)", blob))
# 姓名
m_name = re.findall(r"[\u4e00-\u9fa5]\*\*", blob)
check("姓名按保留姓氏脱敏", len(m_name) > 0, len(m_name))

# ── SKU 一致性 ──
print("\n[SKU 一致性]")
sku = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"), encoding="utf-8-sig", dtype=str)
valid = set(sku["SKU编码"])
used = set()
for s in df["涉及SKU"]:
    for x in s.split("|"):
        if x.strip():
            used.add(x.strip())
check("引用的 SKU 全部存在于主数据", used.issubset(valid), sorted(used - valid))
check("引用 SKU 数 >= 10", len(used) >= 10, len(used))

# 正文中出现的 SKU 编码也须合法
in_text = set(re.findall(r"WK-[A-Z]+-?[A-Z0-9]*", blob))
in_text = {x for x in in_text if x}
bad_txt = {x for x in in_text if x not in valid}
check("正文中 SKU 编码全部合法", not bad_txt, sorted(bad_txt)[:8])

# ── 口径合规 ──
print("\n[口径合规]")
BAD_PROMISE = ["赔偿你", "赔付您", "给你赔偿", "赔你", "包退包换", "无理由包邮", "15天无理由", "30天无理由", "终身质保"]
hits = [b for b in BAD_PROMISE if b in blob]
check("客服话术无口径外承诺", not hits, hits)
check("未出现身份证号/银行卡号字样泄漏", "身份证号" not in blob or "[已删除]" in blob)

# ── XLSX 一致 ──
print("\n[XLSX 一致性]")
xl = pd.ExcelFile(xlsx_p)
check("XLSX Sheet 含 对话语料/对话汇总",
      {"对话语料", "对话汇总"}.issubset(set(xl.sheet_names)), xl.sheet_names)
x1 = pd.read_excel(xlsx_p, sheet_name="对话语料", dtype=str).fillna("")
check("XLSX 对话语料与 CSV 形状一致", x1.shape == df.shape, "{} vs {}".format(x1.shape, df.shape))
check("XLSX 列名与 CSV 一致", list(x1.columns) == list(df.columns))

print("\n" + "=" * 70)
print("PASS: {}    FAIL: {}".format(len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  -", f)
    print("RESULT: FAILED")
    sys.exit(1)
print("RESULT: ALL PASS")
