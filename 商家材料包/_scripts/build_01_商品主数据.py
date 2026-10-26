# -*- coding: utf-8 -*-
"""生成「玩可爱」商品主数据表（对应商家资料索取清单 S-01 / S-11）。

输出：
  data/商品主数据表.xlsx  —— 主数据 Sheet + 字段说明 Sheet
  data/商品主数据表.csv   —— utf-8-sig，便于 Excel / pandas 直接读取
"""
import csv
import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

HEADERS = [
    "SKU编码", "商品名", "类目", "尺寸", "材质", "填充物", "适用年龄",
    "洗护方式", "包装形式", "上市时间", "是否IP联名", "吊牌价", "现价", "库存状态",
]

# ── 洗护话术模板 ────────────────────────────────────────────────
WASH_HAND = "手洗：30℃以下温水加中性洗涤剂轻揉，不可漂白，阴凉处平铺晾干"
WASH_HAND_DRY = "手洗：30℃以下温水轻揉，不可机洗，不可漂白，避免阳光暴晒"
WASH_MACHINE = "机洗：装入洗衣袋后 30℃轻柔模式，不可漂白，低温烘干"
WASH_WIPE = "表面擦拭：湿布蘸中性清洁剂轻擦，不可水洗，避免暴晒"
WASH_DRY = "仅可干洗：交由专业干洗处理，不可水洗，不可漂白"

# ── 库存状态轮转（保证每类目都有现货/预售/缺货样本）─────────────
STOCK = [
    "现货", "现货", "现货", "预售(7天)", "现货", "预售(15天)",
    "现货", "现货", "缺货", "现货", "预售(30天)", "现货",
]

# ── 类目 1：毛绒玩偶 ────────────────────────────────────────────
# (编码, 名称, [(尺寸, 吊牌价, 现价)], 材质, 填充物, 适用年龄, 洗护, 包装, 上市, 是否IP)
PLUSH = [
    ("BEAR", "奶油小熊", [("20cm", 69, 49), ("30cm", 99, 79), ("40cm", 159, 129), ("50cm", 229, 189)],
     "水晶超柔", "PP棉", "3岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-03-15", "否"),
    ("RABBIT", "云朵兔子", [("20cm", 69, 52), ("30cm", 109, 85), ("40cm", 169, 139), ("50cm", 239, 199)],
     "仿兔毛", "PP棉", "3岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-04-02", "否"),
    ("DINO", "抹茶恐龙", [("25cm", 89, 69), ("35cm", 139, 109), ("45cm", 199, 159)],
     "短毛绒", "PP棉", "3岁以上", WASH_HAND_DRY, "礼盒装", "2024-05-20", "否"),
    ("PIG", "草莓猪", [("20cm", 79, 59), ("30cm", 119, 92), ("40cm", 179, 145)],
     "牛奶绒", "PP棉", "3岁以上", WASH_HAND, "彩盒装", "2024-06-08", "否"),
    ("PENGUIN", "布丁企鹅", [("25cm", 99, 79), ("35cm", 149, 119)],
     "法兰绒", "三维中空棉", "3岁以上", WASH_MACHINE, "卡纸吊牌+PE袋", "2024-07-12", "否"),
    ("DEER", "小鹿斑斑", [("30cm", 129, 99), ("45cm", 199, 159)],
     "仿兔毛", "PP棉", "3岁以上", WASH_HAND_DRY, "礼盒装", "2024-08-01", "否"),
    ("POLARBEAR", "北极熊白白", [("30cm", 139, 109), ("45cm", 209, 169)],
     "水晶超柔", "PP棉", "3岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-09-10", "否"),
    ("SHIBA", "柴犬豆豆", [("25cm", 89, 72), ("35cm", 139, 112), ("45cm", 199, 165)],
     "羊羔绒", "PP棉", "3岁以上", WASH_MACHINE, "卡纸吊牌+PE袋", "2024-02-18", "否"),
    ("PANDA", "熊猫团子", [("30cm", 129, 105), ("50cm", 249, 205)],
     "水晶超柔", "PP棉", "3岁以上", WASH_HAND, "礼盒装", "2024-10-01", "否"),
    ("UNICORN", "独角兽星愿", [("35cm", 179, 145)],
     "幻彩绒", "PP棉", "6岁以上", WASH_HAND_DRY, "礼盒装", "2024-11-11", "是"),
    ("SLOTH", "树懒慢慢", [("40cm", 189, 155)],
     "短毛绒", "PP棉", "3岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-05-06", "否"),
    ("SHEEP", "小羊咩咩", [("30cm", 119, 96)],
     "羊羔绒", "PP棉", "3岁以上", WASH_MACHINE, "彩盒装", "2024-06-22", "否"),
    ("FOX", "狐狸阿橘", [("35cm", 149, 122)],
     "仿兔毛", "PP棉", "3岁以上", WASH_HAND_DRY, "卡纸吊牌+PE袋", "2024-09-25", "否"),
    ("OTTER", "海獭抱抱", [("45cm", 219, 179)],
     "水晶超柔", "PP棉+泡沫粒子", "3岁以上", WASH_HAND, "礼盒装", "2025-01-06", "否"),
    ("ELEPHANT", "大象嘟嘟", [("40cm", 179, 149)],
     "牛奶绒", "PP棉", "3岁以上", WASH_MACHINE, "卡纸吊牌+PE袋", "2024-12-02", "否"),
]

