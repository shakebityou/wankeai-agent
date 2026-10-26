# -*- coding: utf-8 -*-
"""【已废弃 · 请勿运行】生成 S-02 商品图片素材库（占位示意图）。

⛔ 本脚本已被 `build_09_爬取商品图片.py` 取代，**不要再运行**。
   它的输出是 Pillow 程序化示意图 + 扁平目录结构
   （`商品图片素材库/WK-XXX-00/...`），运行会覆盖当前的
   「类目 / SKU / 图片」两级目录与 296 张爬取的真实商品参考图。

   保留本文件仅为记录 S-02 的历史演进；如需重建图片素材库，请运行：
       python _scripts/build_09_爬取商品图片.py
       python _scripts/verify_09.py

背景：当前不具备生图能力，且直接抓取真实商品图存在版权与合规风险。
因此本脚本用 Pillow **程序化绘制**每个 SKU 的示意图：
  - 白底主图（正面）：纯白背景 + 类目配色 + SKU 标签
  - 细节图（特写）：放大主体 + 材质文字标注
  - 场景图：柔和渐变背景 + 主体 + 尺寸标注

命名严格对应 SKU，并输出 data/商品图片索引.csv 供图搜训练与评测使用。

⚠️ 交付说明：本目录为**占位示意图**，用于打通图搜链路与评测流程；
   正式上线前须由商家用真实商品照片按同一命名规则替换。
"""
import os
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
IMGDIR = os.path.join(DATA, "商品图片素材库")
os.makedirs(IMGDIR, exist_ok=True)

W = H = 800

# 类目配色：(主体色, 浅色, 深色)
PALETTE = {
    "毛绒玩偶": ("#F2C6CE", "#FBE8EB", "#C97B84"),
    "挂件":     ("#F5D9A8", "#FDF1DC", "#D9A441"),
    "抱枕":     ("#BFD8E8", "#E7F0F6", "#5B8FB9"),
    "搪胶潮玩": ("#C9B6E4", "#EDE5F6", "#7E5FA8"),
    "盲盒":     ("#B9DFD0", "#E4F4ED", "#4E9E7E"),
}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


F_TITLE = load_font(40)
F_SKU = load_font(34)
F_SMALL = load_font(26)
F_TINY = load_font(22)
F_TAG = load_font(28)


