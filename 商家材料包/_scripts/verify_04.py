# -*- coding: utf-8 -*-
"""验收第 4 步产出：docs/ 下四份政策与 SOP 文档。

对应商家资料索取清单 S-05（售后政策）、S-07（物流发货SOP）、
S-08（营销活动规则）、S-09（品牌与IP资料）。

核心检查：第 3 步 FAQ 的 authority 字段引用的每一个条款编号，
必须能在对应文档里找到；反之文档条款也应能被引用。
"""
import os
import re
import sys
from collections import Counter

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(BASE, "docs")
DATA = os.path.join(BASE, "data")

DOC_FILES = {
    "售后政策": "售后政策.md",
    "物流发货SOP": "物流发货SOP.md",
    "营销活动规则": "营销活动规则.md",
    "品牌与IP资料": "品牌与IP资料.md",
}

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("{}  {}  {}".format("PASS" if ok else "FAIL", name, "-> " + str(detail) if detail else ""))


# ────────── 读取文档 ──────────
texts = {}
for doc, fn in DOC_FILES.items():
    p = os.path.join(DOCS, fn)
    if not os.path.exists(p):
        print("缺少文件:", p)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        texts[doc] = f.read()

# ────────── 四要素检查 ──────────
for doc, t in texts.items():
    check("{} 含文档编号".format(doc), bool(re.search(r"文档编号", t)))
    check("{} 含生效日期".format(doc), bool(re.search(r"生效日期\*\*：\s*\d{4}-\d{2}-\d{2}", t)))
    check("{} 含适用渠道".format(doc), bool(re.search(r"适用渠道\*\*：", t)))
    check("{} 含例外情形章节".format(doc), bool(re.search(r"例外情形", t)))
    check("{} 含条款索引表".format(doc), bool(re.search(r"条款索引", t)))
    # 条款编号形如 ### 3.2 标题
    secs = re.findall(r"^### (\d+\.\d+) ", t, re.M)
    check("{} 条款编号 >= 10 个".format(doc), len(secs) >= 10, len(secs))

# ────────── 逐项验收（来自 TODO note 的硬要求）──────────
af = texts["售后政策"]
check("售后政策写明 7 天无理由边界",
      bool(re.search(r"7 个自然日|7 个自然日内", af)) and "3.1" in af)
check("售后政策写明运费承担规则", "运费承担规则" in af)
check("售后政策写明瑕疵认定标准",
      bool(re.search(r"A 级", af)) and bool(re.search(r"B 级", af)) and bool(re.search(r"C 级", af)))
check("售后政策写明退款时效", bool(re.search(r"1–3 个工作日|1-3 个工作日", af)))

lg = texts["物流发货SOP"]
check("物流SOP写明现货发货时限", bool(re.search(r"48 小时内", lg)))
check("物流SOP写明预售发货时限",
      bool(re.search(r"预售\(7天\)|预售\(7 天\)", lg)) and "30 个自然日" in lg)
check("物流SOP写明合作快递",
      all(k in lg for k in ["顺丰", "中通", "圆通"]))
check("物流SOP写明偏远地区", "偏远地区" in lg and "新疆" in lg)
check("物流SOP写明节假日安排", "春节" in lg and "国庆" in lg)
check("物流SOP写明丢件流程", "丢件流程" in lg)
check("物流SOP写明破损流程", "破损流程" in lg)
check("物流SOP写明签收未收到", "签收未收到" in lg)

mk = texts["营销活动规则"]
check("营销规则含优惠券规则", "优惠券类型" in mk)
check("营销规则含满减规则", "满减档位" in mk)
check("营销规则含赠品规则", "赠品发放" in mk)
check("营销规则含会员权益", "会员等级与权益" in mk)
check("营销规则含积分规则", "积分规则" in mk)

br = texts["品牌与IP资料"]
check("品牌资料含 GB 6675 玩具安全",
      "GB 6675" in br and "GB 6675.1" in br and "GB 6675.4" in br)
check("品牌资料含正品鉴别方式", "正品鉴别方式" in br)
check("品牌资料含防伪码", "防伪码" in br)
check("品牌资料含 IP 授权范围", "IP 授权范围" in br)
check("品牌资料含假一赔十", "假一赔十" in br)

# ────────── 核心：FAQ 引用的条款编号是否全部存在 ──────────
faq = pd.read_csv(os.path.join(DATA, "FAQ知识库.csv"), encoding="utf-8-sig", dtype=str).fillna("")
refs = Counter()
for a in faq["authority"]:
    for doc, num in re.findall(r"(售后政策|物流发货SOP|营销活动规则|品牌与IP资料) (\d+\.\d+) 条", a):
        refs[(doc, num)] += 1

missing = []
for (doc, num), _ in sorted(refs.items()):
    if not re.search(r"^### " + re.escape(num) + r" ", texts[doc], re.M):
        missing.append("{} {}".format(doc, num))
check("FAQ 引用的条款全部存在于文档中", len(missing) == 0, missing)

print()
print("FAQ 引用条款核对明细：")
for doc in DOC_FILES:
    items = sorted([n for (d, n) in refs if d == doc],
                   key=lambda x: tuple(int(p) for p in x.split(".")))
    print("  {}: {} 个 -> {}".format(doc, len(items), ", ".join(items)))
print("  合计: {} 个被引用条款".format(len(refs)))

# 反向：文档里的条款是否都能被 FAQ 引用（不强制，仅统计）
print()
print("文档条款总数 vs 被引用数：")
for doc, t in texts.items():
    secs = set(re.findall(r"^### (\d+\.\d+) ", t, re.M))
    used = set(n for (d, n) in refs if d == doc)
    print("  {}: 文档 {} 条，被 FAQ 引用 {} 条，未引用 {}".format(
        doc, len(secs), len(used), len(secs - used)))

# ────────── 汇总 ──────────
print()
failed = [r for r in results if not r[1]]
print("RESULT:", "ALL PASS" if not failed else "{} 项 FAIL".format(len(failed)))
sys.exit(0 if not failed else 1)
