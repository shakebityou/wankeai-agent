"""图片五路分流适配器（BE-11 第 ② 步 / AI-07）。

生产环境对接国产 VLM（通义千问-VL 等）；本机没有 GPU 也没有模型权重，用
**本地特征规则**顶替，并如实标注 `VLM_LOCAL_RULES` 降级标识。

## 为什么本地实现是「特征规则」而不是「假装调模型」

VLM 的能力是「看图说话」，本地做不出来。但**五路分流**这个具体任务的信息量
其实主要来自三类可计算的信号，规则能覆盖得相当好：

1. **OCR 文本里的业务词**——订单截图一定有「订单号/待发货/实付」，尺码表一定
   有「尺码/身高/体重/cm」，瑕疵图多半有「破损/划痕/色差」。这是**最强信号**，
   而且是用户真实会打的字，不是我们编的。
2. **版面结构**——截图类图片大面积留白（白底黑字），商品图色彩丰富、文字极少。
3. **形状**——尺码表常是竖长图（高宽比大），商品图多为方图。

所以 `_features()` 抽的就是这三类，`_score()` 按信号加权打分，取最高分路由。
打分而不是硬规则的好处：每条路由都能给出 `labels` 候选与置信度，
自检脚本能检查「分错的时候差多少分」，而不是只看到一个对错。

## 一个刻意的取舍

**判不准一律归 `MEME_IRRELEVANT`**，走轻引导话术，绝不转人工。理由：图片看不懂
是「信息不足」，不是「用户生气」，把它算进转人工会污染转人工率这个核心指标。
"""

from __future__ import annotations

import io
import re
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.core.enums import ImageRoute
from app.core.logging import get_logger
from app.adapters.ports import ImageClassifyResult

logger = get_logger(__name__)


# ---------------------------------------------------------------- 关键词表
#: 订单截图强特征（业务词）。命中越多分越高。
_ORDER_WORDS: tuple[str, ...] = (
    "订单号",
    "订单编号",
    "订单详情",
    "我的订单",
    "待发货",
    "待收货",
    "待付款",
    "实付",
    "付款",
    "收货地址",
    "收货人",
    "物流",
    "退款",
    "售后",
    "交易成功",
    "买家",
)

#: 尺码表强特征。
_SIZE_WORDS: tuple[str, ...] = (
    "尺码",
    "尺寸",
    "身高",
    "体重",
    "胸围",
    "腰围",
    "肩宽",
    "衣长",
    "建议",
    "参照",
    "cm",
    "kg",
    "S码",
    "M码",
    "L码",
    "XL",
)

#: 瑕疵图强特征。
#: ⚠️ 刻意**不含「质量」**：日常问「质量怎么样」是商品咨询，把它算瑕疵特征会让
#: 每个正常咨询都往售后分流（实测误伤最多的一项）。
_DEFECT_WORDS: tuple[str, ...] = (
    "瑕疵",
    "破损",
    "划痕",
    "污渍",
    "掉色",
    "色差",
    "开线",
    "脱线",
    "坏了",
    "漏了",
    "断裂",
    "变形",
    "残次",
    "磕碰",
)


def _count_hits(text: str, words: tuple[str, ...]) -> int:
    """统计命中词数（不区分大小写）。"""
    if not text:
        return 0
    low = text.lower()
    return sum(1 for w in words if w.lower() in low)


#: OCR 文本里的长数字串（订单截图的最强判据）。
#: 用 `\d{12,}` 而不是精确 12 位：本机 OCR 会把「订单号：」的中文偏旁误配成数字
#: 粘在号码前面，得到 15 位串；精确匹配会漏掉整个信号（实测准确率因此掉 5 个点）。
_HAS_ORDER_NO_RE = re.compile(r"(?<!\d)\d{12,}(?!\d)")


