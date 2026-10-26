# -*- coding: utf-8 -*-
"""生成 data/ERP供应商信息表.csv + .xlsx —— 对应商家资料索取清单 S-18（ERP / 中台供应商信息）。

联系方式按脱敏要求不写入文件，统一标注「走安全渠道提供」。
"""
import os

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

HEADERS = ["系统名称", "系统代号", "供应商", "供应商类型", "对接窗口人", "职务",
           "联系方式", "负责范围", "对接接口", "SLA承诺", "故障响应", "备注"]

ROWS = [
    ["订单中台 OMS", "OMS", "杭州云链信息技术有限公司", "外部供应商", "周航", "对接经理",
     "走安全渠道提供", "订单查询、订单状态流转、工单落库", "order/query, ticket/create",
     "可用性 99.9%，查询 P99 ≤ 300ms", "P0 30 分钟内响应", "订单状态与平台同步延迟 ≤ 1 分钟"],
    ["库存 ERP", "ERP", "江苏智仓软件股份有限公司", "外部供应商", "吴磊", "技术负责人",
     "走安全渠道提供", "库存实时查询、SKU 属性同步、预售排产", "inventory/query, product/detail",
     "可用性 99.5%，库存刷新 ≤ 15 分钟", "P0 1 小时内响应", "只读视图为 15 分钟延迟，实时性要求高走接口"],
    ["商品中心 PIM", "PIM", "本店自研", "自研系统", "陈涛", "本店 IT 主管",
     "走安全渠道提供", "商品属性维护、类目体系、商品主数据", "product/detail",
     "可用性 99.5%", "工作时间 2 小时内响应", "商品主数据表（S-01/S-11）由此系统导出"],
    ["物流聚合平台", "LOGISTICS", "快递鸟（第三方 SaaS）", "外部 SaaS", "刘倩", "客服经理",
     "走安全渠道提供", "快递轨迹聚合查询（中通/圆通/顺丰）", "logistics/track",
     "可用性 99.0%，轨迹刷新 ≤ 2 小时", "P0 1 小时内响应", "备选通道为快递 100，双通道互备"],
    ["客服工单系统", "TICKET", "本店自研", "自研系统", "陈涛", "本店 IT 主管",
     "走安全渠道提供", "工单创建、坐席派发、客服工作台", "ticket/create",
     "可用性 99.5%", "工作时间 1 小时内响应", "工单号格式 TS{yyyyMMdd}{4位序号}"],
    ["渠道开放平台对接", "CHANNEL", "各平台官方（淘宝/拼多多/抖店/企业微信）", "平台官方",
     "陈涛", "本店 IT 主管", "走安全渠道提供",
     "五渠道客服消息收发授权与回调", "message/send, message/callback",
     "按平台 SLA", "按平台工单时效", "抖音小店需服务商认证，审核周期 10 天"],
]

df = pd.DataFrame(ROWS, columns=HEADERS)

csv_path = os.path.join(DATA, "ERP供应商信息表.csv")
df.to_csv(csv_path, index=False, encoding="utf-8-sig")

xlsx_path = os.path.join(DATA, "ERP供应商信息表.xlsx")
with pd.ExcelWriter(xlsx_path, engine="openpyxl") as w:
    df.to_excel(w, sheet_name="ERP供应商信息", index=False)

print("已生成:", csv_path)
print("已生成:", xlsx_path)
print("行数:", len(df), "列数:", len(df.columns))
