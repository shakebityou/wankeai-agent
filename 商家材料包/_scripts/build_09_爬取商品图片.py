# -*- coding: utf-8 -*-
"""S-02 商品图片素材库：爬取真实商品照片，按「类目 / SKU」两级文件夹存放。

背景
----
当前不具备生图能力；Pillow 程序化示意图（build_07）不够像真实商品照片。
本脚本从公开图片搜索抓取与每个 SKU 主题相近的**真实商品照片**，作为图搜
训练与识别评测的素材（用户要求：「商家照片需要爬取，大概长得差不多就行」）。

目录结构（每个文件放属于自己的文件夹）
--------------------------------------
    商品图片素材库/
      毛绒玩偶/
        WK-BEAR-20/
          WK-BEAR-20_主图1_白底.jpg
          WK-BEAR-20_主图2_细节.jpg
          WK-BEAR-20_主图3_场景.jpg
          WK-BEAR-20_主图4_包装.jpg
        WK-BEAR-30/ ...
      挂件/ 抱枕/ 搪胶潮玩/ 盲盒/    （同构）

⚠️ 合规声明（务必随材料包一并交付）
----------------------------------
这些是**相似参考图**（同品类、相近造型的公开网络商品图），**不是本店真实商品照片**。
- 仅用于本项目图搜链路打通与评测，**不得对外当作商品主图发布**
- 每张图的来源页面均记录在 `data/商品图片索引.csv` 与 `data/商品图片来源与署名.csv`
- 正式上线前须由商家用真实商品照片按同一命名规则替换
"""
import hashlib
import io
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
IMGDIR = os.path.join(DATA, "商品图片素材库")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_img_cache")
os.makedirs(IMGDIR, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

W = H = 800
RNG = random.Random(20261015)

# ══════════════════════════════════════════════════════════════════
# 1. 每个 SKU「系列」的检索词（主词 + 变体，逐个累加去重）
# ══════════════════════════════════════════════════════════════════
FAMILY_QUERY = {
    # ── 毛绒玩偶 ──
    "WK-BEAR":        ("teddy bear plush toy",             ["stuffed teddy bear", "cute teddy bear soft toy"]),
    "WK-RABBIT":      ("plush bunny rabbit stuffed animal", ["stuffed bunny rabbit toy", "cute rabbit plush"]),
    "WK-DINO":        ("plush dinosaur stuffed animal",    ["stuffed dinosaur toy", "cute dino plush"]),
    "WK-PIG":         ("plush pig stuffed animal",         ["stuffed pig toy", "cute pig plush"]),
    "WK-PENGUIN":     ("plush penguin stuffed animal",     ["stuffed penguin toy", "cute penguin plush"]),
    "WK-DEER":        ("plush deer stuffed animal",        ["stuffed deer toy", "fawn plush toy"]),
    "WK-POLARBEAR":   ("polar bear plush toy",             ["stuffed polar bear", "white bear plush"]),
    "WK-SHIBA":       ("shiba inu plush toy",              ["stuffed shiba dog", "cute shiba plush"]),
    "WK-PANDA":       ("panda plush toy",                  ["stuffed panda toy", "cute panda plush"]),
    "WK-UNICORN":     ("unicorn plush toy",                ["stuffed unicorn", "cute unicorn soft toy"]),
    "WK-SLOTH":       ("sloth plush toy",                  ["stuffed sloth toy", "cute sloth plush"]),
    "WK-SHEEP":       ("sheep lamb plush toy",             ["stuffed lamb toy", "cute sheep plush"]),
    "WK-FOX":         ("fox plush toy",                    ["stuffed fox toy", "cute fox plush"]),
    "WK-OTTER":       ("otter plush toy",                  ["stuffed otter toy", "cute otter plush"]),
    "WK-ELEPHANT":    ("elephant plush toy",               ["stuffed elephant toy", "cute elephant plush"]),
    # ── 挂件 ──
    "WK-BEARK":       ("teddy bear keychain charm",        ["bear plush keyring", "cute bear keychain"]),
    "WK-RABBITK":     ("bunny keychain plush charm",       ["rabbit plush keyring", "cute bunny keychain"]),
    "WK-OCTO":        ("octopus plush toy",                ["stuffed octopus toy", "cute octopus plush"]),
    "WK-BURGER":      ("plush burger toy",                 ["stuffed hamburger toy", "cute burger plush"]),
    "WK-STAR":        ("plush star toy",                   ["stuffed star cushion", "cute star plush"]),
    "WK-SHIBAK":      ("shiba plush keychain",             ["shiba dog keyring", "cute shiba keychain"]),
    "WK-PANDAK":      ("panda plush keychain",             ["panda keyring charm", "cute panda keychain"]),
    "WK-PIGK":        ("pig plush keychain",               ["pig keyring charm", "cute pig keychain"]),
    "WK-DINOK":       ("dinosaur plush keychain",          ["dino keyring charm", "cute dinosaur keychain"]),
    "WK-PENGUINK":    ("penguin plush keychain",           ["penguin keyring charm", "cute penguin keychain"]),
    "WK-CLOVER":      ("four leaf clover keychain",        ["lucky clover charm keyring", "clover pendant charm"]),
    # ── 抱枕 ──
    "WK-BEARP":       ("teddy bear pillow cushion",        ["bear shaped pillow", "cute bear cushion"]),
    "WK-RABBITP":     ("bunny pillow cushion",             ["rabbit shaped pillow", "cute bunny cushion"]),
    "WK-DINOP":       ("dinosaur plush cushion",           ["dinosaur shaped pillow", "dino egg cushion"]),
    "WK-CATP":        ("cat plush pillow cushion",         ["cat shaped pillow", "cute cat cushion"]),
    "WK-SHIBAP":      ("shiba pillow cushion",             ["shiba dog shaped pillow", "cute shiba cushion"]),
    "WK-PANDAP":      ("panda pillow cushion",             ["panda shaped pillow", "cute panda cushion"]),
    "WK-CLOUDP":      ("cloud shaped pillow cushion",      ["cloud plush cushion", "cloud pillow toy"]),
    # ── 搪胶潮玩 ──
    "WK-STARBEAR":    ("astronaut bear figure toy",        ["space bear figurine", "astronaut toy figure"]),
    "WK-MECHRAB":     ("robot rabbit figure toy",          ["mecha bunny figure", "robot rabbit toy"]),
    "WK-TRENDBEAR":   ("designer bear art toy figure",     ["art toy bear figure", "collectible bear figure"]),
    "WK-SPACECAT":    ("space cat figure toy",             ["astronaut cat figurine", "space cat toy"]),
    "WK-ROCKPENGUIN": ("penguin figurine toy",             ["rock penguin figure", "penguin toy figure"]),
    "WK-DESSERTBEAR": ("dessert bear toy figure",          ["cake bear figure toy", "sweet bear figurine"]),
    "WK-CYBERSHIBA":  ("cyberpunk dog figure toy",         ["robot dog figure", "mecha shiba figure"]),
    "WK-GARDENELF":   ("elf figurine toy",                 ["garden elf figure", "cute elf figurine"]),
    "WK-ASTROABBIT":  ("astronaut rabbit figure toy",      ["space bunny figurine", "astronaut rabbit toy"]),
    "WK-DEEPSEAOCTO": ("octopus figure toy",               ["octopus figurine toy", "sea octopus figure"]),
    # ── 盲盒 ──
    "WK-BLINDPLANET": ("blind box toy figure",             ["mystery box toy figure", "blind box collectible"]),
    "WK-BLINDDESSERT": ("dessert toy figure blind box",    ["sweet dessert figurine", "dessert blind box toy"]),
    "WK-BLINDFOREST": ("forest animal figure toy",         ["woodland animal figurine", "forest animal blind box"]),
    "WK-BLINDSPACE":  ("space toy figure blind box",       ["galaxy figurine toy", "space blind box toy"]),
}

# 明显是插画/矢量图而非照片，命中即剔除
NON_PHOTO = [
    "sketch", "drawing", "doodle", "illustration", "illustrated", "painting",
    "cartoon", "clipart", "vector", "logo", "poster", "print", "engraving",
    "watercolor", "comic", "artwork", "line art", "silhouette", "icon",
    "stamp", "pattern", "seamless", "tattoo", "diagram", "chart", "graph",
    "map", "blueprint", "schematic", "coat of arms", "crochet", "knitting",
    "amigurumi", "pattern pdf", "tutorial", "svg",
]

# 标题/URL 里出现这些，说明是书刊扫描件或非商品内容
DOC_JUNK = [
    ".djvu", ".pdf", ".svg", ".gif", "volume ", "number ", "no. ", "issue ",
    "page ", "book", "magazine", "newspaper", "journal", "manuscript",
    "catalog", "catalogue", "cover", "title page", "frontispiece", "letter",
    "document", "archive", "scan", "edition", "chapter", "appendix",
    "encyclopedia", "album", "advertisement", "advert", "wikimedia", "wikipedia",
]

# 电商图床（优先，图片质量与商品属性更贴合）
GOOD_HOSTS = [
    "walmartimages", "media-amazon", "ebayimg", "etsystatic", "alicdn",
    "pinimg", "shopify", "cdn.shopify", "targetimg", "scene7", "temu",
    "shein", "dhresource", "alibaba", "taobaocdn", "jd.com", "360buyimg",
]

VIEWS = ["主图1_白底", "主图2_细节", "主图3_场景", "主图4_包装"]


# ══════════════════════════════════════════════════════════════════
# 2. 网络与缓存
# ══════════════════════════════════════════════════════════════════
def http_get(url, timeout=30, extra=None, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            hdrs = {"User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9"}
            if extra:
                hdrs.update(extra)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                                  # noqa: BLE001
            last = e
            time.sleep(0.7 + i * 0.8)
    raise last


def _cache_path(key):
    return os.path.join(CACHE, hashlib.md5(key.encode("utf-8")).hexdigest()[:16] + ".json")


def _ok_text(text):
    t = (text or "").lower()
    if any(bad in t for bad in NON_PHOTO):
        return False
    if any(bad in t for bad in DOC_JUNK):
        return False
    return True


# ══════════════════════════════════════════════════════════════════
# 3. 候选图来源
# ══════════════════════════════════════════════════════════════════
def from_bing(query, count=35):
    """Bing 图片搜索：真实商品照片直链（主源）。"""
    url = ("https://www.bing.com/images/async?q=" + urllib.parse.quote(query) +
           "&first=0&count={}&mmasync=1".format(count))
    try:
        html = http_get(url, timeout=30, extra={
            "Referer": "https://www.bing.com/images/search?q=" + urllib.parse.quote(query)}).decode("utf-8", "ignore")
    except Exception as e:                                      # noqa: BLE001
        print("    [bing] err:", str(e)[:70])
        return []
    urls = []
    for pat in (r'murl&quot;:&quot;(.*?)&quot;', r'"murl":"(.*?)"'):
        for mm in re.finditer(pat, html):
            u = mm.group(1).replace("\\/", "/")
            if u.startswith("http"):
                urls.append(u)
    out, seen = [], set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        tail = u.rsplit("/", 1)[-1].split("?")[0].lower()
        if not re.search(r"\.(jpe?g|png|webp)$", tail):
            continue
        if any(j in u.lower() for j in DOC_JUNK):
            continue
        host = urllib.parse.urlparse(u).netloc.lower()
        out.append({
            "url": u,
            "page": u,
            "title": tail,
            "author": "见来源页面",
            "license": "来源页面版权（仅作内部评测参考）",
            "source": "Bing 图片搜索",
            "host": host,
            "prefer": any(g in host for g in GOOD_HOSTS),
        })
    # 电商图床优先，其余保持搜索序
    out.sort(key=lambda c: (not c["prefer"],))
    return out


def from_wikimedia(query, limit=30):
    """Wikimedia Commons：CC/公有领域，作为补充源。"""
    url = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search"
           "&gsrsearch=" + urllib.parse.quote(query) +
           "&gsrnamespace=6&gsrlimit={}".format(limit) +
           "&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=900&format=json")
    try:
        d = json.loads(http_get(url, accept=None).decode("utf-8"))
    except Exception as e:                                      # noqa: BLE001
        print("    [wikimedia] err:", str(e)[:70])
        return []
    out = []
    for pid, p in (d.get("query", {}).get("pages", {}) or {}).items():
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime") or ""
        if not mime.startswith("image/") or "svg" in mime:
            continue
        u = ii.get("thumburl") or ii.get("url")
        if not u:
            continue
        if min(ii.get("width") or 0, ii.get("height") or 0) < 400:
            continue
        title = (p.get("title") or "").replace("File:", "")
        if not _ok_text(title):
            continue
        em = ii.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", str((em.get("Artist") or {}).get("value") or "")).strip()
        out.append({
            "url": u,
            "page": ii.get("descriptionurl") or "",
            "title": title[:90],
            "author": artist[:60] or "未标注",
            "license": str((em.get("LicenseShortName") or {}).get("value") or "见来源页"),
            "source": "Wikimedia Commons",
            "host": "commons.wikimedia.org",
            "prefer": False,
        })
    return out


def candidates(family, queries):
    """带磁盘缓存地取候选图：Bing 主源 + Wikimedia 补充，合并去重。"""
    cp = _cache_path("cand3::" + family)
    if os.path.exists(cp):
        try:
            cached = json.load(open(cp, encoding="utf-8"))
            if len(cached) >= 10:
                return cached
        except Exception:                                       # noqa: BLE001
            pass
    got = []
    for q in queries:
        got += from_bing(q, 35)
        if len({c["url"] for c in got}) >= 60:
            break
        time.sleep(0.5)
    if len({c["url"] for c in got}) < 40:
        for q in queries[:2]:
            got += from_wikimedia(q, 30)
            time.sleep(0.3)
    seen, uniq = set(), []
    for c in got:
        if c["url"] in seen or not _ok_text(c.get("title")) or not _ok_text(c["url"]):
            continue
        seen.add(c["url"])
        uniq.append(c)
    RNG.shuffle(uniq)
    uniq.sort(key=lambda c: (not c.get("prefer"),))
    json.dump(uniq, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    return uniq


def download(url):
    """下载并校验为可用图片，返回 (PIL.Image, bytes) 或 (None, None)。"""
    raw_path = os.path.join(CACHE, "img_" + hashlib.md5(url.encode("utf-8")).hexdigest() + ".bin")
    if os.path.exists(raw_path):
        b = open(raw_path, "rb").read()
    else:
        try:
            b = http_get(url, timeout=30, extra={"Referer": url})
        except Exception:                                       # noqa: BLE001
            return None, None
        if len(b) < 12 * 1024 or len(b) > 15 * 1024 * 1024:
            return None, None
        open(raw_path, "wb").write(b)
    try:
        im = Image.open(io.BytesIO(b))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if min(im.size) < 380:
            return None, None
        if im.width * im.height > 40_000_000:
            return None, None
        return im, b
    except Exception:                                           # noqa: BLE001
        return None, None


# ══════════════════════════════════════════════════════════════════
# 4. 图片处理：统一 800x800 的四种视图
# ══════════════════════════════════════════════════════════════════
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:                                   # noqa: BLE001
                continue
    return ImageFont.load_default()


F_TAG = load_font(22)


def _square_crop(im):
    w, h = im.size
    s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))


