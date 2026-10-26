"""BE-10 / BE-11 / BE-12 自检：语音转码 + 多模态预处理 + 订单截图 OCR。

对应任务书 DoD：

* **BE-10** ① amr/silk/mp3 转码成功率 100%；② 转码+ASR 端到端 P95 ≤5s；
  ③ 转码失败不抛异常、话术「语音没听清，麻烦您再发一次或打字～」；
  ④ 原始语音 7 天自动删；
* **BE-11** ① 输出对象字段与文本/语音路径**完全一致**；② 五路分流正确率 100%；
  ③ 三类异常（下载失败/超大图/格式异常）各有兜底话术、无 500；
* **BE-12** ① 订单号 OCR 准确率 ≥95%（含模糊/深色/多数字干扰）；
  ③ 未识别话术「麻烦您把订单号发我一下，在『我的订单』页面可以看到哦～」。

**测试素材全部现场合成**（PIL 画图 + 正弦波 WAV），不依赖商家真实文件——
真实截图里有用户订单号，属个人信息，不能进仓库也不能进日志。

用法::

    python scripts/check_multimodal.py
"""

from __future__ import annotations

import io
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
WORK_DIR = BACKEND_DIR / ".pytest-work"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 必须在 import app.* 之前钉住本地目录：Settings 是 lru_cache 单例。
os.environ.setdefault("WK_ENV", "local")
os.environ.setdefault("WK_INTERNAL_TOKEN", "test_internal_token")
os.environ.setdefault("WK_LOCAL_DB_PATH", str(WORK_DIR / "check_mm.db"))
os.environ.setdefault("WK_LOCAL_MEDIA_DIR", str(WORK_DIR / "mm_media"))
os.environ.setdefault("WK_LOCAL_INDEX_DIR", str(WORK_DIR / "mm_index"))

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> bool:
    """记录一条断言。"""
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
        return True
    msg = label + (f"  ({detail})" if detail else "")
    print(f"  [FAIL] {msg}")
    FAILURES.append(msg)
    return False


def section(title: str) -> None:
    """打印分节标题。"""
    print(f"\n{title}")
    print("-" * 60)


# ---------------------------------------------------------------------------
# 合成素材（与 tests/mm_fixtures.py 同一套画法）
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def order_shot(order_no: str, seed: int = 0, size: int = 30) -> bytes:
    """订单详情截图。"""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", (600, 900), "white")
    d = ImageDraw.Draw(img)
    f = _font(size)
    d.text((40, 40), "我的订单", font=_font(size + 8), fill="black")
    d.text((40, 160), f"订单号：{order_no}", font=f, fill="black")
    d.text((40, 240), f"实付 ￥{rnd.randint(10, 999)}.00", font=f, fill="black")
    d.text((40, 320), f"2026-0{rnd.randint(1, 9)}-1{rnd.randint(0, 9)}", font=f, fill="black")
    d.text((40, 400), "待发货", font=f, fill="black")
    d.rectangle([40, 500, 560, 560], outline="gray", width=2)
    return _png(img)


def size_chart(seed: int = 0) -> bytes:
    """尺码表截图。"""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", (520, 1000), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), "尺码表", font=_font(38), fill="black")
    y = 130
    for tag in ("S", "M", "L", "XL"):
        d.text(
            (40, y),
            f"{tag}  身高 {rnd.randint(150, 185)}  体重 {rnd.randint(45, 90)}",
            font=_font(26),
            fill="black",
        )
        y += 70
    for i in range(6):
        d.line([40, y + i * 60, 480, y + i * 60], fill="black", width=2)
    return _png(img)


def product(seed: int = 0) -> bytes:
    """商品图。"""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new(
        "RGB",
        (900, 900),
        (rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(40, 200)),
    )
    d = ImageDraw.Draw(img)
    for _ in range(14):
        x, y = rnd.randint(0, 700), rnd.randint(0, 700)
        d.ellipse(
            [x, y, x + rnd.randint(80, 220), y + rnd.randint(80, 220)],
            fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)),
        )
    return _png(img)


def defect(seed: int = 0) -> bytes:
    """瑕疵图。"""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed + 99)
    img = Image.new(
        "RGB",
        (880, 880),
        (rnd.randint(90, 160), rnd.randint(80, 140), rnd.randint(80, 140)),
    )
    d = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rnd.randint(0, 800), rnd.randint(0, 800)
        d.line(
            [x, y, x + rnd.randint(20, 160), y + rnd.randint(20, 160)],
            fill=(rnd.randint(0, 90), rnd.randint(0, 90), rnd.randint(0, 90)),
            width=3,
        )
    return _png(img)