# ── 类目 2：挂件 ────────────────────────────────────────────────
KEYCHAIN = [
    ("BEARK", "奶油小熊挂件", [("10cm", 39, 29), ("15cm", 49, 39)],
     "水晶超柔", "PP棉", "6岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-03-20", "否"),
    ("RABBITK", "云朵兔子挂件", [("10cm", 39, 32), ("15cm", 49, 42)],
     "仿兔毛", "PP棉", "6岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-04-15", "否"),
    ("OCTO", "章鱼哥挂件", [("12cm", 45, 35)],
     "短毛绒", "PP棉", "6岁以上", WASH_HAND_DRY, "卡纸吊牌+PE袋", "2024-07-01", "否"),
    ("BURGER", "小熊汉堡挂件", [("10cm", 42, 33)],
     "短毛绒", "PP棉", "6岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-08-18", "否"),
    ("STAR", "星星月亮挂件", [("12cm", 45, 36)],
     "幻彩绒", "PP棉", "6岁以上", WASH_WIPE, "卡纸吊牌+PE袋", "2024-09-01", "否"),
    ("SHIBAK", "柴犬豆豆挂件", [("10cm", 39, 31), ("15cm", 52, 44)],
     "羊羔绒", "PP棉", "6岁以上", WASH_MACHINE, "卡纸吊牌+PE袋", "2024-02-28", "否"),
    ("PANDAK", "熊猫团子挂件", [("12cm", 45, 38)],
     "水晶超柔", "PP棉", "6岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-10-10", "否"),
    ("PIGK", "草莓猪挂件", [("10cm", 39, 30)],
     "牛奶绒", "PP棉", "6岁以上", WASH_HAND, "卡纸吊牌+PE袋", "2024-06-15", "否"),
    ("DINOK", "抹茶恐龙挂件", [("12cm", 45, 36), ("15cm", 52, 42)],
     "短毛绒", "PP棉", "6岁以上", WASH_HAND_DRY, "卡纸吊牌+PE袋", "2024-05-28", "否"),
    ("PENGUINK", "布丁企鹅挂件", [("10cm", 39, 31)],
     "法兰绒", "三维中空棉", "6岁以上", WASH_MACHINE, "卡纸吊牌+PE袋", "2024-07-20", "否"),
    ("CLOVER", "幸运四叶草挂件", [("8cm", 29, 22)],
     "幻彩绒", "PP棉", "6岁以上", WASH_WIPE, "卡纸吊牌+PE袋", "2025-01-15", "否"),
]

