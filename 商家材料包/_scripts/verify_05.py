# -*- coding: utf-8 -*-
"""验收第 5 步产出：docs/接口对接说明书.md、docs/接口权限申请清单.md、
data/ERP供应商信息表.csv（+xlsx）。

对应商家资料索取清单 S-13 ~ S-18（第 3 批）。
"""
import os
import re
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(BASE, "docs")
DATA = os.path.join(BASE, "data")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("{}  {}  {}".format("PASS" if ok else "FAIL", name, "-> " + str(detail) if detail else ""))


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


spec = read(os.path.join(DOCS, "接口对接说明书.md"))
access = read(os.path.join(DOCS, "接口权限申请清单.md"))

# ────────── 说明书：6 个接口 ──────────
INTERFACES = {
    "订单查询": "/api/v1/order/query",
    "物流轨迹查询": "/api/v1/logistics/track",
    "库存查询": "/api/v1/inventory/query",
    "商品信息读取": "/api/v1/product/detail",
    "工单创建": "/api/v1/ticket/create",
    "平台客服消息收发": "/api/v1/message/send",
}
for name, path in INTERFACES.items():
    check("说明书含接口：{}".format(name), path in spec)

# ────────── 每个接口的 9 项必备内容 ──────────
# 用「### N.x」小节切分，逐个接口核对其小节内是否含必备项
sections = re.split(r"^## (\d+)\. ", spec, flags=re.M)
# sections: [前言, "1", 内容1, "2", 内容2, ...]
iface_sections = {}
for i in range(1, len(sections), 2):
    if sections[i] == "0":      # 第 0 章是「通用约定」，不是接口
        continue
    iface_sections[sections[i]] = sections[i + 1]

check("说明书含 6 个接口小节", len(iface_sections) == 6, sorted(iface_sections.keys()))

REQUIRED = {
    "接口地址": r"\| 接口地址 \|",
    "请求方式": r"\| 请求方式 \|",
    "鉴权方式": r"\| 鉴权方式 \|",
    "字段字典": r"字段字典",
    "请求示例": r"请求示例",
    "响应示例": r"响应示例",
    "错误码表": r"错误码",
    "限流阈值": None,   # 在通用章节
    "测试环境地址": r"open-test\.wankeai\.example\.com",
}
for num, body in sorted(iface_sections.items()):
    title = re.search(r"^(.+)$", body, re.M).group(1).strip()
    for item, pat in REQUIRED.items():
        if pat is None:
            continue
        check("接口 {}（{}）含{}".format(num, title, item), bool(re.search(pat, body)))

check("说明书含通用限流阈值表", "限流阈值" in spec and "QPS 上限" in spec)
check("说明书含测试环境地址", "open-test.wankeai.example.com" in spec)
check("说明书含生产环境地址", "open.wankeai.example.com" in spec)

# ────────── 字段命名与项目文档 3.4 节一致 ──────────
DOC34_FIELDS = ["order_no", "status", "items", "logistics", "sku_id", "tracking_no",
                "estimated_arrival", "session_id", "buyer_id", "reason", "summary",
                "entities", "tried_actions", "priority"]
for f in DOC34_FIELDS:
    check("字段命名与项目文档 3.4 节一致：{}".format(f), f in spec)

# 状态枚举一致性
for enum in ["CREATED", "PAID", "SHIPPED", "DELIVERED", "CLOSED"]:
    check("订单状态枚举含 {}".format(enum), enum in spec)
for enum in ["USER_REQUEST", "EMOTION", "UNRESOLVED", "HIGH_RISK", "SYSTEM_ERROR"]:
    check("转人工原因枚举含 {}".format(enum), enum in spec)

# ────────── 说明书通用章节 ──────────
for kw in ["通用约定", "鉴权方式", "通用错误码", "环境地址", "字段命名映射表", "联调检查清单"]:
    check("说明书含章节：{}".format(kw), kw in spec)

# 错误码覆盖
for code in ["40001", "40101", "40102", "40301", "40401", "42901", "50001", "50301", "50401"]:
    check("说明书含错误码 {}".format(code), code in spec)

# 脱敏要求
check("说明书含脱敏强制要求",
      "138****8888" in spec or "保留前 3 后 4 位" in spec)

# ────────── 权限清单：7 项权限 ──────────
PERMS = [
    "订单查询（只读）",
    "物流轨迹查询",
    "库存查询（只读）",
    "商品信息读取（只读）",
    "工单创建（写入）",
    "平台客服消息收发",
    "测试环境访问",
]
for p in PERMS:
    check("权限清单含：{}".format(p), p in access)

# 7 项权限每项都要有的四要素
for item in ["用途", "期望形式", "前置条件", "不可提供"]:
    n = len(re.findall(r"\*\*" + item + r"\*\*", access))
    check("权限清单每项含「{}」（7 处）".format(item), n >= 7, n)

# 限流与鉴权只对 6 个接口类权限有意义（第 7 项是测试环境访问，用「访问方式」）
for item in ["限流", "鉴权方式"]:
    n = len(re.findall(r"\*\*" + item + r"\*\*", access))
    check("权限清单含「{}」（6 个接口类权限）".format(item), n >= 6, n)

# 五渠道
for ch in ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]:
    check("权限清单含渠道：{}".format(ch), ch in access)

# 测试数据
check("权限清单含测试订单样本", "测试订单样本" in access and "202601010001" in access)
check("权限清单含测试 SKU 样本", "测试 SKU 样本" in access and "WK-BEAR-20" in access)
check("权限清单含交付方式说明（加密压缩包）", "加密压缩包" in access)
check("权限清单含风险提示", "风险提示" in access)

# ────────── ERP 供应商表 ──────────
erp_path = os.path.join(DATA, "ERP供应商信息表.csv")
check("ERP供应商信息表.csv 存在", os.path.exists(erp_path))
erp = pd.read_csv(erp_path, encoding="utf-8-sig", dtype=str).fillna("")
check("ERP表列名正确",
      list(erp.columns) == ["系统名称", "系统代号", "供应商", "供应商类型", "对接窗口人",
                            "职务", "联系方式", "负责范围", "对接接口", "SLA承诺",
                            "故障响应", "备注"],
      list(erp.columns))
check("ERP表行数 >= 5", len(erp) >= 5, len(erp))
check("ERP表无空值", (erp == "").sum().sum() == 0, (erp == "").sum().sum())
check("ERP表含 OMS 与 ERP 系统",
      any("OMS" in x for x in erp["系统代号"]) and any("ERP" in x for x in erp["系统代号"]))
check("ERP表联系方式已脱敏处理",
      (erp["联系方式"] == "走安全渠道提供").all(), set(erp["联系方式"]))

xlsx_erp = os.path.join(DATA, "ERP供应商信息表.xlsx")
check("ERP供应商信息表.xlsx 存在", os.path.exists(xlsx_erp))
if os.path.exists(xlsx_erp):
    xe = pd.read_excel(xlsx_erp, sheet_name="ERP供应商信息", dtype=str).fillna("")
    check("ERP xlsx 与 csv 一致", xe.shape == erp.shape, "{} vs {}".format(xe.shape, erp.shape))

# ────────── 交叉引用 ──────────
check("权限清单引用说明书", "接口对接说明书.md" in access)
check("说明书引用商品主数据表", "商品主数据表.csv" in spec)
check("权限清单引用 ERP 供应商表", "ERP供应商信息表.csv" in access)

# ────────── 汇总 ──────────
print()
failed = [r for r in results if not r[1]]
print("RESULT:", "ALL PASS" if not failed else "{} 项 FAIL".format(len(failed)))
sys.exit(0 if not failed else 1)