def center_text(d, y, text, font, fill):
    box = d.textbbox((0, 0), text, font=font)
    d.text(((W - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def draw_creature(d, cx, cy, r, body, dark, kind="玩偶"):
    """画一个简化的可爱主体：头 + 双耳 + 眼 + 腮红。"""
    ear_r = int(r * 0.36)
    # 耳朵
    d.ellipse([cx - r * 0.72, cy - r * 1.12, cx - r * 0.72 + ear_r * 2,
               cy - r * 1.12 + ear_r * 2], fill=body, outline=dark, width=4)
    d.ellipse([cx + r * 0.72 - ear_r * 2, cy - r * 1.12,
               cx + r * 0.72, cy - r * 1.12 + ear_r * 2],
              fill=body, outline=dark, width=4)
    # 身体
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=body, outline=dark, width=5)
    # 肚皮
    d.ellipse([cx - r * 0.52, cy - r * 0.05, cx + r * 0.52, cy + r * 0.78],
              fill="#FFFFFF", outline=dark, width=3)
    # 眼睛
    eye_r = int(r * 0.10)
    for dx in (-r * 0.34, r * 0.34):
        d.ellipse([cx + dx - eye_r, cy - r * 0.30 - eye_r,
                   cx + dx + eye_r, cy - r * 0.30 + eye_r], fill="#3A3A3A")
        d.ellipse([cx + dx - eye_r * 0.35, cy - r * 0.36 - eye_r * 0.35,
                   cx + dx + eye_r * 0.1, cy - r * 0.36 + eye_r * 0.1], fill="#FFFFFF")
    # 鼻子/嘴
    d.ellipse([cx - r * 0.07, cy - r * 0.10, cx + r * 0.07, cy + r * 0.04], fill=dark)
    # 腮红
    for dx in (-r * 0.60, r * 0.60):
        d.ellipse([cx + dx - r * 0.14, cy - r * 0.10,
                   cx + dx + r * 0.14, cy + r * 0.16], fill="#F7B7C0")


def base_canvas(bg="#FFFFFF"):
    return Image.new("RGB", (W, H), bg)


def img_white(sku, name, cat, size, material, idx):
    """白底主图 / 细节图。"""
    body, light, dark = PALETTE.get(cat, ("#F2C6CE", "#FBE8EB", "#C97B84"))
    im = base_canvas("#FFFFFF")
    d = ImageDraw.Draw(im)
    if idx == 0:
        # 白底正面主图
        draw_creature(d, W // 2, H // 2 - 30, 175, body, dark)
        center_text(d, H - 190, name, F_TITLE, "#333333")
        center_text(d, H - 138, "{}  |  {}".format(sku, size), F_SKU, dark)
        center_text(d, H - 92, cat + "  ·  白底主图", F_SMALL, "#888888")
    else:
        # 细节特写图：放大 + 材质标注
        draw_creature(d, W // 2, H // 2 - 40, 235, body, dark)
        center_text(d, H - 200, "细节特写 · 材质", F_TITLE, "#333333")
        center_text(d, H - 146, material, F_SKU, dark)
        center_text(d, H - 98, "{}  |  {}".format(sku, name), F_SMALL, "#888888")
    # 角标
    d.rectangle([0, 0, 132, 54], fill=dark)
    d.text((16, 12), "示意图", font=F_TAG, fill="#FFFFFF")
    return im


def img_scene(sku, name, cat, size):
    """场景图：柔和渐变 + 尺寸标注。"""
    body, light, dark = PALETTE.get(cat, ("#F2C6CE", "#FBE8EB", "#C97B84"))
    im = base_canvas(light)
    d = ImageDraw.Draw(im)
    # 竖向渐变
    r0, g0, b0 = im.getpixel((0, 0))
    r1, g1, b1 = 255, 255, 255
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=(int(r0 + (r1 - r0) * t), int(g0 + (g1 - g0) * t), int(b0 + (b1 - b0) * t)))
    # 地面椭圆
    d.ellipse([W // 2 - 250, H - 330, W // 2 + 250, H - 210], fill="#FFFFFF", outline=dark, width=3)
    draw_creature(d, W // 2, H // 2 - 40, 165, body, dark)
    # 尺寸标注线
    d.line([W // 2 - 250, H - 270, W // 2 + 250, H - 270], fill=dark, width=4)
    for x in (W // 2 - 250, W // 2 + 250):
        d.line([x, H - 282, x, H - 258], fill=dark, width=4)
    center_text(d, H - 250, "尺寸：{}".format(size), F_SKU, dark)
    center_text(d, H - 168, name, F_TITLE, "#333333")
    center_text(d, H - 114, sku + "  ·  场景图", F_SMALL, "#777777")
    d.rectangle([0, 0, 132, 54], fill=dark)
    d.text((16, 12), "示意图", font=F_TAG, fill="#FFFFFF")
    return im


def img_package(sku, name, cat, packaging):
    """包装图。"""
    body, light, dark = PALETTE.get(cat, ("#F2C6CE", "#FBE8EB", "#C97B84"))
    im = base_canvas("#FFFFFF")
    d = ImageDraw.Draw(im)
    # 盒子
    d.polygon([(W // 2 - 200, 250), (W // 2, 190), (W // 2 + 200, 250), (W // 2, 310)],
              fill=light, outline=dark)
    d.polygon([(W // 2 - 200, 250), (W // 2, 310), (W // 2, 560), (W // 2 - 200, 500)],
              fill=body, outline=dark)
    d.polygon([(W // 2 + 200, 250), (W // 2, 310), (W // 2, 560), (W // 2 + 200, 500)],
              fill=light, outline=dark)
    center_text(d, 340, "玩可爱", F_TITLE, dark)
    center_text(d, 600, "包装形式", F_TITLE, "#333333")
    center_text(d, 656, packaging, F_SKU, dark)
    center_text(d, 712, "{}  |  {}".format(sku, name), F_SMALL, "#888888")
    d.rectangle([0, 0, 132, 54], fill=dark)
    d.text((16, 12), "示意图", font=F_TAG, fill="#FFFFFF")
    return im


def main():
    import pandas as pd

    sku_df = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"),
                         encoding="utf-8-sig", dtype=str).fillna("")
    index = []
    for _, r in sku_df.iterrows():
        sku = r["SKU编码"]
        name = r["商品名"]
        cat = r["类目"]
        size = r["尺寸"]
        material = r["材质"]
        packaging = r["包装形式"]
        d = os.path.join(IMGDIR, sku)
        os.makedirs(d, exist_ok=True)

        jobs = [
            ("主图1_白底", "白底主图", img_white(sku, name, cat, size, material, 0)),
            ("主图2_细节", "细节图", img_white(sku, name, cat, size, material, 1)),
            ("主图3_场景", "场景图", img_scene(sku, name, cat, size)),
            ("主图4_包装", "包装图", img_package(sku, name, cat, packaging)),
        ]
        for fname, kind, im in jobs:
            rel = "{}/{}_{}.jpg".format(sku, sku, fname)
            path = os.path.join(d, "{}_{}.jpg".format(sku, fname))
            im.save(path, "JPEG", quality=88)
            index.append({
                "SKU编码": sku, "商品名": name, "类目": cat,
                "图片文件": rel, "图片类型": kind,
                "尺寸": "800x800", "生成方式": "Pillow 程序化示意图",
                "是否占位图": "是", "备注": "正式上线前须替换为真实商品照片",
            })
        print("已生成:", sku, len(jobs), "张")

    idx_df = pd.DataFrame(index)
    idx_df.to_csv(os.path.join(DATA, "商品图片索引.csv"), index=False, encoding="utf-8-sig")
    print()
    print("图片总数:", len(index), "；SKU 数:", sku_df.shape[0])
    print("索引文件:", os.path.join(DATA, "商品图片索引.csv"))


if __name__ == "__main__":
    raise SystemExit(
        "⛔ 本脚本已废弃（会覆盖当前爬取的真实商品参考图与两级目录结构）。\n"
        "   请改用：python _scripts/build_09_爬取商品图片.py\n"
        "   如需强行运行历史版本，请设置环境变量 FORCE_BUILD_07=1。"
    ) if os.environ.get("FORCE_BUILD_07") != "1" else main()
