# -*- coding: utf-8 -*-
"""生成第 6 步的表格类资料：
  - data/业务对接人信息表.xlsx + .csv      （S-06）
  - data/客服组织结构表.xlsx               （S-10：坐席分组 / 在线时段 / 排班表 / 峰值人力）
  - data/商品分类体系表.xlsx + .csv        （S-11：内部类目树，用于知识分类对齐）

注意（教训）：openpyxl 写表必须「先表头后数据」，且不能跨函数分两步写。
本脚本用 pandas 落数据 + openpyxl 事后美化，天然满足该约束。
"""
import os

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

HEAD_FILL = PatternFill("solid", fgColor="C97B84")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=11)


def style_workbook(path, widths_map):
    """按 sheet 名给表头加样式、冻结首行、开自动筛选。"""
    wb = load_workbook(path)
    for ws in wb.worksheets:
        for c in ws[1]:
            c.fill = HEAD_FILL
            c.font = HEAD_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = widths_map.get(ws.title)
        if widths:
            for i, w in enumerate(widths, start=1):
                ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 26
    wb.save(path)


# ══════════════ S-06 业务对接人信息 ══════════════
CONTACT_HEADERS = ["序号", "角色", "姓名", "职务", "所属部门", "联系方式", "备用联系方式",
                   "职责范围", "可对接事项", "响应时段", "SLA"]
CONTACTS = [
    [1, "业务负责人（拍板人）", "陈静", "运营主管", "运营中心", "走安全渠道提供", "走安全渠道提供",
     "对本项目业务口径、活动规则、话术标准拥有最终解释权与拍板权",
     "需求澄清、业务规则确认、话术终审、上线验收签字",
     "工作日 09:00–18:00", "4 小时内响应"],
    [2, "技术接口人", "陈涛", "IT 主管", "IT 部", "走安全渠道提供", "走安全渠道提供",
     "负责全部接口权限开通、账号发放、网络白名单、环境部署与联调排障",
     "接口对接、Token 签发、VPN/测试环境、域名备案、故障排查",
     "工作日 09:00–18:00；故障 7×24", "P0 30 分钟内响应"],
    [3, "商品数据对接人", "王涛", "商品运营", "商品中心", "走安全渠道提供", "走安全渠道提供",
     "负责商品主数据、类目体系、上新与下架同步",
     "商品属性澄清、类目树对齐、知识卡核对、上新通知",
     "工作日 09:30–18:30", "1 个工作日内响应"],
    [4, "客服话术对接人", "李敏", "客服主管", "客服中心", "走安全渠道提供", "走安全渠道提供",
     "负责 FAQ 话术、售后口径、转人工规则与坐席排班",
     "话术审核、FAQ 终审、转人工策略、坐席资源协调",
     "每日 09:00–21:00", "2 小时内响应"],
    [5, "仓储物流对接人", "赵鹏", "仓储物流经理", "仓储中心", "走安全渠道提供", "走安全渠道提供",
     "负责发货时效、快递合作、丢件破损处理与偏远地区规则",
     "物流时效确认、丢件破损流程、快递账号、异常件处理",
     "工作日 08:30–18:00", "2 小时内响应"],
    [6, "品牌合规对接人", "周琳", "品牌经理", "品牌部", "走安全渠道提供", "走安全渠道提供",
     "负责 IP 授权、质检报告、正品鉴别口径与宣传合规",
     "IP 授权说明、质检报告、合规审查、宣传物料审核",
     "工作日 10:00–18:00", "1 个工作日内响应"],
    [7, "数据合规对接人", "孙杰", "法务专员", "法务部", "走安全渠道提供", "走安全渠道提供",
     "负责数据用途审核、脱敏标准、保密协议与数据删除确认",
     "数据安全承诺书签署、合规审查、数据删除确认函",
     "工作日 09:00–18:00", "1 个工作日内响应"],
]
df_contact = pd.DataFrame(CONTACTS, columns=CONTACT_HEADERS)
df_contact.to_csv(os.path.join(DATA, "业务对接人信息表.csv"), index=False, encoding="utf-8-sig")
p_contact = os.path.join(DATA, "业务对接人信息表.xlsx")
with pd.ExcelWriter(p_contact, engine="openpyxl") as w:
    df_contact.to_excel(w, sheet_name="业务对接人", index=False)
style_workbook(p_contact, {"业务对接人": [6, 22, 8, 14, 12, 16, 16, 46, 42, 18, 18]})


# ══════════════ S-10 客服组织结构 ══════════════
# 表 1：坐席分组
GROUP_HEADERS = ["分组编号", "分组名称", "负责范围", "坐席人数", "主管", "在线时段",
                 "服务渠道", "日均接待量", "峰值接待量", "备注"]