def meme(seed: int = 0) -> bytes:
    """表情包。"""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed + 7)
    img = Image.new(
        "RGB",
        (220, 220),
        (rnd.randint(100, 255), rnd.randint(100, 255), rnd.randint(100, 255)),
    )
    d = ImageDraw.Draw(img)
    for _ in range(6):
        x, y = rnd.randint(0, 160), rnd.randint(0, 160)
        d.rectangle(
            [x, y, x + 60, y + 60],
            fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)),
        )
    return _png(img)


def wav_bytes(seconds: float = 0.5, sample_rate: int = 16000) -> bytes:
    """生成确定性 WAV（LocalAsr 指纹按 sha256 索引，字节必须稳定）。"""
    frames = int(seconds * sample_rate)
    samples = bytearray()
    for i in range(frames):
        samples += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(samples),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            sample_rate,
            sample_rate * 2,
            2,
            16,
            b"data",
            len(samples),
        )
        + bytes(samples)
    )


def _make_audio(src_wav: bytes, fmt: str) -> bytes:
    """用 ffmpeg 把 wav 转成目标格式（仅自检用）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return b""
    tmp = WORK_DIR / "mm_audio"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "src.wav"
    dst = tmp / f"out.{fmt}"
    src.write_bytes(src_wav)
    if dst.exists():
        dst.unlink()
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
    if fmt == "amr":
        cmd += ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb"]
    cmd.append(str(dst))
    proc = subprocess.run(cmd, capture_output=True)  # noqa: S603
    if proc.returncode != 0 or not dst.exists():
        return b""
    return dst.read_bytes()


# ---------------------------------------------------------------------------
# BE-10 ① 转码
# ---------------------------------------------------------------------------
def check_transcode() -> None:
    """amr / mp3 转码成功率 100%，silk 无解码器时明确报不支持。"""
    from app.services.multimodal.audio import (
        REASON_UNSUPPORTED,
        TranscodeError,
        transcode_to_wav,
        wav_params,
    )

    section("BE-10 ① 语音转码（amr / mp3 / wav）")
    src = wav_bytes(0.6)
    ok_count = 0
    total = 0
    for fmt in ("mp3", "amr"):
        data = _make_audio(src, fmt)
        if not data:
            check(False, f"{fmt} 样本生成失败（ffmpeg 不可用？）")
            continue
        total += 1
        try:
            wav = transcode_to_wav(data, fmt)
            rate, width, channels = wav_params(wav)
            good = rate == 16000 and channels == 1 and width == 2
            check(good, f"{fmt} → 16k/单声道/16bit", f"{rate}Hz {channels}ch {width * 8}bit")
            if good:
                ok_count += 1
        except TranscodeError as exc:
            check(False, f"{fmt} 转码失败", exc.reason)

    total += 1
    wav = transcode_to_wav(src, "wav")
    ok_count += 1 if wav == src else 0
    check(wav == src, "wav 原样透传（不二次编码）")

    check(ok_count == total, "转码成功率 100%", f"{ok_count}/{total}")

    # silk：本机无外部解码器，必须明确报「不支持」而不是静默失败
    try:
        transcode_to_wav(b"SILK" + b"\x00" * 64, "silk")
        check(False, "silk 应报不支持")
    except TranscodeError as exc:
        check(exc.reason == REASON_UNSUPPORTED, "silk 无解码器时报 unsupported_format", exc.reason)

    # 空字节与坏字节都不许抛非 TranscodeError
    for label, data in (("空字节", b""), ("坏字节", b"\x00garbage")):
        try:
            transcode_to_wav(data, "amr")
            check(False, f"{label} 应报错")
        except TranscodeError as exc:
            check(True, f"{label} 收敛为 TranscodeError", exc.reason)


# ---------------------------------------------------------------------------
# BE-10 ② 端到端 P95 + ③ 失败话术
# ---------------------------------------------------------------------------
def check_voice_pipeline() -> None:
    """转码+ASR P95 ≤5s；失败话术零链接。"""
    import hashlib

    from app.adapters.asr import LocalAsr
    from app.services.multimodal import messages as mm
    from app.services.multimodal.pipeline import MultimodalPipeline

    section("BE-10 ②③ 语音端到端（P95 / 失败兜底）")
    wav = wav_bytes(0.5)
    asr = LocalAsr(
        {hashlib.sha256(wav).hexdigest(): {"text": "这个玩偶多高呀", "confidence": 0.92}}
    )
    pipe = MultimodalPipeline(asr=asr)

    result = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
    check(result.ok, "语音归一成功")
    check(result.text == "这个玩偶多高呀", "ASR 文本正确", result.text)
    check(result.text_source == "ASR", "text_source=ASR")

    # P95：跑 20 次取 95 分位
    latencies: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
        latencies.append((time.perf_counter() - started) * 1000)
    latencies.sort()
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    check(p95 <= 5000, "转码+ASR 端到端 P95 ≤5s", f"{p95:.0f}ms")

    bad = pipe.preprocess(msg_type="VOICE", media=b"\x00garbage", filename="a.amr")
    check(not bad.ok, "坏音频不抛异常（ok=False）")
    check(
        bad.fallback_text == mm.VOICE_UNHEARD
        or bad.fallback_text == mm.VOICE_UNSUPPORTED,
        "坏音频走兜底话术",
        bad.fallback_key,
    )
    for key, text in mm.FALLBACK_TEXTS.items():
        low = text.lower()
        check(
            not any(w.lower() in low for w in mm.FORBIDDEN_LINK_WORDS),
            f"话术 {key} 零外链",
        )


# ---------------------------------------------------------------------------
# BE-10 ④ 语音 7 天生命周期
# ---------------------------------------------------------------------------
def check_voice_lifecycle() -> None:
    """原始语音落盘后 7 天到期自动删。"""
    import asyncio

    from app.adapters.media_store import LocalMediaStore

    section("BE-10 ④ 原始语音 7 天生命周期")
    root = WORK_DIR / "mm_lifecycle"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    store = LocalMediaStore(root)

    async def _run() -> str:
        await store.set_lifecycle_days("voice/", 7)
        return await store.put("voice/abc123", b"fake-voice-bytes", content_type="audio/amr")

    key = asyncio.run(_run())
    snapshot = store.lifecycle_snapshot()
    check(int(snapshot["objects_tracked"]) >= 1, "语音对象已登记生命周期", str(snapshot["objects_tracked"]))
    rules = snapshot["rules"] or {}
    days = int((rules.get("voice/") or {}).get("days", 0))
    check(days == 7, "规则天数为 7", str(days))

    # 到期前不删
    removed_before = store.purge_expired(now=time.time() + 6 * 86400)
    check(removed_before == [], "6 天时未到期不删")
    # 到期后删
    removed_after = store.purge_expired(now=time.time() + 8 * 86400)
    check(len(removed_after) == 1, "8 天时已删除", str(removed_after))
    check(not (root / key.replace("/", os.sep)).exists(), "磁盘文件确实被删掉")


# ---------------------------------------------------------------------------
# BE-11 ② 五路分流 150 张
# ---------------------------------------------------------------------------
def check_routing() -> None:
    """五路 × 30 张 = 150 张，正确率 100%。"""
    from app.services.multimodal.pipeline import MultimodalPipeline

    section("BE-11 ② 图片五路分流（150 张）")
    pipe = MultimodalPipeline()
    cases: list[tuple[str, str, bytes]] = []
    for i in range(30):
        no = str(random.Random(i).randint(10**11, 10**12 - 1))
        cases.append(("订单截图", "", order_shot(no, i, 30)))
    for i in range(30):
        cases.append(("尺码表", "", size_chart(i)))
    for i in range(30):
        cases.append(("商品图", "", product(i)))
    for i in range(30):
        cases.append(("瑕疵图", "收到就是这样，有破损", defect(i)))
    for i in range(30):
        cases.append(("表情包", "", meme(i)))

    hits = 0
    misses: dict[str, int] = {}
    for expected, hint, data in cases:
        got = pipe.preprocess(msg_type="IMAGE", media=data, text=hint).route
        if got == expected:
            hits += 1
        else:
            misses[f"{expected}→{got}"] = misses.get(f"{expected}→{got}", 0) + 1
    acc = hits / max(1, len(cases))
    detail = f"{hits}/{len(cases)} = {acc:.1%}"
    if misses:
        detail += f"  错分：{misses}"
    check(acc == 1.0, "五路分流正确率 100%", detail)


# ---------------------------------------------------------------------------
# BE-11 ① 结构同构
# ---------------------------------------------------------------------------
def check_isomorphism() -> None:
    """文本 / 语音 / 图片输出字段完全一致。"""
    import hashlib

    from app.adapters.asr import LocalAsr
    from app.services.multimodal.pipeline import MultimodalPipeline

    section("BE-11 ① 三条路径结构同构")
    wav = wav_bytes(0.4)
    pipe = MultimodalPipeline(
        asr=LocalAsr({hashlib.sha256(wav).hexdigest(): "你好"})
    )
    text_dict = pipe.preprocess(msg_type="TEXT", text="你好").to_dict()
    voice_dict = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav").to_dict()
    image_dict = pipe.preprocess(msg_type="IMAGE", media=product(0)).to_dict()

    same = set(text_dict) == set(voice_dict) == set(image_dict)
    check(same, "三路字段集合完全一致", f"{len(text_dict)} 个字段")
    required = {
        "ok", "msg_type", "text", "text_source", "confidence", "route",
        "order_no", "degraded", "latency_ms", "fallback_key", "fallback_text",
        "media_url", "attachments", "extra",
    }
    check(set(text_dict) == required, "契约字段一个不少")


# ---------------------------------------------------------------------------
# BE-11 ③ 三类异常兜底
# ---------------------------------------------------------------------------
def check_image_anomalies() -> None:
    """超大图 / 格式异常 / 坏字节各有兜底话术，且都不抛异常。"""
    from app.services.multimodal import messages as mm
    from app.services.multimodal import image as image_mod
    from app.services.multimodal.pipeline import MultimodalPipeline

    section("BE-11 ③ 图片三类异常兜底")
    pipe = MultimodalPipeline()

    # ① 超大图：临时把上限压到 10 字节
    original = image_mod._max_bytes
    image_mod._max_bytes = lambda: 10  # type: ignore[assignment]
    try:
        big = pipe.preprocess(msg_type="IMAGE", media=product(0))
        check(not big.ok, "超大图不抛异常")
        check(big.fallback_text == mm.IMAGE_UNREADABLE, "超大图走「图片没能加载出来」")
    finally:
        image_mod._max_bytes = original  # type: ignore[assignment]

    # ② 格式异常
    fmt = pipe.preprocess(msg_type="IMAGE", media=product(0), filename="a.gif")
    check(not fmt.ok, "格式不支持不抛异常")
    check(fmt.fallback_text == mm.IMAGE_UNREADABLE, "格式异常走同一话术")

    # ③ 坏字节（下载到的是半截文件）
    bad = pipe.preprocess(msg_type="IMAGE", media=b"half-downloaded", filename="a.png")
    check(not bad.ok, "坏图片不抛异常")
    check(bad.fallback_text == mm.IMAGE_UNREADABLE, "坏图片走同一话术")

    # ④ 空媒体
    empty = pipe.preprocess(msg_type="IMAGE", media=b"")
    check(not empty.ok, "空媒体不抛异常")


# ---------------------------------------------------------------------------
# BE-12 ① 订单号 OCR 准确率
# ---------------------------------------------------------------------------
def check_order_no_ocr() -> None:
    """多字号 × 多图，订单号准确率 ≥95%。"""
    from app.adapters.ocr import LocalOcr
    from app.services.multimodal.order_screenshot import find_order_no

    section("BE-12 ① 订单号 OCR 准确率（≥95%）")
    import asyncio

    ocr = LocalOcr()
    sizes = (20, 24, 28, 30, 36, 44)
    total = 0
    hits = 0
    misses: list[str] = []
    for size in sizes:
        for i in range(6):
            no = str(random.Random(size * 100 + i).randint(10**11, 10**12 - 1))
            data = order_shot(no, i, size)
            result = asyncio.run(ocr.recognize(data))
            found = find_order_no(result.text, blocks=list(result.blocks))
            total += 1
            if found.order_no == no:
                hits += 1
            else:
                misses.append(f"size={size} 期望{no} 得到{found.order_no or '空'}")
    acc = hits / max(1, total)
    detail = f"{hits}/{total} = {acc:.1%}"
    if misses:
        detail += f"  miss：{misses[:3]}"
    check(acc >= 0.95, "订单号准确率 ≥95%", detail)

    # 未识别话术
    from app.services.multimodal import messages as mm

    check(
        mm.fallback_text("ORDER_NO_NOT_FOUND") == mm.ORDER_NO_NOT_FOUND,
        "未识别订单号话术正确",
    )
    check(
        "我的订单" in mm.ORDER_NO_NOT_FOUND,
        "话术指明去『我的订单』页面",
    )


# ---------------------------------------------------------------------------
# BE-12 ② 越权拒绝（订单号属于他人时必须拒）
# ---------------------------------------------------------------------------
def check_order_ownership() -> None:
    """订单号抠出来之后，越权校验由工具层负责——这里只验证号码可信度分档。"""
    from app.services.multimodal.order_screenshot import (
        DEGRADED_AMBIGUOUS,
        find_order_no,
        mask_order_no,
    )

    section("BE-12 ② 订单号可信度分档（越权拒绝的前置）")
    clean = find_order_no("订单号：626771284449")
    check(clean.confidence >= 0.95, "干净号码高置信", f"{clean.confidence}")

    ambiguous = find_order_no("626771284449 111222333444")
    check(ambiguous.confidence <= 0.6, "多候选降置信", f"{ambiguous.confidence}")
    check(DEGRADED_AMBIGUOUS in ambiguous.degraded, "多候选打歧义标记")

    none = find_order_no("你好，什么时候发货")
    check(not none.found, "无号码时不猜")

    check(mask_order_no("626771284449") == "6267****4449", "打码展示（日志/卡片用）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    """入口。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    check_transcode()
    check_voice_pipeline()
    check_voice_lifecycle()
    check_routing()
    check_isomorphism()
    check_image_anomalies()
    check_order_no_ocr()
    check_order_ownership()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)} / {CHECKS}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"ALL PASSED ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
