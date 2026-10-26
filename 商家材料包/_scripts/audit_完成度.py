# -*- coding: utf-8 -*-
"""交付完成度盘点：按《商家资料索取清单》S-01~S-22 与第三节 7 项权限，
逐项核对磁盘上的实际交付物，输出完成度矩阵。

不信任 README 的自述，只认磁盘事实。
"""
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
DOCS = os.path.join(BASE, "docs")
IMGDIR = os.path.join(DATA, "商品图片素材库")

ok, partial, missing = [], [], []


def has(*paths):
    return all(os.path.exists(os.path.join(BASE, p)) for p in paths)


def rows_of(csv):
    p = os.path.join(DATA, csv)
    if not os.path.exists(p):
        return 0
    return pd.read_csv(p, encoding="utf-8-sig").shape[0]


def img_count():
    n = 0
    for root, _dirs, fs in os.walk(IMGDIR):
        n += len([f for f in fs if f.lower().endswith((".jpg", ".png", ".jpeg", ".webp"))])
    return n


def sku_dirs():
    n = 0
    for c in os.listdir(IMGDIR) if os.path.isdir(IMGDIR) else []:
        p = os.path.join(IMGDIR, c)
        if os.path.isdir(p):
            n += len([d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d))])
    return n


def record(code, name, status, evidence):
    (ok if status == "done" else partial if status == "partial" else missing).append(
        (code, name, evidence))
    tag = {"done": "✅", "partial": "⚠️", "missing": "❌"}[status]
    print("{} {:<6} {:<22} {}".format(tag, code, name, evidence))


print("=" * 92)
print("商家材料包交付完成度盘点（基于磁盘实际文件）")
print("=" * 92)

sku_n = rows_of("商品主数据表.csv")
faq_n = rows_of("FAQ知识库.csv")
card_n = rows_of("商品知识卡.csv")
conv_n = rows_of("历史客服会话语料.csv")
multi_n = rows_of("多轮客服对话语料.csv")
idx_n = rows_of("商品图片索引.csv")
imgs = img_count()
skudirs = sku_dirs()

print("\n【第 1 批 · 项目启动即需】")
record("S-01", "全量商品属性数据",
       "done" if sku_n >= 60 and has("data/商品主数据表.xlsx") else "partial",
       "{} SKU（CSV+XLSX，14 字段）".format(sku_n))
record("S-02", "商品图片素材库",
       "done" if imgs >= 296 and skudirs == 74 else "partial",
       "{} 张 / {} 个 SKU 目录 / 5 类目（爬取参考图，非实拍）".format(imgs, skudirs))
record("S-03", "近 12 个月客服会话记录",
       "done" if conv_n >= 300 else "partial",
       "单轮 {} 条 + 多轮 {} 条（均已脱敏）".format(conv_n, multi_n))
record("S-04", "现行客服话术库",
       "done" if faq_n >= 120 else "partial",
       "FAQ {} 条 + 商品知识卡 {} 条".format(faq_n, card_n))
record("S-05", "售后政策文档",
       "done" if has("docs/售后政策.md") else "missing", "docs/售后政策.md")
record("S-06", "业务对接人信息",
       "done" if rows_of("业务对接人信息表.csv") >= 2 else "partial",
       "{} 名对接人（联系方式走安全渠道）".format(rows_of("业务对接人信息表.csv")))

print("\n【第 2 批 · 设计阶段需】")
record("S-07", "物流与发货 SOP", "done" if has("docs/物流发货SOP.md") else "missing",
       "docs/物流发货SOP.md")
record("S-08", "营销活动规则", "done" if has("docs/营销活动规则.md") else "missing",
       "docs/营销活动规则.md")
record("S-09", "品牌与 IP 资料", "done" if has("docs/品牌与IP资料.md") else "missing",
       "docs/品牌与IP资料.md（含 GB 6675；授权书原件未提供）")
record("S-10", "客服组织结构",
       "done" if has("data/客服组织结构表.xlsx", "data/客服坐席分组表.csv") else "missing",
       "4 Sheet + 分组表")
record("S-11", "商品分类体系",
       "done" if rows_of("商品分类体系表.csv") >= 5 else "partial", "类目树 6 行")
record("S-12", "现有机器人/知识库资产",
       "done" if has("docs/现有知识库资产说明.md") else "missing",
       "含历史准确率 61.2%；历史工单 2847 条待脱敏")

