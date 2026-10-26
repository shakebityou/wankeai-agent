# -*- coding: utf-8 -*-
"""小样测试：只跑 2 个系列，验证抓取→下载→处理→落盘链路。"""
import importlib.util
import os
import sys

SPEC = r"D:\learn\homework\agent\商家材料包\_scripts\build_09_爬取商品图片.py"
spec = importlib.util.spec_from_file_location("b9", SPEC)
m = importlib.util.module_from_spec(spec)
sys.modules["b9"] = m
spec.loader.exec_module(m)

FAMS = ["WK-BEAR", "WK-CLOVER"]
for fam in FAMS:
    q = m.FAMILY_QUERY[fam][0]
    print("\n" + "=" * 62)
    print("系列", fam, "←", q)
    c = m.candidates(fam, q)
    print("候选数:", len(c))
    for i, x in enumerate(c[:3]):
        print("  [{}] {} | {} | {}".format(i, x["source"], x["license"], (x["title"] or "")[:50]))
    got = []
    for x in c[:8]:
        im, raw = m.download(x["url"])
        if im is not None:
            got.append((im, x))
            print("  下载OK", im.size, len(raw), "B")
    print("可用:", len(got))
    if got:
        im, x = got[0]
        outdir = os.path.join(m.IMGDIR, "_smoketest", fam)
        os.makedirs(outdir, exist_ok=True)
        for kind in m.VIEWS:
            t = m.treat(im.copy(), kind, fam + "-TEST")
            p = os.path.join(outdir, "{}_{}.jpg".format(fam, kind))
            t.save(p, "JPEG", quality=88)
            print("  落盘", os.path.basename(p), t.size, os.path.getsize(p), "B")