# ── 类目 3：抱枕 ────────────────────────────────────────────────
PILLOW = [
    ("BEARP", "奶油小熊抱枕", [("40cm", 119, 89), ("50cm", 159, 125)],
     "水晶超柔", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-03-25", "否"),
    ("RABBITP", "云朵兔子抱枕", [("40cm", 119, 95), ("50cm", 159, 129)],
     "仿兔毛", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-04-20", "否"),
    ("DINOP", "恐龙蛋抱枕", [("45cm", 139, 109)],
     "短毛绒", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-06-01", "否"),
    ("CATP", "猫咪抱枕", [("40cm", 129, 99), ("55cm", 179, 145)],
     "牛奶绒", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-08-10", "否"),
    ("SHIBAP", "柴犬豆豆抱枕", [("50cm", 169, 135)],
     "羊羔绒", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-02-25", "否"),
    ("PANDAP", "熊猫团子抱枕", [("45cm", 149, 119)],
     "水晶超柔", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-10-15", "否"),
    ("CLOUDP", "云朵造型抱枕", [("40cm", 109, 85)],
     "法兰绒", "三维中空棉", "3岁以上", WASH_MACHINE, "压缩袋+卡纸", "2024-11-20", "否"),
]

# ── 类目 4：搪胶潮玩 ────────────────────────────────────────────
VINYL = [
    ("STARBEAR", "星际小熊搪胶", [("18cm", 199, 159)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-04-08", "否"),
    ("MECHRAB", "机械兔搪胶", [("20cm", 239, 189)], "搪胶（Vinyl）+ABS", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-06-18", "否"),
    ("TRENDBEAR", "潮流熊搪胶", [("25cm", 329, 269)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装+亚克力罩", "2024-09-05", "否"),
    ("SPACECAT", "宇宙猫搪胶", [("22cm", 279, 229)], "搪胶（Vinyl）+ABS", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-10-20", "否"),
    ("ROCKPENGUIN", "摇滚企鹅搪胶", [("20cm", 249, 199)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-07-28", "否"),
    ("DESSERTBEAR", "甜品熊搪胶", [("16cm", 179, 139)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-05-12", "否"),
    ("CYBERSHIBA", "赛博柴犬搪胶", [("24cm", 299, 239)], "搪胶（Vinyl）+ABS", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装+亚克力罩", "2024-12-12", "否"),
    ("GARDENELF", "花园精灵搪胶", [("18cm", 209, 169)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2024-08-22", "否"),
    ("ASTROABBIT", "宇航兔搪胶", [("26cm", 359, 299)], "搪胶（Vinyl）+ABS", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装+亚克力罩", "2024-11-11", "是"),
    ("DEEPSEAOCTO", "深海章鱼搪胶", [("21cm", 259, 209)], "搪胶（Vinyl）", "无（实心摆件）",
     "15岁以上", WASH_WIPE, "彩盒装", "2025-01-20", "否"),
]

# ── 类目 5：盲盒 ────────────────────────────────────────────────
BLIND = [
    ("BLIND-PLANET", "玩偶星球盲盒", [("单盒", 59, 49), ("端盒(6盒)", 354, 279)],
     "PVC+ABS", "无（实心摆件）", "15岁以上", WASH_WIPE, "彩盒装（密封）", "2024-05-01", "是"),
    ("BLIND-DESSERT", "甜品派对盲盒", [("单盒", 59, 49), ("端盒(6盒)", 354, 279)],
     "PVC+ABS", "无（实心摆件）", "15岁以上", WASH_WIPE, "彩盒装（密封）", "2024-08-08", "否"),
    ("BLIND-FOREST", "森林物语盲盒", [("单盒", 69, 55), ("端盒(6盒)", 414, 319)],
     "PVC+ABS", "无（实心摆件）", "15岁以上", WASH_WIPE, "彩盒装（密封）", "2024-10-25", "否"),
    ("BLIND-SPACE", "太空旅行盲盒", [("单盒", 69, 55), ("端盒(6盒)", 414, 319)],
     "PVC+ABS", "无（实心摆件）", "15岁以上", WASH_WIPE, "彩盒装（密封）", "2024-12-20", "是"),
]

SERIES = [
    ("毛绒玩偶", PLUSH, "玩偶"),
    ("挂件", KEYCHAIN, "挂件"),
    ("抱枕", PILLOW, "抱枕"),
    ("搪胶潮玩", VINYL, "搪胶"),
    ("盲盒", BLIND, "盲盒"),
]


def build_rows():
    rows = []
    idx = 0
    for cat, series, suffix in SERIES:
        for code, name, sizes, mat, fill, age, wash, pkg, launch, is_ip in series:
            for size, tag_price, now_price in sizes:
                if cat == "盲盒":
                    # 盲盒编码去掉中间连字符，保持 WK-{系列}-{后缀} 统一格式
                    blind_code = code.replace("BLIND-", "BLIND")
                    sku = "WK-{}-{}".format(blind_code, "S" if size == "单盒" else "C")
                    display_size = "单盒 8cm" if size == "单盒" else "端盒 8cm×6"
                    goods = "{} {}".format(name, "单盒" if size == "单盒" else "端盒")
                else:
                    num = size.replace("cm", "")
                    sku = "WK-{}-{}".format(code, num)
                    display_size = size
                    goods = "{} {}".format(name, size)
                rows.append({
                    "SKU编码": sku,
                    "商品名": goods,
                    "类目": cat,
                    "尺寸": display_size,
                    "材质": mat,
                    "填充物": fill,
                    "适用年龄": age,
                    "洗护方式": wash,
                    "包装形式": pkg,
                    "上市时间": launch,
                    "是否IP联名": is_ip,
                    "吊牌价": tag_price,
                    "现价": now_price,
                    "库存状态": STOCK[idx % len(STOCK)],
                })
                idx += 1
    return rows


FIELD_DOC = [
    ("SKU编码", "商品唯一编码，规则 WK-{系列}-{尺寸}，全店唯一，接口与知识库统一引用此字段"),
    ("商品名", "对客展示名称，含尺寸后缀，与详情页标题一致"),
    ("类目", "商家内部类目树一级分类：毛绒玩偶 / 挂件 / 抱枕 / 搪胶潮玩 / 盲盒"),
    ("尺寸", "商品尺寸，盲盒为「单盒 8cm」「端盒 8cm×6」"),
    ("材质", "面料或主体材质，用于回答「什么材质」类问题"),
    ("填充物", "内部填充物；搪胶与盲盒为实心摆件，无填充"),
    ("适用年龄", "安全提示年龄，玩具类商品必填，涉及 GB 6675 适用性"),
    ("洗护方式", "标准洗护话术，可直接作为客服回答内容"),
    ("包装形式", "出厂包装，用于回答「有没有礼盒」「怎么包装」类问题"),
    ("上市时间", "YYYY-MM-DD，用于判断新款与老款"),
    ("是否IP联名", "是 / 否，IP 联名款需额外提供授权证明"),
    ("吊牌价", "官方标价，单位元"),
    ("现价", "当前实际售价，单位元，促销期间与活动规则联动"),
    ("库存状态", "现货 / 预售(N天) / 缺货；实时库存需走库存接口，本字段仅作静态参考"),
]


def main():
    rows = build_rows()
    assert rows, "no rows generated"

    # 唯一性校验
    skus = [r["SKU编码"] for r in rows]
    assert len(skus) == len(set(skus)), "SKU 编码重复"

    # ── CSV ──
    csv_path = os.path.join(DATA, "商品主数据表.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)

    # ── XLSX ──
    wb = Workbook()
    ws = wb.active
    ws.title = "商品主数据"

    head_font = Font(bold=True, color="FFFFFF", size=11)
    head_fill = PatternFill("solid", fgColor="C97B84")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(HEADERS)
    for c in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    ws.row_dimensions[1].height = 24

    for r in rows:
        ws.append([r[h] for h in HEADERS])

    widths = [20, 22, 12, 14, 20, 18, 12, 46, 18, 12, 12, 10, 10, 12]
    for i, w_ in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w_

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(HEADERS)):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=(cell.column == 8))

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:{}{}".format(get_column_letter(len(HEADERS)), ws.max_row)

    # 字段说明 Sheet
    ws2 = wb.create_sheet("字段说明")
    ws2.append(["字段名", "含义与用途"])
    for c in range(1, 3):
        cell = ws2.cell(row=1, column=c)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for name, desc in FIELD_DOC:
        ws2.append([name, desc])
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 80
    for row in ws2.iter_rows(min_row=2, max_row=ws2.max_row, max_col=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws2.freeze_panes = "A2"

    xlsx_path = os.path.join(DATA, "商品主数据表.xlsx")
    wb.save(xlsx_path)

    # ── 统计 ──
    from collections import Counter
    by_cat = Counter(r["类目"] for r in rows)
    print("SKU 总数:", len(rows))
    for cat, n in by_cat.items():
        print("  {}: {}".format(cat, n))
    print("XLSX:", xlsx_path)
    print("CSV :", csv_path)


if __name__ == "__main__":
    main()
