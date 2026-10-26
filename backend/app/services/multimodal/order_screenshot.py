"""订单截图 OCR → 订单号（BE-12）。

只做一件事：**从 OCR 文本里把 12 位订单号抠出来**，然后交给 BE-07 订单工具。
不查订单、不做归属校验（归属校验在订单工具里做，那才是唯一的鉴权点）。

## 为什么要做「字符纠正」

真实 OCR 输出不是干净的纯数字。订单号在截图里常被识别成：

* 带分隔：`1234 5678 9012`、`1234-5678-9012`、换行拆成两行；
* 形近字混淆：`O/o`→`0`、`I/l/|`→`1`、`S`→`5`、`B`→`8`、`Z`→`2`、`G`→`6`；
* 混进前后缀：`订单号：`、`No.`、`#`。

所以流程是：**先去噪取候选窗口 → 再做形近字纠正 → 按证据强度给置信度**。
置信度分档很重要，因为纠正过的号码可能错一位，而错一位就会查到别人的订单——
宁可让用户确认一眼，也不能拿不准的号码去查。

## 一个刻意的保守设计

`find_order_no()` 默认**只在候选唯一时**才返回高置信度；出现多个 12 位候选
就降置信度并带上 `ORDER_NO_AMBIGUOUS`，由 pipeline 决定是否先跟用户确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 订单号长度（商家口径固定 12 位，见 `app/services/tools/logistics.py`）。
ORDER_NO_LENGTH = 12

#: 干净的 12 位连号（最高置信）。
_CLEAN_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")

#: 允许分隔符的 12 位（空格/横线/点/换行）。
_SEPARATED_RE = re.compile(
    r"(?<!\d)(\d{2,6})[\s\-–—_.·]{1,3}(\d{2,6})[\s\-–—_.·]{1,3}(\d{2,6})(?!\d)"
)

#: 形近字 → 数字（只在「够长的一串准数字」里做替换，避免把正常文字改成数字）。
_LOOKALIKE: dict[str, str] = {
    "o": "0",
    "O": "0",
    "D": "0",
    "Q": "0",
    "i": "1",
    "I": "1",
    "l": "1",
    "|": "1",
    "S": "5",
    "s": "5",
    "B": "8",
    "Z": "2",
    "z": "2",
    "G": "6",
    "b": "6",
    "g": "9",
    "q": "9",
}

#: 形近字候选串：至少 10 个「数字或形近字」连在一起才尝试纠正。
_LOOSE_RE = re.compile(
    r"[0-9OoDQiIl|SsBZzGbgq]{10,16}"
)

#: 订单号前后常见的标签（用于给置信度加权，不影响是否能抠出来）。
_CONTEXT_WORDS: tuple[str, ...] = (
    "订单号",
    "订单编号",
    "订单",
    "单号",
    "No.",
    "NO.",
    "编号",
)

#: 降级标识。
DEGRADED_AMBIGUOUS = "ORDER_NO_AMBIGUOUS"
DEGRADED_CORRECTED = "ORDER_NO_CORRECTED"
DEGRADED_LOW_SCORE = "ORDER_NO_LOW_SCORE"

#: 走「按行分组 + 分数感知滑窗」时，单字最低分（低于此不参与滑窗）。
#: 本机模板匹配的分数分布：真数字 0.85+，小字号 0.7 左右，中文偏旁误配 0.55~0.78。
_GLYPH_FLOOR = 0.5

#: 一个 12 位窗口的**平均**分数门槛。低于此认为这一行不是订单号行。
_WINDOW_FLOOR = 0.6


@dataclass(slots=True)
class OrderNumberResult:
    """订单号提取结果。

    Attributes:
        order_no: 12 位订单号，没找到为 `""`。
        confidence: 0-1，见模块 docstring 的分档说明。
        candidates: 全部候选（去重后），排障与用户确认用。
        corrected: 是否做过形近字纠正。
        degraded: 降级标识。
    """

    order_no: str = ""
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)
    corrected: bool = False
    degraded: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        """是否抠到了订单号。"""
        return bool(self.order_no)

    def to_dict(self) -> dict[str, Any]:
        """转字典（写进消息 extra / 上下文卡片）。"""
        return {
            "order_no": self.order_no,
            "confidence": round(self.confidence, 3),
            "corrected": self.corrected,
            "degraded": list(self.degraded),
        }


def _has_context(text: str) -> bool:
    """文本里是否有「订单号」类标签。"""
    if not text:
        return False
    low = text.lower()
    return any(w.lower() in low for w in _CONTEXT_WORDS)


def _correct(token: str) -> str:
    """形近字纠正成纯数字串。"""
    return "".join(_LOOKALIKE.get(ch, ch) for ch in token)


def _group_lines(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把 OCR 字符块按「竖直中心」聚成行。

    单靠文本拼串是不行的：订单截图里「实付 99.00」「2026-01-01」的数字会和订单号
    粘成一个长串，12 位窗口就滑到别的地方去了。按几何位置分行，每行独立滑窗。
    """
    items = [b for b in blocks if b.get("text") and isinstance(b.get("box"), (list, tuple))]
    if not items:
        return []
    heights = sorted(int(b["box"][3]) for b in items)
    median_h = heights[len(heights) // 2] or 1
    tolerance = max(4.0, median_h * 0.5)

    ordered = sorted(items, key=lambda b: (b["box"][1] + b["box"][3] / 2.0, b["box"][0]))
    lines: list[list[dict[str, Any]]] = []
    for item in ordered:
        center = item["box"][1] + item["box"][3] / 2.0
        if lines:
            prev = lines[-1]
            prev_center = sum(b["box"][1] + b["box"][3] / 2.0 for b in prev) / len(prev)
            if abs(center - prev_center) <= tolerance:
                prev.append(item)
                continue
        lines.append([item])
    for line in lines:
        line.sort(key=lambda b: b["box"][0])
    return lines


def _best_window(seq: list[tuple[str, float]]) -> tuple[str, float] | None:
    """在一行的「字符+分数」序列里，找最像订单号的 12 位数字窗口。

    排序键是 **(最低分, 平均分)** 而不是平均分，这是关键：
    订单号那 12 个字是**整整齐齐都高**，而「订单号：」这类中文标签会被误配成
    数字且分数忽高忽低（实测 0.55~0.78，偶尔有个别字冲到 0.9）。用平均分的话，
    混进 3 个高分配错字的窗口照样能赢；用**最低分**就稳——窗口里只要有一个
    中文偏旁，最低分立刻掉下去。
    """
    best: tuple[str, float, float] | None = None
    n = len(seq)
    for start in range(0, n - ORDER_NO_LENGTH + 1):
        window = seq[start : start + ORDER_NO_LENGTH]
        chars = [ch for ch, _s in window]
        if not all(ch.isdigit() for ch in chars):
            continue
        scores = [s for _ch, s in window]
        low = min(scores)
        if low < _GLYPH_FLOOR:
            continue
        mean = sum(scores) / len(scores)
        if mean < _WINDOW_FLOOR:
            continue
        key = (low, mean)
        if best is None or key > (best[1], best[2]):
            best = ("".join(chars), low, mean)
    if best is None:
        return None
    return best[0], best[2]


def _find_by_blocks(blocks: list[dict[str, Any]]) -> OrderNumberResult | None:
    """按行做分数感知滑窗。返回 `None` 表示「这一层给不出结论」，交回文本路径。"""
    lines = _group_lines(blocks)
    if not lines:
        return None

    per_line: list[tuple[str, float]] = []
    for line in lines:
        seq = [(str(b["text"]), float(b.get("confidence", 0.0))) for b in line]
        found = _best_window(seq)
        if found is not None:
            per_line.append(found)
    if not per_line:
        return None

    per_line.sort(key=lambda kv: kv[1], reverse=True)
    top_no, top_mean = per_line[0]

    degraded: list[str] = []
    confidence = min(0.99, top_mean)
    if len(per_line) > 1:
        # 多行都给得出 12 位：号码不唯一，降置信度让上层先跟用户确认
        confidence = min(confidence, 0.6)
        degraded.append(DEGRADED_AMBIGUOUS)
    if top_mean < 0.8:
        degraded.append(DEGRADED_LOW_SCORE)

    return OrderNumberResult(
        order_no=top_no,
        confidence=round(confidence, 3),
        candidates=[no for no, _m in per_line],
        corrected=False,
        degraded=degraded,
    )


def find_order_no(
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> OrderNumberResult:
    """从 OCR 结果里抠 12 位订单号。

    Args:
        text: OCR 全文（可含换行、标签、噪声）。
        blocks: OCR 逐字块（含 `text` / `confidence` / `box`）。**传了就走分数感知
            路径**，比纯文本正则稳得多；不传则退回纯文本正则（兼容外部 OCR）。

    Returns:
        `OrderNumberResult`。
    """
    if blocks:
        by_blocks = _find_by_blocks(blocks)
        if by_blocks is not None and by_blocks.found:
            # 有几何+分数信息时以它为准：纯文本路径在低门槛 OCR 下会把
            # 中文偏旁误配的数字拼进号码，得到一个「12 位但错位」的号，
            # 那比查不到更糟（可能查到别人的订单）。
            return by_blocks
    if not text:
        return OrderNumberResult()

    context = _has_context(text)
    candidates: list[tuple[str, float, bool]] = []

    # ① 干净连号：最可信
    for match in _CLEAN_RE.finditer(text):
        candidates.append((match.group(1), 0.99, False))

    # ② 带分隔符的三段：拼起来是 12 位才算
    for match in _SEPARATED_RE.finditer(text):
        joined = "".join(match.groups())
        if len(joined) == ORDER_NO_LENGTH:
            candidates.append((joined, 0.95, False))

    # ③ 形近字纠正：只有 ①②都没结果时才做，避免把正常文字误伤成号码
    if not candidates:
        for match in _LOOSE_RE.finditer(text):
            token = match.group(0)
            if not any(ch.isdigit() for ch in token):
                continue  # 纯字母串（如 "SILK"）不当候选
            fixed = _correct(token)
            if len(fixed) == ORDER_NO_LENGTH and fixed.isdigit():
                candidates.append((fixed, 0.82, True))
            elif len(fixed) > ORDER_NO_LENGTH and fixed.isdigit():
                # 前后粘了噪声：滑窗找 12 位
                for start in range(0, len(fixed) - ORDER_NO_LENGTH + 1):
                    window = fixed[start : start + ORDER_NO_LENGTH]
                    candidates.append((window, 0.72, True))

    if not candidates:
        return OrderNumberResult()

    # 去重，保留每个号码的最高分
    best: dict[str, tuple[float, bool]] = {}
    for no, score, corrected in candidates:
        if no not in best or score > best[no][0]:
            best[no] = (score, corrected)

    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    top_no, (top_score, top_corrected) = ranked[0]

    degraded: list[str] = []
    if top_corrected:
        degraded.append(DEGRADED_CORRECTED)
    if len(ranked) > 1:
        # 多个候选：降置信度，让上层决定是否先跟用户确认
        top_score = min(top_score, 0.6)
        degraded.append(DEGRADED_AMBIGUOUS)
    if context and top_score >= 0.9:
        # 有「订单号」标签且号码干净，给一点加权（封顶 0.99）
        top_score = min(0.99, top_score + 0.0)

    return OrderNumberResult(
        order_no=top_no,
        confidence=round(top_score, 3),
        candidates=[no for no, _ in ranked],
        corrected=top_corrected,
        degraded=degraded,
    )


def mask_order_no(order_no: str) -> str:
    """打码展示（前 4 后 4，中间 4 个星号）。

    日志与上下文卡片一律用打码值——订单号属于个人信息关联标识（HC-2）。
    """
    if not order_no or len(order_no) <= 8:
        return order_no
    return f"{order_no[:4]}****{order_no[-4:]}"


__all__ = [
    "DEGRADED_AMBIGUOUS",
    "DEGRADED_CORRECTED",
    "DEGRADED_LOW_SCORE",
    "ORDER_NO_LENGTH",
    "OrderNumberResult",
    "find_order_no",
    "mask_order_no",
]
