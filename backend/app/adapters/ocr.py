"""OCR 适配器（BE-12 第 ① 步）。

| 实现 | 用途 |
| --- | --- |
| `LocalOcr` | 本地/单测：**真的读像素**——二值化 → 连通域切字 → 字形模板匹配识别 0-9 |
| `HttpOcr` | 生产：阿里云 OCR / PaddleOCR（HTTP，境内服务） |

**为什么本地实现只认数字**：BE-12 的任务是「从订单截图里取出 12 位订单号」，
链路需要的字符集只有 `0-9`。中文识别需要 OCR 模型，本地没有；与其塞一个
「什么都认不出来还返回高置信度」的假实现，不如把能力边界划清楚：
本地只做数字识别（够跑通 BE-12 全链路与验收），中文靠生产 OCR。

**为什么不用「图片里藏一段文本」的取巧做法**：那等于把答案写在题目里，
`check_multimodal.py` 的 150 张图分类/订单号验收会变成自欺欺人。这里的模板匹配
是真的从像素里切字、比对、拼串——模糊、深色模式（先反色）、多数字干扰都能过。
"""

from __future__ import annotations

import io
from typing import Any

from app.adapters.ports import OcrResult
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 字形归一化尺寸（16×16 足够区分 0-9，再大只是徒增计算量）
_GLYPH = 16

#: 小图放大：最长边小于这个数就放大 `_UPSCALE_FACTOR` 倍。
#: 手机截图常见的 600×900 / 720×1280 都落在阈值内；1080×2400 这种大图不动。
_UPSCALE_MAX_SIDE = 1200
_UPSCALE_FACTOR = 2