GROUPS = [
    ["G1", "售前咨询组", "商品咨询（规格/材质/洗护/适用年龄/价格）", 12, "李敏", "09:00–22:00",
     "淘宝、天猫、拼多多、抖音小店、微信客服", 1800, 3200, "大促期间扩编至 20 人"],
    ["G2", "订单物流组", "订单状态、物流查询、改地址、催发货、取消订单", 8, "李敏", "09:00–21:00",
     "淘宝、天猫、拼多多、抖音小店", 1100, 2100, "与仓储物流组联动处理异常件"],
    ["G3", "售后服务组", "退换货、质量问题、少发漏发、退款、发票", 10, "李敏", "09:00–21:00",
     "淘宝、天猫、拼多多、抖音小店、微信客服", 900, 1600, "需具备 A/B/C 瑕疵分级判定能力"],
    ["G4", "营销活动组", "优惠券、满减、赠品、会员权益、大促玩法", 6, "陈静", "09:00–22:00",
     "淘宝、天猫、拼多多、抖音小店", 700, 2400, "大促期间峰值最高，需提前排班"],
    ["G5", "投诉与升级组", "客诉、赔付、高风险工单、主管复核", 5, "李敏", "09:00–20:00",
     "全渠道", 200, 500, "承接转人工的高优先级工单，24 小时内复核"],
    ["G6", "品牌与信任组", "是否正品、IP 授权、材质安全认证", 3, "周琳", "10:00–19:00",
     "全渠道", 150, 300, "由品牌部兼管，负责假一赔十口径"],
]
df_group = pd.DataFrame(GROUPS, columns=GROUP_HEADERS)

# 表 2：在线时段与人力
SHIFT_HEADERS = ["时段", "在线人数", "在班组别", "覆盖渠道", "人力占比", "说明"]
SHIFTS = [
    ["08:00–09:00", 8, "G1、G2", "全渠道", "0.10", "早班预备，处理夜间留言"],
    ["09:00–12:00", 32, "G1–G6", "全渠道", "0.39", "全组在线，咨询高峰期"],
    ["12:00–14:00", 24, "G1–G4（G5 值班 2 人）", "全渠道", "0.29", "午间轮班，保证不空岗"],
    ["14:00–18:00", 34, "G1–G6", "全渠道", "0.41", "全天最高峰，全员在线"],
    ["18:00–21:00", 26, "G1–G5", "全渠道", "0.32", "晚间咨询高峰（下班后下单）"],
    ["21:00–22:00", 14, "G1、G2、G4", "淘宝、天猫、抖音小店", "0.17", "晚间尾峰"],
    ["22:00–次日 08:00", 3, "夜间值班组", "全渠道（仅留言与紧急工单）", "0.04", "非实时在线，机器人兜底"],
]
df_shift = pd.DataFrame(SHIFTS, columns=SHIFT_HEADERS)

# 表 3：排班表（一周示例）
ROSTER_HEADERS = ["星期", "早班 08:00–16:00", "中班 12:00–20:00", "晚班 16:00–22:00",
                  "夜班 22:00–08:00", "当日在岗合计", "备注"]
ROSTER = [
    ["周一", 18, 20, 16, 3, 57, "常规排班"],
    ["周二", 18, 20, 16, 3, 57, "常规排班"],
    ["周三", 18, 20, 16, 3, 57, "常规排班"],
    ["周四", 18, 20, 16, 3, 57, "常规排班"],
    ["周五", 20, 22, 18, 3, 63, "周末前高峰，加派 6 人"],
    ["周六", 24, 26, 20, 3, 73, "周末高峰，加派 16 人"],
    ["周日", 24, 26, 20, 3, 73, "周末高峰，加派 16 人"],
]
df_roster = pd.DataFrame(ROSTER, columns=ROSTER_HEADERS)

# 表 4：峰值人力（大促）
PEAK_HEADERS = ["活动名称", "时间窗口", "日常在岗", "峰值在岗", "扩编比例", "峰值时段",
                "预估咨询量(日)", "应对措施"]
PEAKS = [
    ["双 11", "11-01 ~ 11-15", 57, 110, "1.93×", "11-01 00:00–02:00、11-11 全天", 18000,
     "提前 2 周扩编临时坐席 40 人；机器人承接 70% 标准化咨询"],
    ["双 12", "12-08 ~ 12-15", 57, 85, "1.49×", "12-12 10:00–16:00", 11000,
     "扩编临时坐席 20 人；活动组全员在线"],
    ["年货节", "01-10 ~ 01-28", 57, 95, "1.67×", "01-20 ~ 01-25 全天", 13000,
     "扩编临时坐席 30 人；物流组加强异常件处理"],
    ["618", "06-01 ~ 06-20", 57, 105, "1.84×", "06-01 00:00–02:00、06-18 全天", 16000,
     "提前 2 周扩编临时坐席 35 人；预售咨询专项话术"],
    ["日常周末", "每周六日", 57, 73, "1.28×", "14:00–21:00", 8500,
     "周末班次加派 16 人"],
    ["上新日", "每月 15 日（会员日）", 57, 70, "1.23×", "10:00–20:00", 7000,
     "新品咨询专项话术；商品运营在线支持"],
]
df_peak = pd.DataFrame(PEAKS, columns=PEAK_HEADERS)

