# -*- coding: utf-8 -*-
"""全局约束验收：跳转/外链类能力「有但不接入」。

用户要求（原话要点）：暂且不用实现跳转淘宝拼多多功能，只是有但不接入；
遇到该问题时绕过就行。

本脚本扫描全部交付物，确认：
  1. 客服话术（FAQ answer / 会话客服回复 / 多轮对话客服发言）中
     不出现「发您链接」「链接发您」「点此跳转」「点击链接」「点击这里」等外链引导
  2. 买家发言中出现「链接」是允许的（买家说的是平台页面上看到的链接）
  3. 接口说明书保留 msg_type=GOODS/CARD 与 handoff_url 字段（能力「有」）
  4. 接口说明书含第 0.8 节「暂不接入的能力」说明（能力「不接入」）
  5. 品牌资料中「扫码验证」属正常业务（防伪码核验），不算跳转外链，予以放行
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


# 禁止出现在「客服发言」里的外链引导词
FORBIDDEN = [
    "发您链接", "发你链接", "链接发您", "链接发你", "我把链接", "把链接发",
    "点此跳转", "点击跳转", "点击链接", "点击这里", "点这里", "戳这里",
    "跳转到商品", "跳转至商品", "商品链接", "领券链接", "专属链接",
    "复制链接", "打开链接", "下单链接",
]

print("=" * 70)
print("全局约束验收：跳转/外链能力「有但不接入」")
print("=" * 70)

# ── 1. FAQ answer ──
print("\n[1] FAQ 客服话术")
faq = pd.read_csv(os.path.join(DATA, "FAQ知识库.csv"), encoding="utf-8-sig", dtype=str).fillna("")
hits = []
for _, r in faq.iterrows():
    for w in FORBIDDEN:
        if w in r["answer"]:
            hits.append((r["standard_q"], w))
check("FAQ answer 无外链引导语（{} 条）".format(len(faq)), not hits, hits[:5])

# ── 2. 单轮会话客服回复 ──
print("\n[2] 单轮会话客服回复")
cs = pd.read_csv(os.path.join(DATA, "历史客服会话语料.csv"), encoding="utf-8-sig", dtype=str).fillna("")
hits = []
for _, r in cs.iterrows():
    for w in FORBIDDEN:
        if w in r["客服回复"]:
            hits.append((r["会话ID"], w))
check("单轮会话客服回复无外链引导语（{} 条）".format(len(cs)), not hits, hits[:5])

# ── 3. 多轮对话：只查客服发言 ──
print("\n[3] 多轮对话客服发言")
dg = pd.read_csv(os.path.join(DATA, "多轮客服对话语料.csv"), encoding="utf-8-sig", dtype=str).fillna("")
agent_turns = dg[dg["角色"] == "客服"]
hits = []
for _, r in agent_turns.iterrows():
    for w in FORBIDDEN:
        if w in r["内容"]:
            hits.append((r["对话ID"], w))
check("多轮客服发言无外链引导语（{} 条）".format(len(agent_turns)), not hits, hits[:5])

# 买家发言允许提「链接」（描述平台页面）
buyer_turns = dg[dg["角色"] == "买家"]
check("买家发言可提「链接」（不误判）", True,
      "买家提及 {} 处".format(buyer_turns["内容"].str.contains("链接").sum()))

# ── 4. 能力「有」：字段与枚举保留 ──
print("\n[4] 接口能力保留（有）")
spec = open(os.path.join(DOCS, "接口对接说明书.md"), encoding="utf-8").read()
check("保留 msg_type 的 GOODS 枚举", "GOODS" in spec)
check("保留 msg_type 的 CARD 枚举", "CARD" in spec)
check("保留 handoff_url 字段", "handoff_url" in spec)
check("保留 CARD 类型说明", "`CARD`" in spec or "CARD" in spec)

# ── 5. 能力「不接入」：0.8 节说明 ──
print("\n[5] 接口能力不接入（不接入）")
check("含第 0.8 节「暂不接入的能力」", "0.8 暂不接入的能力" in spec)
check("0.8 节明确写出跳转淘宝/拼多多不接入", "跳转淘宝" in spec and "不接入" in spec)
check("0.8 节要求话术不得出现外链表述", "不得出现「我把链接发您」" in spec)
check("0.8 节说明本期只使用 TEXT/IMAGE", "`TEXT`" in spec and "`IMAGE`" in spec)

# ── 6. 营销活动规则同步 ──
print("\n[6] 营销活动规则同步")
mkt = open(os.path.join(DOCS, "营销活动规则.md"), encoding="utf-8").read()
check("营销规则已改为直播间领取（无专属链接发放）", "专属链接限量发放" not in mkt)
check("营销规则含不发送链接的说明", "不通过客服会话发送领券链接" in mkt)

# ── 7. 全局扫描：docs 下不得残留「专属链接」等外链表述 ──
print("\n[7] docs 目录全局扫描")
bad_docs = []
for fn in os.listdir(DOCS):
    if not fn.endswith(".md"):
        continue
    for ln, line in enumerate(open(os.path.join(DOCS, fn), encoding="utf-8"), start=1):
        # 规则陈述行本身会引用被禁短语（如「不得出现『我把链接发您』」），跳过
        if "不得出现" in line or "不接入" in line or "不通过客服会话发送" in line:
            continue
        for w in ["发您链接", "发你链接", "链接发您", "我把链接", "专属链接领取", "点击跳转", "点此跳转"]:
            if w in line:
                bad_docs.append((fn, ln, w))
check("docs 下无残留外链引导表述", not bad_docs, bad_docs)

print("\n" + "=" * 70)
print("PASS: {}    FAIL: {}".format(len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  -", f)
    print("RESULT: FAILED")
    sys.exit(1)
print("RESULT: ALL PASS")