def _label(im, sku, kind):
    """右下角轻量标注：SKU + 视图类型（便于人工核对与图搜标注）。"""
    if im.mode != "RGB":
        im = im.convert("RGB")
    d = ImageDraw.Draw(im, "RGBA")
    txt = "{}  {}".format(sku, kind)
    box = d.textbbox((0, 0), txt, font=F_TAG)
    tw, th = box[2] - box[0], box[3] - box[1]
    x0, y0 = W - tw - 34, H - th - 32
    d.rectangle([x0 - 10, y0 - 6, W - 14, H - 14], fill=(0, 0, 0, 96))
    d.text((x0, y0), txt, font=F_TAG, fill=(255, 255, 255, 225))
    return im


def treat(im, kind, sku):
    """把原始照片处理成 800x800 的指定视图。"""
    if im.mode != "RGB":
        im = im.convert("RGB")

    if kind == "主图1_白底":
        # 白底展示：等比缩放后居中放在纯白画布上
        c = im.copy()
        c.thumbnail((640, 640), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (W, H), "#FFFFFF")
        canvas.paste(c, ((W - c.width) // 2, (H - c.height) // 2))
        return _label(canvas, sku, "白底")

    if kind == "主图2_细节":
        # 细节特写：居中方形裁切后放大
        c = _square_crop(im)
        side = max(64, int(min(c.size) * 0.62))
        cx, cy = c.width // 2, c.height // 2
        c = c.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2))
        return _label(c.resize((W, H), Image.Resampling.LANCZOS), sku, "细节")

    if kind == "主图3_场景":
        # 场景图：铺满画布（等比裁切），保留环境氛围
        return _label(_square_crop(im).resize((W, H), Image.Resampling.LANCZOS), sku, "场景")

    # 主图4_包装：等比缩放 + 浅灰底 + 细边框，模拟包装展示位
    c = im.copy()
    c.thumbnail((680, 680), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (W, H), "#F4F4F6")
    x, y = (W - c.width) // 2, (H - c.height) // 2
    canvas.paste(c, (x, y))
    d = ImageDraw.Draw(canvas)
    d.rectangle([x - 2, y - 2, x + c.width + 1, y + c.height + 1], outline="#C9C9CF", width=2)
    return _label(canvas, sku, "包装")


