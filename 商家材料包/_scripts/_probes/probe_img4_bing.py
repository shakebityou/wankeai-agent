# -*- coding: utf-8 -*-
"""探测 Bing 图片搜索的可解析性：能否稳定取出直链 murl。"""
import json
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def bing(query, count=35):
    url = ("https://www.bing.com/images/async?q=" + urllib.parse.quote(query) +
           "&first=0&count={}&mmasync=1".format(count))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/images/search?q=" + urllib.parse.quote(query),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "ignore")
    out = []
    # m="{\"murl\":\"...\"}" 形式
    for mm in re.finditer(r'm="(\{[^"]*?&quot;murl&quot;.*?\})"', html):
        pass
    # 直接抓 murl&quot;:&quot;URL&quot;
    for mm in re.finditer(r'murl&quot;:&quot;(.*?)&quot;', html):
        u = mm.group(1)
        if u.startswith("http"):
            out.append(u)
    if not out:
        for mm in re.finditer(r'"murl":"(.*?)"', html):
            u = mm.group(1).replace("\\/", "/")
            if u.startswith("http"):
                out.append(u)
    # 标题
    titles = [t for t in re.findall(r't="([^"]{6,90})"', html)]
    return out, titles, len(html)


for q in ["teddy bear plush toy", "four leaf clover keychain charm", "shiba inu plush"]:
    print("=" * 64)
    print("query:", q)
    try:
        urls, titles, hl = bing(q)
        print("  html len:", hl, " 直链数:", len(urls))
        for u in urls[:5]:
            print("    ", u[:110])
        print("  样例标题:", titles[:4])
    except Exception as e:
        print("  ERR", type(e).__name__, str(e)[:90])
    time.sleep(1.5)