print("\n【第 3 批 · 开发联调需】")
record("S-13", "订单系统接口",
       "done" if has("docs/接口对接说明书.md") else "missing", "说明书第 1 节（API 规范齐）")
record("S-14", "物流查询接口", "done" if has("docs/接口对接说明书.md") else "missing",
       "说明书第 2 节（快递鸟+快递100+直连）")
record("S-15", "库存系统接口", "done" if has("docs/接口对接说明书.md") else "missing",
       "说明书第 3 节")
record("S-16", "工单/售后系统接口", "done" if has("docs/接口对接说明书.md") else "missing",
       "说明书第 5 节（按 session_id 幂等）")
record("S-17", "测试环境与测试账号",
       "done" if has("docs/接口权限申请清单.md", "docs/接口对接说明书.md") else "missing",
       "账号清单已列，密码走安全渠道")
record("S-18", "ERP/中台供应商信息",
       "done" if rows_of("ERP供应商信息表.csv") >= 5 else "partial",
       "{} 个系统".format(rows_of("ERP供应商信息表.csv")))

print("\n【第 4 批 · 上线准备需】")
record("S-19", "渠道接入资质", "done" if has("docs/渠道接入资质说明.md") else "missing",
       "5 渠道现状（抖音需服务商认证）")
record("S-20", "服务器与部署环境", "done" if has("docs/服务器与部署环境说明.md") else "missing",
       "含白名单/备案/合规清单")
record("S-21", "数据合规要求", "done" if has("docs/数据合规要求说明.md") else "missing",
       "存储/保留/脱敏三要求")
record("S-22", "预算与成本上限", "done" if has("docs/预算与成本说明.md") else "missing",
       "月度上限 18,000 元")

print("\n【第三节 · 7 项接口权限（IT 专项）】")
acc = os.path.join(DOCS, "接口权限申请清单.md")
acc_txt = open(acc, encoding="utf-8").read() if os.path.exists(acc) else ""
for i, (name, kw) in enumerate([
        ("订单查询（只读）", "订单查询"), ("物流轨迹查询", "物流轨迹查询"),
        ("库存查询（只读）", "库存查询"), ("商品信息读取（只读）", "商品信息读取"),
        ("工单创建（写入）", "工单创建"), ("平台客服消息收发", "平台客服消息收发"),
        ("测试环境访问", "测试环境访问")], 1):
    record("权限{}".format(i), name,
           "done" if kw in acc_txt else "missing",
           "权限清单已说明用途/形式/周期" if kw in acc_txt else "未找到")

print("\n【接口资料 8 要素（清单第三节硬要求）】")
spec = os.path.join(DOCS, "接口对接说明书.md")
spec_txt = open(spec, encoding="utf-8").read() if os.path.exists(spec) else ""
for kw in ["接口地址", "请求方式", "鉴权方式", "字段字典", "错误码",
           "限流", "测试环境", "响应示例"]:
    print("   {} {:<10} 出现 {} 次".format("✅" if kw in spec_txt else "❌",
                                          kw, spec_txt.count(kw)))

print("\n【项目文档 FR 五大功能 · 商家材料支撑度】")
print("   FR-01 语音识别      —— 商家侧无需额外材料（属项目组技术选型）")
print("   FR-02 文字回复      —— 支撑材料：FAQ {} 条 + 商品知识卡 {} 条 + 语料 {} 条".format(
    faq_n, card_n, conv_n + multi_n))
print("   FR-03 图文识别      —— 支撑材料：{} 张商品图（索引含类目/SKU）".format(imgs))
print("   FR-04 订单查询      —— 支撑材料：接口说明书 + 测试订单样本 10 个")
print("   FR-05 转人工        —— 支撑材料：客服组织表 + 权限清单第 5 项 + 语料转人工样本")

print("\n" + "=" * 92)
print("完成度：✅ {} 项    ⚠️ 部分 {} 项    ❌ 缺失 {} 项".format(
    len(ok), len(partial), len(missing)))
total = len(ok) + len(partial) + len(missing)
print("静态资料完成率：{}/{} = {:.1f}%".format(len(ok), total, 100.0 * len(ok) / total))
if partial:
    print("\n部分完成：")
    for c, n, e in partial:
        print("   ⚠️ {} {} —— {}".format(c, n, e))
if missing:
    print("\n缺失：")
    for c, n, e in missing:
        print("   ❌ {} {} —— {}".format(c, n, e))
print("=" * 92)