# ══════════════════════════════════════════════════════════════════
# 5. 主流程
# ══════════════════════════════════════════════════════════════════
def family_of(sku):
    m = re.match(r"^(WK-[A-Z]+)", sku)
    return m.group(1) if m else sku


def main():
    import pandas as pd

    sku_df = pd.read_csv(os.path.join(DATA, "商品主数据表.csv"),
                         encoding="utf-8-sig", dtype=str).fillna("")

    by_family = {}
    for _, r in sku_df.iterrows():
        by_family.setdefault(family_of(r["SKU编码"]), []).append(r.to_dict())

    missing = [f for f in by_family if f not in FAMILY_QUERY]
    if missing:
        print("!! 缺少检索词配置的系列:", missing)
        raise SystemExit(2)

    index = []
    total = len(sku_df)
    done = 0
    reused = 0

    for fam in sorted(by_family):
        rows = sorted(by_family[fam], key=lambda r: r["SKU编码"])
        query, variants = FAMILY_QUERY[fam]
        need = len(rows) * len(VIEWS)
        print("\n[{}] {} 个 SKU，需要 {} 张  ← 检索: {}".format(fam, len(rows), need, query))

        cands = candidates(fam, [query] + list(variants))
        print("    候选: {} 张".format(len(cands)))

        pool, seen_hash = [], set()
        for c in cands:
            if len(pool) >= need + 6:
                break
            im, raw = download(c["url"])
            if im is None:
                continue
            hh = hashlib.md5(raw).hexdigest()
            if hh in seen_hash:                 # 内容去重
                continue
            seen_hash.add(hh)
            pool.append((im, c))
            time.sleep(0.10)

        print("    下载可用: {} 张".format(len(pool)))
        if not pool:
            print("    !! 无可用图片，跳过")
            continue
        if len(pool) < need:
            reused += need - len(pool)

        cursor = 0
        for r in rows:
            sku, name, cat = r["SKU编码"], r["商品名"], r["类目"]
            outdir = os.path.join(IMGDIR, cat, sku)          # ← 类目 / SKU 两级
            os.makedirs(outdir, exist_ok=True)
            for vi, kind in enumerate(VIEWS):
                im, c = pool[(cursor + vi) % len(pool)]
                out = treat(im.copy(), kind, sku)
                fn = "{}_{}.jpg".format(sku, kind)
                out.save(os.path.join(outdir, fn), "JPEG", quality=88)
                index.append({
                    "SKU编码": sku, "商品名": name, "类目": cat,
                    "图片文件": "{}/{}/{}/{}".format("商品图片素材库", cat, sku, fn),
                    "图片类型": kind.replace("主图", "").split("_")[-1] + "图",
                    "尺寸": "800x800",
                    "生成方式": "爬取相似参考图",
                    "是否占位图": "是",
                    "来源平台": c["source"],
                    "来源页面": c["page"],
                    "作者": c.get("author") or "见来源页面",
                    "授权协议": c.get("license") or "见来源页面",
                    "原图地址": c["url"],
                    "备注": "同品类相似参考图，非本店真实商品；上线前须替换",
                })
            cursor += len(VIEWS)
            done += 1
            print("    [{:>2}/{}] {} {}".format(done, total, sku, name))

    idx = pd.DataFrame(index)
    idx.to_csv(os.path.join(DATA, "商品图片索引.csv"), index=False, encoding="utf-8-sig")

    attr = idx.drop_duplicates(subset=["原图地址"])[
        ["来源平台", "来源页面", "作者", "授权协议", "原图地址"]].copy()
    attr.to_csv(os.path.join(DATA, "商品图片来源与署名.csv"), index=False, encoding="utf-8-sig")

    print("\n" + "=" * 66)
    print("图片总数:", len(idx), "；SKU 数:", total)
    print("复用补充:", reused, "张")
    print("类目目录:", sorted(d for d in os.listdir(IMGDIR)
                              if os.path.isdir(os.path.join(IMGDIR, d))))
    print("索引:", os.path.join(DATA, "商品图片索引.csv"))
    print("署名:", os.path.join(DATA, "商品图片来源与署名.csv"), "共", len(attr), "个来源")


if __name__ == "__main__":
    main()
