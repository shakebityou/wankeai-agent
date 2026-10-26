"""多模态测试的合成图 / 合成音频工厂。

真实图片素材不宜进仓库（体积 + 隐私），但**五路分流必须用真图验证**——
只 mock 分类器等于什么都没测。所以这里用 PIL 现场画合成图：
订单截图、尺码表、商品图、瑕疵图、表情包各一个生成函数，
`make_cases()` 批量产出带标注的用例集。

刻意保持与探针脚本（`.pytest-work/probe_mm.py`）同一套画法，
这样测试断言的数字能和自检脚本 `scripts/check_multimodal.py` 对齐。
"""

from __future__ import annotations

import io
import math
import random
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.enums import ImageRoute

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


def font(size: int):
    """取中文字体（找不到就退化成 PIL 内置位图字体）。"""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - 字体缺失不该让测试崩
            continue
    return ImageFont.load_default()


def to_png(img: Image.Image) -> bytes:
    """PIL 图像 → PNG 字节。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def order_shot(order_no: str, seed: int = 0, size: int = 30) -> bytes:
    """订单详情截图（白底 + 12 位订单号 + 金额/日期/状态等干扰数字）。"""
    rnd = random.Random(seed)
    img = Image.new("RGB", (600, 900), "white")
    d = ImageDraw.Draw(img)
    f = font(size)
    d.text((40, 40), "我的订单", font=font(size + 8), fill="black")
    d.text((40, 160), f"订单号：{order_no}", font=f, fill="black")
    d.text((40, 240), f"实付 ￥{rnd.randint(10, 999)}.00", font=f, fill="black")
    d.text(
        (40, 320),
        f"2026-0{rnd.randint(1, 9)}-1{rnd.randint(0, 9)}",
        font=f,
        fill="black",
    )
    d.text((40, 400), "待发货", font=f, fill="black")
    d.rectangle([40, 500, 560, 560], outline="gray", width=2)
    return to_png(img)


def size_chart(seed: int = 0) -> bytes:
    """尺码表截图（竖长比例 + 大量「身高/体重」数字，用来考验订单号误判）。"""
    rnd = random.Random(seed)
    img = Image.new("RGB", (520, 1000), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), "尺码表", font=font(38), fill="black")
    y = 130
    for tag in ("S", "M", "L", "XL"):
        d.text(
            (40, y),
            f"{tag}  身高 {rnd.randint(150, 185)}  体重 {rnd.randint(45, 90)}",
            font=font(26),
            fill="black",
        )
        y += 70
    for i in range(6):
        d.line([40, y + i * 60, 480, y + i * 60], fill="black", width=2)
    return to_png(img)


def product(seed: int = 0) -> bytes:
    """商品图（高饱和度 + 无文字，靠色彩度判别）。"""
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
    return to_png(img)


def defect(seed: int = 0) -> bytes:
    """瑕疵图（暗色 + 高频划痕，靠边缘密度判别）。"""
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
    return to_png(img)


def meme(seed: int = 0) -> bytes:
    """表情包（小尺寸 + 低文字密度）。"""
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
    return to_png(img)


def make_cases(
    n: int = 8, size: int = 30
) -> list[tuple[str, str, str, bytes]]:
    """批量生成带标注的用例。

    Returns:
        `(期望路由, 附带文字, 期望订单号, 图片字节)` 四元组列表。
    """
    cases: list[tuple[str, str, str, bytes]] = []
    for i in range(n):
        no = str(random.Random(i).randint(10**11, 10**12 - 1))
        cases.append((ImageRoute.ORDER_SCREENSHOT.value, "", no, order_shot(no, i, size)))
    for i in range(n):
        cases.append((ImageRoute.SIZE_CHART.value, "", "", size_chart(i)))
    for i in range(n):
        cases.append((ImageRoute.PRODUCT_IMAGE.value, "", "", product(i)))
    for i in range(n):
        cases.append(
            (ImageRoute.DEFECT_IMAGE.value, "收到就是这样，有破损", "", defect(i))
        )
    for i in range(n):
        cases.append((ImageRoute.MEME_IRRELEVANT.value, "", "", meme(i)))
    return cases


# --------------------------------------------------------------- 音频
def wav_bytes(
    *, seconds: float = 1.0, sample_rate: int = 16000, freq: float = 440.0
) -> bytes:
    """生成一段单声道 16bit PCM WAV（正弦波）。

    手写 WAV 头而不是用 wave 模块，是为了让字节**完全确定**——
    `LocalAsr` 的 fixture 表按 `sha256(wav)` 索引，字节一变就命不中。
    """
    frames = int(seconds * sample_rate)
    samples = bytearray()
    for i in range(frames):
        value = int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
        samples += struct.pack("<h", value)
    data = bytes(samples)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
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
        len(data),
    )
    return header + data


def _sin(x: float) -> float:
    """泰勒级数近似 sin（避免 import math 后在闭包里重复调用开销）。"""
    import math

    return math.sin(x)


def write_file(path: Path, data: bytes) -> Path:
    """把字节写到磁盘并返回路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


__all__ = [
    "defect",
    "font",
    "make_cases",
    "meme",
    "order_shot",
    "product",
    "size_chart",
    "to_png",
    "wav_bytes",
    "write_file",
]
