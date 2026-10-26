# -*- coding: utf-8 -*-
"""小样：验证 Bing 主源 + 处理链路，只跑 3 个系列，不落正式目录。"""
import importlib.util
import os
import sys

SPEC = r"D:\learn\homework\agent\商家材料包\_scripts\build_09_爬取商品图片.py"
spec = importlib.util.spec_from_file_location("b9", SPEC)
m = importlib.util.module_from_spec(spec)
sys.modules["b9"] = m
spec.loader.exec_module(m)

for fam in ["WK-BEAR", "WK-CLOVER", "WK-DEEPSEAOCTO"]:
    q, vs = m.FAMILY_QUERY[fam]
    print("\n" + "=" * 64)
    print("系列", fam, "←", q)
    c = m.candidates(fam, [q] + list(vs))
    print("候选:", len(c), " 其中电商图床:", sum(1 for x in c if x.get("prefer")))
    for x in c[:4]:
        print("   ", x["source"], "|", x["url"][:88])
    got = []
    for x in c[:10]:
        im, raw = m.download(x["url"])
        if im is not None:
            got.append((im, x))
    print("可用:", len(got), "/10")
    if got:
        outdir = os.path.join(m.IMGDIR, "_smoketest", fam)
        os.makedirs(outdir, exist_ok=True)
        for kind in m.VIEWS:
            t = m.treat(got[0][0].copy(), kind, fam + "-TEST")
            p = os.path.join(outdir, "{}_{}.jpg".format(fam, kind))
            t.save(p, "JPEG", quality=88)
        print("已落盘 4 张 →", outdir)
