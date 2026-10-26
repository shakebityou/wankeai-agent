# -*- coding: utf-8 -*-
"""探测 Openverse / Wikimedia 的返回结构，确定图片直链字段。"""
import json
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── Openverse ──
u = "https://api.openverse.org/v1/images/?q=" + urllib.parse.quote("teddy bear plush toy") + "&page_size=3&license_type=all"
d = json.loads(get(u).decode("utf-8"))
print("Openverse result_count:", d.get("result_count"))
for it in d.get("results", [])[:3]:
    print("  title :", it.get("title"))
    print("  url   :", it.get("url"))
    print("  thumb :", it.get("thumbnail"))
    print("  w/h   :", it.get("width"), it.get("height"))
    print("  lic   :", it.get("license"), it.get("license_version"))
    print("  src   :", it.get("source"), "|", it.get("foreign_landing_url"))
    print()

# ── Wikimedia Commons ──
u2 = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search"
      "&gsrsearch=" + urllib.parse.quote("teddy bear plush") +
      "&gsrnamespace=6&gsrlimit=3&prop=imageinfo&iiprop=url|size|extmetadata"
      "&iiurlwidth=800&format=json")
d2 = json.loads(get(u2).decode("utf-8"))
pages = d2.get("query", {}).get("pages", {})
print("Wikimedia pages:", len(pages))
for pid, p in list(pages.items())[:3]:
    ii = (p.get("imageinfo") or [{}])[0]
    print("  title:", p.get("title"))
    print("  url  :", ii.get("url"))
    print("  thumb:", ii.get("thumburl"))
    print("  size :", ii.get("width"), ii.get("height"))
    em = ii.get("extmetadata") or {}
    print("  lic  :", (em.get("LicenseShortName") or {}).get("value"))
    print()
