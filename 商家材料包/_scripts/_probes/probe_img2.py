# -*- coding: utf-8 -*-
"""探测图片源限流头 + 实际下载成功率，并确认索引文件被哪些验收脚本引用。"""
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def head(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.headers, r.read()


print("=" * 68)
print("A. 限流头")
print("=" * 68)
try:
    h, _ = head("https://api.openverse.org/v1/images/?q=teddy+bear&page_size=1")
    for k in ["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
              "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset"]:
        if k in h:
            print("  openverse", k, "=", h[k])
    print("  openverse 全部限流相关头:", [k for k in h.keys() if "ate" in k or "imit" in k])
except Exception as e:
    print("  openverse err:", e)

print()
print("=" * 68)
print("B. Wikimedia 批量查询 + 下载成功率")
print("=" * 68)
kw = "plush teddy bear toy"
u = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search"
     "&gsrsearch=" + urllib.parse.quote(kw) +
     "&gsrnamespace=6&gsrlimit=12&prop=imageinfo&iiprop=url|size|mime|extmetadata"
     "&iiurlwidth=800&format=json")
h, body = head(u)
d = json.loads(body.decode("utf-8"))
pages = d.get("query", {}).get("pages", {})
print("  候选数:", len(pages))
ok = bad = 0
for pid, p in pages.items():
    ii = (p.get("imageinfo") or [{}])[0]
    url = ii.get("thumburl") or ii.get("url")
    mime = ii.get("mime", "")
    if not url or not mime.startswith("image/"):
        continue
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            b = r.read()
        from PIL import Image
        im = Image.open(io.BytesIO(b))
        im.load()
        ok += 1
        print("    OK  {:>5}x{:<5} {:>7}B  {}".format(im.size[0], im.size[1], len(b),
                                                     (p.get("title") or "")[:44]))
    except Exception as e:
        bad += 1
        print("    BAD", type(e).__name__, str(e)[:60])
        time.sleep(0.2)
print("  下载成功 {} / 失败 {}".format(ok, bad))

print()
print("=" * 68)
print("C. Openverse 下载成功率")
print("=" * 68)
u2 = ("https://api.openverse.org/v1/images/?q=" + urllib.parse.quote(kw) +
      "&page_size=12&license_type=all")
try:
    h2, b2 = head(u2)
    d2 = json.loads(b2.decode("utf-8"))
    ok = bad = 0
    for it in d2.get("results", []):
        url = it.get("url")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                b = r.read()
            from PIL import Image
            im = Image.open(io.BytesIO(b))
            im.load()
            ok += 1
            print("    OK  {:>5}x{:<5} {:>7}B  {}".format(im.size[0], im.size[1], len(b),
                                                         (it.get("title") or "")[:40]))
        except Exception as e:
            bad += 1
            print("    BAD", type(e).__name__, str(e)[:60])
    print("  下载成功 {} / 失败 {}".format(ok, bad))
except Exception as e:
    print("  openverse err:", e)

print()
print("=" * 68)
print("D. 哪些脚本引用了「商品图片」")
print("=" * 68)
sd = os.path.join(BASE, "_scripts")
for fn in sorted(os.listdir(sd)):
    if not fn.endswith(".py"):
        continue
    t = open(os.path.join(sd, fn), encoding="utf-8").read()
    hits = [m for m in ["商品图片索引", "商品图片素材库", "图片文件", "是否占位图"] if m in t]
    if hits:
        print("  {:<34} {}".format(fn, hits))