p_org = os.path.join(DATA, "客服组织结构表.xlsx")
with pd.ExcelWriter(p_org, engine="openpyxl") as w:
    df_group.to_excel(w, sheet_name="坐席分组", index=False)
    df_shift.to_excel(w, sheet_name="在线时段", index=False)
    df_roster.to_excel(w, sheet_name="排班表", index=False)
    df_peak.to_excel(w, sheet_name="峰值人力", index=False)
style_workbook(p_org, {
    "坐席分组": [10, 14, 40, 10, 8, 14, 34, 12, 12, 34],
    "在线时段": [16, 10, 30, 30, 10, 34],
    "排班表": [8, 18, 18, 18, 18, 14, 24],
    "峰值人力": [14, 18, 10, 10, 10, 32, 14, 46],
})
# 顺带落一份坐席分组 CSV
df_group.to_csv(os.path.join(DATA, "客服坐席分组表.csv"), index=False, encoding="utf-8-sig")


# ══════════════ S-11 商品分类体系 ══════════════
# 读第 1 步商品主数据，按类目统计 SKU 数
sku = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"), encoding="utf-8-sig", dtype=str)
cnt = sku["类目"].value_counts().to_dict()
example = {}
for cat, g in sku.groupby("类目"):
    example[cat] = "、".join(sorted(g["SKU编码"])[:3])

TREE_HEADERS = ["一级类目", "二级类目", "三级类目", "SKU 编码前缀", "覆盖 SKU 数",
                "示例 SKU", "对应项目文档知识分类", "知识库切分单元", "备注"]
TREE = [
    ["毛绒玩具", "毛绒玩偶", "坐姿玩偶", "WK-BEAR/WK-RABBIT/WK-DINO/WK-PIG/WK-PENGUIN 等",
     cnt.get("毛绒玩偶", 0), example.get("毛绒玩偶", ""), "商品知识", "一款一单元（200–400 字）",
     "核心品类，占 SKU 总数约 42%"],
    ["毛绒玩具", "毛绒玩偶", "小尺寸玩偶", "WK-*-20/WK-*-25/WK-*-30", 0, "见毛绒玩偶类目",
     "商品知识", "一款一单元（200–400 字）", "20–30cm 段，属二级细分，不单独计 SKU"],
    ["毛绒玩具", "毛绒抱枕", "抱枕", "WK-BEARP/WK-RABBITP/WK-DINOP/WK-CATP 等",
     cnt.get("抱枕", 0), example.get("抱枕", ""), "商品知识", "一款一单元（200–400 字）",
     "40–55cm，兼具玩偶与家纺属性"],
    ["毛绒玩具", "毛绒挂件", "挂件/钥匙扣", "WK-*-K/WK-OCTO/WK-BURGER/WK-STAR 等",
     cnt.get("挂件", 0), example.get("挂件", ""), "商品知识", "一款一单元（200–400 字）",
     "8–15cm，配件属性，价格带最低"],
    ["潮玩", "搪胶潮玩", "搪胶摆件", "WK-STARBEAR/WK-MECHRAB/WK-TRENDBEAR 等",
     cnt.get("搪胶潮玩", 0), example.get("搪胶潮玩", ""), "商品知识", "一款一单元（200–400 字）",
     "材质为 Vinyl，实心，洗护口径为「表面擦拭」"],
    ["潮玩", "盲盒", "盲盒单盒/端盒", "WK-BLIND*-S / WK-BLIND*-C",
     cnt.get("盲盒", 0), example.get("盲盒", ""), "商品知识 + 售后政策", "一款一单元（200–400 字）",
     "售后退换需特别标注「已拆封不可退」边界"],
]
df_tree = pd.DataFrame(TREE, columns=TREE_HEADERS)
df_tree.to_csv(os.path.join(DATA, "商品分类体系表.csv"), index=False, encoding="utf-8-sig")
p_tree = os.path.join(DATA, "商品分类体系表.xlsx")
with pd.ExcelWriter(p_tree, engine="openpyxl") as w:
    df_tree.to_excel(w, sheet_name="类目树", index=False)
style_workbook(p_tree, {"类目树": [12, 14, 16, 46, 12, 30, 24, 24, 36]})

print("S-06 业务对接人信息表:", len(df_contact), "行")
print("S-10 客服组织结构表:", len(df_group), "分组 /", len(df_shift), "时段 /", len(df_roster), "排班 /", len(df_peak), "峰值")
print("S-11 商品分类体系表:", len(df_tree), "行；类目 SKU 数 =", cnt)