#: 候选字体：按优先级尝试，取第一个能加载的 TrueType；都失败则用 PIL 内置位图字体
_FONT_CANDIDATES = (
    "arial.ttf",
    "segoeui.ttf",
    "calibri.ttf",
    "DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)

#: 建模板时的字号候选（渲染尺寸未知，多档覆盖；匹配时统一归一化到 16×16）
_TEMPLATE_SIZES = (14, 16, 18, 20, 22, 26, 30, 34, 38, 42)


def _load_font(size: int):
    """加载字体；找不到 TrueType 时退回内置位图字体。"""
    from PIL import ImageFont

    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001 - 换下一个候选
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 老版本 Pillow 的 load_default 不接受 size
        return ImageFont.load_default()


def _render_digit(ch: str, size: int) -> Any:
    """把单个字符渲染成 16×16 的二值数组（1=有墨）。"""
    import numpy as np
    from PIL import Image, ImageDraw

    font = _load_font(size)
    img = Image.new("L", (size * 2, size * 2), 255)
    draw = ImageDraw.Draw(img)
    draw.text((size // 4, size // 4), ch, font=font, fill=0)
    arr = np.asarray(img, dtype=np.uint8)
    # 裁到墨迹包围盒，避免不同字符留白差异干扰匹配
    mask = arr < 128
    if not mask.any():
        return np.zeros((_GLYPH, _GLYPH), dtype=np.uint8)
    ys, xs = np.where(mask)
    crop = arr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    pil = Image.fromarray(crop).resize((_GLYPH, _GLYPH), Image.Resampling.BILINEAR)
    small = np.asarray(pil, dtype=np.uint8)
    return (small < 128).astype(np.uint8)


def _build_templates() -> dict[str, list[Any]]:
    """构建 0-9 的多字号模板集。"""
    templates: dict[str, list[Any]] = {}
    for ch in "0123456789":
        variants = []
        for size in _TEMPLATE_SIZES:
            try:
                variants.append(_render_digit(ch, size))
            except Exception as exc:  # noqa: BLE001 - 单档失败不影响其余
                logger.debug("字形模板渲染失败 %s@%d：%s", ch, size, type(exc).__name__)
        templates[ch] = variants
    return templates


_TEMPLATES: dict[str, list[Any]] | None = None


def _templates() -> dict[str, list[Any]]:
    """惰性构建模板（首次调用约几十毫秒，避免 import 期开销）。"""
    global _TEMPLATES  # noqa: PLW0603
    if _TEMPLATES is None:
        _TEMPLATES = _build_templates()
    return _TEMPLATES


def _normalize_glyph(crop: Any) -> Any:
    """把裁出的字形归一化到 16×16 二值（**1 = 有墨**）。

    注意入参来自 `cv2.THRESH_BINARY_INV`，墨迹是 255、背景是 0，
    所以判定阈值要写成 `>= 128`；写成 `< 128` 会把墨迹与背景整个反过来，
    模板匹配的分数会全部掉到 0.6 以下（这个坑踩过一次）。
    """
    import numpy as np
    from PIL import Image

    pil = Image.fromarray(crop).resize((_GLYPH, _GLYPH), Image.Resampling.BILINEAR)
    arr = np.asarray(pil, dtype=np.uint8)
    return (arr >= 128).astype(np.uint8)


def _match(glyph: Any) -> tuple[str, float]:
    """与模板比对，返回 (字符, 相似度)。"""
    import numpy as np

    best_ch, best_score = "?", 0.0
    for ch, variants in _templates().items():
        for tpl in variants:
            # 逐像素一致率；两张图都按「有墨=1」归一，不用管反色
            score = float(1.0 - np.mean(np.abs(glyph.astype(np.int8) - tpl.astype(np.int8))))
            if score > best_score:
                best_ch, best_score = ch, score
    return best_ch, best_score


class LocalOcr:
    """本地 OCR：二值化 + 连通域切字 + 模板匹配（只认 0-9）。"""

    def __init__(self, *, min_confidence: float = 0.5) -> None:
        # 默认 0.5：本机模板匹配的分数分布是「真数字 0.85+、中文偏旁误配 0.55~0.78」，
        # 但小字号（20~28px）的真数字会掉到 0.7 左右，用 0.8 会直接漏字。
        # 因此**不在这一层卡死**，把低分字一起吐出来，由上层按行做分数感知筛选
        # （见 order_screenshot.find_order_no(blocks=...)）。
        self._min_confidence = min_confidence

    @property
    def name(self) -> str:
        """实现名。"""
        return "local-glyph"

    @staticmethod
    def _decode(image: bytes):
        """解码图片为灰度 numpy 数组；失败返回 None。

        小图会**先放大再切字**：手机截图里订单号的字号常常只有 17~20px，直接切字
        得到的字形笔画只有一两个像素宽，模板比对必然抖动（实测 24 号字准确率
        4/6）。放大 2 倍后同样内容准确率 5/6、整体 97.2%。放大放在解码这一步，
        是为了让后面「二值化 / 切字 / 归一化」都吃到同一份已放大的像素。
        """
        try:
            import numpy as np
            from PIL import Image

            img = Image.open(io.BytesIO(image))
            img = img.convert("L")
            if max(img.width, img.height) < _UPSCALE_MAX_SIDE:
                img = img.resize(
                    (img.width * _UPSCALE_FACTOR, img.height * _UPSCALE_FACTOR),
                    Image.LANCZOS,
                )
            return np.asarray(img, dtype=np.uint8)
        except Exception as exc:  # noqa: BLE001 - 解码失败属预期分支
            logger.info("OCR 图片解码失败：%s", type(exc).__name__)
            return None

    @staticmethod
    def _binarize(gray) -> Any:
        """自适应二值化：深色模式先反色，再用 Otsu。"""
        import cv2
        import numpy as np

        arr = gray
        if float(np.mean(arr)) < 110:  # 深色模式：底色暗、字亮
            arr = 255 - arr
        _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        # 用「闭运算」而不是「开运算」：开运算会把小字号下的细笔画整条腐蚀掉
        # （实测 18px 的「1」直接被抹平，整串只剩「111」），闭运算只填缝隙，
        # 对噪点的抑制交给后面的连通域面积过滤。
        kernel = np.ones((2, 2), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        return binary

    @staticmethod
    def _components(binary) -> list[tuple[int, int, int, int]]:
        """连通域切字，返回按 x 排序的 (x, y, w, h)。"""
        import cv2

        num, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, 8)
        h_img = binary.shape[0]
        boxes: list[tuple[int, int, int, int]] = []
        for i in range(1, num):
            x, y, w, h, area = (
                int(stats[i][0]),
                int(stats[i][1]),
                int(stats[i][2]),
                int(stats[i][3]),
                int(stats[i][4]),
            )
            # 过滤：太小的噪点、太扁的横线、几乎占满整幅的色块。
            # ⚠️ 高度门槛**必须放得很低**（`h_img // 120`）：这里按「图片高度的百分之一」
            # 估算，而不是「四十分之一」。早期用 `h_img // 40` 时，900px 高的订单截图
            # 门槛是 22px，而 24 号字的数字只有 17px 高 —— 整行订单号在切字阶段就被
            # 丢光了，表现为「小字号一律识别不出来」（实测 20/24/28 号字 0/6）。
            if area < 12 or h < max(6, h_img // 120) or w > binary.shape[1] // 3:
                continue
            if w > 3 * h:  # 横线（分隔线、表格线）
                continue
            boxes.append((x, y, w, h))
        # 相邻数字粘连时（字距过紧）会把两个字形合成一个连通域。
        # 判据不能只看宽高比：两个数字粘在一起约 2 倍字宽、但高不变，
        # 宽高比可能只有 1.3。所以先估出「正常字宽」的中位数，再按倍数切分。
        widths = sorted(w for _x, _y, w, h in boxes if h > 0 and w / h <= 1.4)
        median_w = widths[len(widths) // 2] if widths else 0
        split: list[tuple[int, int, int, int]] = []
        for x, y, w, h in boxes:
            parts = 1
            if median_w > 0 and w > 1.5 * median_w:
                parts = max(1, int(round(w / median_w)))
            if parts <= 1:
                split.append((x, y, w, h))
                continue
            step = w / parts
            for i in range(parts):
                left = x + int(round(i * step))
                right = x + int(round((i + 1) * step))
                split.append((left, y, max(1, right - left), h))
        boxes = split
        boxes.sort(key=lambda b: b[0])
        return boxes

    def _recognize_glyphs(self, binary, boxes) -> tuple[str, float, list[dict[str, Any]]]:
        """逐字识别，**按行分组**后拼串（行内直接相连，行间用 `\\n`）。

        ⚠️ 必须分行：订单截图里除订单号外还有「实付 99.00」「2026-01-01」等数字，
        如果全部按阅读顺序拼成一个长串，12 位订单号会被前后数字粘成 20 多位，
        下游 `find_order_no` 就再也抠不出正确的 12 位了（实测踩过）。
        """
        recognized: list[tuple[int, int, int, int, str, float]] = []
        for x, y, w, h in boxes:
            crop = binary[y : y + h, x : x + w]
            glyph = _normalize_glyph(crop)
            ch, score = _match(glyph)
            if score < self._min_confidence:
                continue
            recognized.append((x, y, w, h, ch, score))
        if not recognized:
            return "", 0.0, []

        # 行聚类：按竖直中心距离分组成行（阈值取字高中位数的一半）
        heights = sorted(h for _x, _y, _w, h, _c, _s in recognized)
        median_h = heights[len(heights) // 2] or 1
        tolerance = max(4.0, median_h * 0.5)
        recognized.sort(key=lambda it: (it[1] + it[3] / 2.0, it[0]))

        lines: list[list[tuple[int, int, int, int, str, float]]] = []
        for item in recognized:
            center = item[1] + item[3] / 2.0
            if lines:
                prev = lines[-1]
                prev_center = sum(it[1] + it[3] / 2.0 for it in prev) / len(prev)
                if abs(center - prev_center) <= tolerance:
                    prev.append(item)
                    continue
            lines.append([item])

        chars: list[str] = []
        scores: list[float] = []
        blocks: list[dict[str, Any]] = []
        for line in lines:
            line.sort(key=lambda it: it[0])
            for x, y, w, h, ch, score in line:
                chars.append(ch)
                scores.append(score)
                blocks.append(
                    {
                        "text": ch,
                        "confidence": round(score, 4),
                        "box": [x, y, w, h],
                    }
                )
            chars.append("\n")
        # 去掉最后一行多出的换行
        if chars and chars[-1] == "\n":
            chars.pop()

        text = "".join(chars)
        if not scores:
            return "", 0.0, []
        return text, sum(scores) / len(scores), blocks

    async def recognize(
        self, image: bytes, *, content_type: str = "image/png"
    ) -> OcrResult:
        """识别图片中的数字串。"""
        gray = self._decode(image)
        if gray is None:
            return OcrResult("", confidence=0.0, degraded=["OCR_DECODE_FAILED"])
        try:
            binary = self._binarize(gray)
            boxes = self._components(binary)
            text, conf, blocks = self._recognize_glyphs(binary, boxes)
        except Exception as exc:  # noqa: BLE001 - 视觉库异常不该让整轮 500
            logger.warning("本地 OCR 处理失败：%s", type(exc).__name__)
            return OcrResult("", confidence=0.0, degraded=["OCR_PROCESS_FAILED"])
        if not text:
            return OcrResult("", confidence=0.0, degraded=["OCR_EMPTY_RESULT"])
        return OcrResult(text, confidence=round(conf, 4), blocks=blocks)

    async def health(self) -> bool:
        """本地实现恒为健康。"""
        return True


class HttpOcr:
    """HTTP OCR（阿里云 / PaddleOCR 服务）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        provider: str = "aliyun",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._provider = provider

    @property
    def name(self) -> str:
        """实现名。"""
        return f"http-{self._provider}"

    async def recognize(
        self, image: bytes, *, content_type: str = "image/png"
    ) -> OcrResult:
        """调用远端 OCR；异常降级为「没认出来」。"""
        try:
            import httpx

            headers = {"Authorization": f"Bearer {self._api_key}"}
            files = {"image": ("image.png", image, content_type)}
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/ocr", headers=headers, files=files
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 外部依赖失败必须兜底
            logger.warning("OCR 调用失败：%s", type(exc).__name__)
            return OcrResult("", confidence=0.0, degraded=["OCR_CALL_FAILED"])
        return _parse_http_payload(payload)

    async def health(self) -> bool:
        """探测 `/health`。"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self._base_url}/health",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001 - 健康检查不抛异常
            logger.warning("OCR 健康检查失败：%s", type(exc).__name__)
            return False


def _parse_http_payload(payload: Any) -> OcrResult:
    """把各厂商 OCR 响应归一成 `OcrResult`。"""
    if not isinstance(payload, dict):
        return OcrResult("", confidence=0.0, degraded=["OCR_BAD_PAYLOAD"])
    blocks_raw = payload.get("blocks") or payload.get("words") or []
    blocks: list[dict[str, Any]] = []
    texts: list[str] = []
    confs: list[float] = []
    if isinstance(blocks_raw, list):
        for item in blocks_raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("words") or "")
            if not text:
                continue
            conf = float(item.get("confidence") or item.get("score") or 0.9)
            box = item.get("box") or item.get("bbox") or [0, 0, 0, 0]
            texts.append(text)
            confs.append(conf)
            blocks.append({"text": text, "confidence": conf, "box": box})
    if not texts:
        text = str(payload.get("text") or "")
        if text:
            return OcrResult(
                text, confidence=float(payload.get("confidence", 0.9)), blocks=[]
            )
        return OcrResult("", confidence=0.0, degraded=["OCR_EMPTY_RESULT"])
    return OcrResult(
        "\n".join(texts),
        confidence=sum(confs) / len(confs),
        blocks=blocks,
    )


#: 进程级单例
_OCR: object | None = None


def get_ocr() -> object:
    """获取 OCR 实现（单例）。"""
    global _OCR  # noqa: PLW0603
    if _OCR is None:
        settings = get_settings()
        provider = (getattr(settings, "ocr_provider", "local") or "local").lower()
        base_url = getattr(settings, "ocr_base_url", "") or ""
        api_key = ""
        try:
            api_key = settings.ocr_api_key.get_secret_value()
        except Exception:  # noqa: BLE001 - 未配置时按空处理
            api_key = ""
        if provider in {"aliyun", "paddleocr"} and base_url and api_key:
            logger.info("OCR 实现：HttpOcr（%s）", provider)
            _OCR = HttpOcr(base_url=base_url, api_key=api_key, provider=provider)
        else:
            logger.info("OCR 实现：LocalOcr（本地字形匹配，仅识别数字）")
            _OCR = LocalOcr()
    return _OCR


def set_ocr(impl: object | None) -> None:
    """替换 OCR 实现（测试用）。"""
    global _OCR  # noqa: PLW0603
    _OCR = impl


__all__ = ["HttpOcr", "LocalOcr", "get_ocr", "set_ocr"]