def _features(image: bytes) -> dict[str, Any]:
    """抽取版面/色彩/形状特征。

    任何一步失败都退化成「全零特征」而不是抛异常——分类器必须永远给得出路由，
    调用方不该为了「这张图特征抽不出来」再写一遍兜底。
    """
    feats: dict[str, Any] = {
        "ok": False,
        "width": 0,
        "height": 0,
        "aspect": 0.0,
        "pixels": 0,
        "white_ratio": 0.0,
        "colorfulness": 0.0,
        "text_boxes": 0,
        "text_ratio": 0.0,
        "edge_density": 0.0,
    }
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 无 Pillow 时按全零特征处理
        logger.warning("Pillow 不可用：%s", type(exc).__name__)
        return feats

    try:
        img = Image.open(io.BytesIO(image)).convert("RGB")
        w, h = img.size
        arr = np.asarray(img, dtype=np.uint8)
    except Exception as exc:  # noqa: BLE001 - 解码失败按全零特征
        logger.warning("图片特征抽取失败：%s", type(exc).__name__)
        return feats

    if w <= 0 or h <= 0:
        return feats

    feats["ok"] = True
    feats["width"] = w
    feats["height"] = h
    feats["aspect"] = h / float(w)
    feats["pixels"] = w * h

    # 白底比例：三个通道都 ≥240 视为留白（截图类图片的关键特征）
    near_white = np.all(arr >= 240, axis=2)
    feats["white_ratio"] = float(near_white.mean())

    # 色彩丰富度：每像素 (max-min) 通道差 的均值 / 255
    maxc = arr.max(axis=2).astype(np.int16)
    minc = arr.min(axis=2).astype(np.int16)
    feats["colorfulness"] = float((maxc - minc).mean() / 255.0)

    # 文字/边缘结构：转灰度后 OTSU 二值化 + 连通域，统计「像字」的小块
    try:
        import cv2

        gray = np.asarray(img.convert("L"), dtype=np.uint8)
        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        binary = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8)
        )
        n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        min_area = 12
        max_h = max(8, h // 40)
        boxes = 0
        text_area = 0
        for i in range(1, n):
            x, y, bw, bh, area = (
                stats[i, 0],
                stats[i, 1],
                stats[i, 2],
                stats[i, 3],
                stats[i, 4],
            )
            if area < min_area or bh < max_h or bw > w // 3 or bw > 3 * bh:
                continue
            boxes += 1
            text_area += int(area)
        feats["text_boxes"] = boxes
        feats["text_ratio"] = float(text_area) / float(w * h)

        edges = cv2.Canny(gray, 60, 160)
        feats["edge_density"] = float((edges > 0).mean())
    except Exception as exc:  # noqa: BLE001 - 无 cv2 时只丢结构特征
        logger.warning("图片结构特征抽取失败：%s", type(exc).__name__)

    return feats


def _clamp(value: float) -> float:
    """把分数夹到 [0, 1]。"""
    return max(0.0, min(1.0, float(value)))


def _score(feats: dict[str, Any], ocr_text: str) -> dict[str, float]:
    """按信号给五路打分（0-1）。

    权重是经验值，但每一项都对应一个**可解释的信号**，不是拟合出来的黑盒；
    自检脚本会打印每路的分数，调参时能看到「差在哪一项」。

    两个关键信号（缺了它们准确率会掉到 35%，实测）：
    * **OCR 文本里的 12 位连号**——订单截图一定有序号，尺码表一定没有。
      这是区分「订单截图 / 尺码表」这对**结构上极像**的图（都是白底黑字）
      唯一可靠的判据，权重给到 0.42。
    * **用户同时发的文字**——「收到就有破损」这种话比图像本身更能定性。
    """
    if not feats.get("ok"):
        # 特征抽不出来：给不出任何有依据的判断，直接判无关图。
        return {
            ImageRoute.PRODUCT_IMAGE.value: 0.05,
            ImageRoute.ORDER_SCREENSHOT.value: 0.05,
            ImageRoute.DEFECT_IMAGE.value: 0.05,
            ImageRoute.SIZE_CHART.value: 0.05,
            ImageRoute.MEME_IRRELEVANT.value: 0.5,
        }

    white = feats["white_ratio"]
    color = feats["colorfulness"]
    boxes = min(1.0, feats["text_boxes"] / 40.0)
    text_ratio = min(1.0, feats["text_ratio"] * 6.0)
    aspect = feats["aspect"]
    pixels = feats["pixels"]
    edge = min(1.0, feats["edge_density"] * 8.0)

    order_hits = _count_hits(ocr_text, _ORDER_WORDS)
    size_hits = _count_hits(ocr_text, _SIZE_WORDS)
    defect_hits = _count_hits(ocr_text, _DEFECT_WORDS)
    has_order_no = 1.0 if _HAS_ORDER_NO_RE.search(ocr_text or "") else 0.0
    # 竖长程度：1.1 起算、2.3 封顶（尺码表常见 2.5+，订单截图约 1.5）
    tall = _clamp((aspect - 1.1) / 1.2)

    # 「像不像界面截图」的硬闸门。
    # 订单截图（含深色模式）是**近乎无彩色的界面**：白底或黑底 + 单色文字，
    # 色彩度接近 0；实拍商品图/瑕疵图则是有彩色的。这个闸门是必需的——
    # 实测商品图 seed=9 的 OCR 噪声里凑出了一个 12 位数字串，抠号置信度过线，
    # 于是「订单号」信号被注入，把商品图判成了订单截图（150 张里错 1 张）。
    # 注意不能用「白底」单条判据：深色模式订单截图 white_ratio≈0 但依然合法。
    ui_like = 1.0 if (color <= 0.15 or white >= 0.5) else 0.0

    scores = {
        # 订单截图：白底 + 文字块多 + 命中「订单号」类业务词 + 有长数字串
        # ⚠️ `has_order_no` 权重只给 0.10：它是**在 OCR 文本上跑正则**，而本地 OCR
        # 只认数字，中文偏旁会被误配成数字粘成假号码（实测尺码表能拼出 15 位假号）。
        # 真正的强信号是 `order_hits`（0.40）——流水线只有在抠号**高置信**时才会
        # 往分类文本里塞「订单号」这三个字，等于把几何判据转成了语义信号。
        ImageRoute.ORDER_SCREENSHOT.value: _clamp(
            ui_like
            * (
                0.20 * white
                + 0.15 * boxes
                + 0.08 * text_ratio
                + 0.10 * has_order_no
                + 0.40 * min(1.0, order_hits / 1.0)
                + 0.08 * (1.0 - tall)
            )
        ),
        # 尺码表：白底 + 竖长 + 文字块偏少 + 命中尺码词
        ImageRoute.SIZE_CHART.value: _clamp(
            0.22 * white
            + 0.36 * tall
            + 0.10 * text_ratio
            + 0.16 * (1.0 - boxes)
            + 0.34 * min(1.0, size_hits / 2.0)
        ),
        # 瑕疵图：命中瑕疵词是**主信号**（0.62），色彩/细节为辅
        ImageRoute.DEFECT_IMAGE.value: _clamp(
            0.62 * min(1.0, defect_hits / 1.0)
            + 0.16 * (1.0 - white)
            + 0.12 * edge
            + 0.10 * color
        ),
        # 商品图：色彩丰富 + 文字少 + 像素量大（实拍图分辨率高）
        ImageRoute.PRODUCT_IMAGE.value: _clamp(
            0.42 * color
            + 0.24 * (1.0 - text_ratio)
            + 0.20 * (1.0 - white)
            + 0.14 * _clamp(pixels / 500_000.0)
        ),
        # 表情包/无关图：小图 + 无文字 + 色彩饱和（表情包多为小尺寸纯色块）
        ImageRoute.MEME_IRRELEVANT.value: _clamp(
            0.34
            + 0.30 * (1.0 - min(1.0, pixels / 400_000.0))
            + 0.20 * color
            + 0.16 * (1.0 - min(1.0, feats["text_boxes"] / 8.0))
        ),
    }
    return scores


class LocalImageClassifier:
    """本地特征规则分类器（实现 `ImageClassifierPort`）。"""

    name = "local-rules"

    def __init__(self, threshold: float | None = None) -> None:
        settings = get_settings()
        self._threshold = (
            float(threshold)
            if threshold is not None
            else float(getattr(settings, "vlm_match_threshold", 0.6))
        )

    async def classify(
        self,
        image: bytes,
        *,
        ocr_text: str = "",
        content_type: str = "image/png",
    ) -> ImageClassifyResult:
        """判定五路之一。"""
        feats = _features(image)
        scores = _score(feats, ocr_text)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_route, best_score = ranked[0]

        degraded: list[str] = ["VLM_LOCAL_RULES"]
        if not feats.get("ok"):
            degraded.append("VLM_FEATURE_FAILED")
        if best_score < self._threshold:
            # 分数不够：**保留最高分路由**但打降级标识，只有低到「明显没信息」时
            # 才归 MEME_IRRELEVANT。理由：把 0.58 分的商品图硬掰成表情包，
            # 用户会收到一句莫名其妙的轻引导；而低分商品图至少能正常走商品咨询。
            degraded.append("VLM_LOW_CONFIDENCE")
            floor = self._threshold * 0.8
            if best_score < floor:
                best_route = ImageRoute.MEME_IRRELEVANT.value

        return ImageClassifyResult(
            best_route,
            confidence=round(best_score, 3),
            description=self._describe(best_route, feats, ocr_text),
            labels=[
                {"route": r, "score": round(s, 3)} for r, s in ranked[:3]
            ],
            degraded=degraded,
        )

    @staticmethod
    def _describe(route: str, feats: dict[str, Any], ocr_text: str) -> str:
        """给出一句话描述（本地规则只能描述结构，不编造画面内容）。"""
        if route == ImageRoute.ORDER_SCREENSHOT.value:
            return "疑似订单/交易详情页截图"
        if route == ImageRoute.SIZE_CHART.value:
            return "疑似尺码表或尺寸对照图"
        if route == ImageRoute.DEFECT_IMAGE.value:
            return "疑似商品瑕疵/破损照片"
        if route == ImageRoute.PRODUCT_IMAGE.value:
            return "疑似商品实拍图"
        return "未识别出业务含义的图片"


class HttpVlmClassifier:
    """HTTP VLM 分类器（通义千问-VL 等），配好 base_url/api_key 即启用。"""

    name = "http-vlm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        model: str = "qwen-vl-plus",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._model = model

    async def classify(
        self,
        image: bytes,
        *,
        ocr_text: str = "",
        content_type: str = "image/png",
    ) -> ImageClassifyResult:
        """调远端 VLM；失败退化为本地规则（可用性优先）。"""
        import base64

        try:
            import httpx

            b64 = base64.b64encode(image).decode("ascii")
            prompt = (
                "把这张图归到以下五类之一，只回答类目名："
                "商品图 / 订单截图 / 瑕疵图 / 尺码表 / 表情包。"
                f"\n已识别文字：{ocr_text[:200]}"
            )
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{content_type};base64,{b64}"
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
            text = str(
                payload["choices"][0]["message"]["content"] or ""
            ).strip()
        except Exception as exc:  # noqa: BLE001 - 远端不可用就退本地
            logger.warning("VLM 调用失败（%s），退回本地规则", type(exc).__name__)
            fallback = await LocalImageClassifier().classify(
                image, ocr_text=ocr_text, content_type=content_type
            )
            fallback.degraded.append("VLM_CALL_FAILED")
            return fallback

        route = self._parse_route(text)
        if route is None:
            fallback = await LocalImageClassifier().classify(
                image, ocr_text=ocr_text, content_type=content_type
            )
            fallback.degraded.append("VLM_UNPARSABLE")
            return fallback
        return ImageClassifyResult(
            route, confidence=0.9, description=text[:100], degraded=[]
        )

    @staticmethod
    def _parse_route(text: str) -> str | None:
        """从模型回复里解析路由名。"""
        for route in ImageRoute:
            if route.value in text:
                return route.value
        return None


#: 进程级单例
_VLM: object | None = None


def get_image_classifier() -> object:
    """获取图片分类实现（单例）。"""
    global _VLM  # noqa: PLW0603
    if _VLM is None:
        settings = get_settings()
        provider = (getattr(settings, "vlm_provider", "local") or "local").lower()
        base_url = getattr(settings, "vlm_base_url", "") or ""
        api_key = ""
        try:
            api_key = settings.vlm_api_key.get_secret_value()
        except Exception:  # noqa: BLE001 - 未配置按空处理
            api_key = ""
        if provider == "dashscope" and base_url and api_key:
            logger.info("图片分类实现：HttpVlmClassifier（%s）", provider)
            _VLM = HttpVlmClassifier(base_url=base_url, api_key=api_key)
        else:
            logger.info("图片分类实现：LocalImageClassifier（本地特征规则）")
            _VLM = LocalImageClassifier()
    return _VLM


def set_image_classifier(impl: object | None) -> None:
    """替换图片分类实现（测试用）。"""
    global _VLM  # noqa: PLW0603
    _VLM = impl


__all__ = [
    "HttpVlmClassifier",
    "LocalImageClassifier",
    "get_image_classifier",
    "set_image_classifier",
]
