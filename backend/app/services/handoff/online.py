"""BE-15 · 人工在线状态判定与「人工不在线」兜底话术。

## 在线状态从哪来

商家给了坐席分组表（`商家材料包/data/客服坐席分组表.csv`），其中
`online_hours` 是**服务时段**（如 `09:00–22:00`），没有实时在线接口。
因此判定口径是：

1. **服务时段内 + 组内有坐席** → 视为在线，可排队；
2. **服务时段外** → 离线，走兜底：**照建工单、照给工单号**，但不承诺
   「马上有人」，也不给排队位（给了就是假承诺）；
3. 若商家后续提供实时在线接口，只需替换 `is_online()` 的实现，其余不动。

⚠️ `online_hours` 里的连接号是**全角 `–`（U+2013）**，不是 ASCII `-`。
直接 `split("-")` 会得到整串，判定永远「离线」——这是 CSV 直读的经典坑，
所以这里把常见分隔符全兜住。

## 兜底话术的三条硬约束

* **不得出现链接/跳转引导**（HC-1）——工单号是纯文本，不是 URL；
* **不得出现「请稍等」「马上」**（BE-15 DoD）——离线场景下这是无限等待的
  暗示，用户会一直等一个不会来的客服；
* **必须给出可核验的凭据**（工单号），让用户相信「确实记下来了」。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Final

from app.core.enums import HandoffReason

#: 服务时段分隔符（全角/半角连接号、波浪号、中文「至」「到」）
_HOURS_SEP: Final[re.Pattern[str]] = re.compile(r"\s*(?:–|—|-|~|～|至|到)\s*")

#: 时间片段（允许全角冒号）
_TIME_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{2})$")

#: 东八区（商家在国内，服务时段按北京时间解释）
CST: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_cst() -> datetime:
    """当前北京时间（不依赖系统时区与 tzdata）。"""
    return datetime.now(tz=CST)


def parse_online_hours(spec: str | None) -> tuple[time, time] | None:
    """解析 `online_hours` 为 `(开始, 结束)`。

    Args:
        spec: 形如 `09:00–22:00`、`09:00-22:00`、`9:00至22:00`。

    Returns:
        `(time, time)`；无法解析时返回 None（调用方按「离线」处理，
        宁可保守也不假装有人在线）。
    """
    if not spec:
        return None
    parts = [p for p in _HOURS_SEP.split(str(spec).strip()) if p]
    if len(parts) != 2:
        return None
    parsed: list[time] = []
    for part in parts:
        m = _TIME_RE.match(part.strip())
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        parsed.append(time(hour=hour, minute=minute))
    return parsed[0], parsed[1]


def is_online(spec: str | None, now: datetime | time | None = None) -> bool:
    """判断当前是否在服务时段内。

    Args:
        spec: `online_hours` 原文。
        now: 参考时间（默认当前北京时间）。既接受 `datetime`（取其时分），
            也接受裸 `time`——后者方便测试固定时刻，也方便调用方只关心时分。

    Returns:
        True 表示在服务时段内。**无法解析时返回 False**（保守离线）。
    """
    window = parse_online_hours(spec)
    if window is None:
        return False
    start, end = window
    if now is None:
        moment = now_cst().timetz().replace(tzinfo=None)
    elif isinstance(now, datetime):
        moment = now.timetz().replace(tzinfo=None)
    else:
        moment = now
    if start <= end:
        return start <= moment <= end
    # 跨零点班次，如 22:00–02:00
    return moment >= start or moment <= end


# ---------------------------------------------------------------------------
# 话术
# ---------------------------------------------------------------------------

#: 在线转接话术（**不含链接、不含「请稍等」「马上」**）
ONLINE_TEMPLATE: Final[str] = (
    "已为您转接人工客服，工单号 {ticket_no}，前面还有 {queue} 位在排队～"
)

#: 在线但无排队位时的简化话术
ONLINE_TEMPLATE_NO_QUEUE: Final[str] = "已为您转接人工客服，工单号 {ticket_no}～"

#: 离线兜底话术（按 `reason` 区分，语气不同但都不承诺时间）
OFFLINE_TEMPLATES: Final[dict[HandoffReason, str]] = {
    HandoffReason.USER_REQUEST: (
        "已记录您的需求，工单号 {ticket_no}。人工客服上线后会按工单顺序联系您。"
    ),
    HandoffReason.EMOTION: (
        "已记录您反馈的问题，工单号 {ticket_no}。人工客服上线后会优先处理您的工单。"
    ),
    HandoffReason.UNRESOLVED: (
        "您的问题已记录下来，工单号 {ticket_no}。人工客服上线后会继续跟进这个问题。"
    ),
    HandoffReason.HIGH_RISK: (
        "您反馈的问题已升级记录，工单号 {ticket_no}。人工客服上线后会优先处理。"
    ),
    HandoffReason.SYSTEM_ERROR: (
        "系统暂时无法自动处理，已为您记录，工单号 {ticket_no}。人工客服上线后会跟进。"
    ),
}

#: 兜底话术禁用词（BE-15 DoD：禁止无限等待暗示）
FORBIDDEN_WAIT_WORDS: Final[tuple[str, ...]] = ("请稍等", "马上", "稍后立即", "立刻回复")


def online_user_message(
    ticket_no: str | None,
    queue_position: int | None = None,
) -> str:
    """在线场景的用户提示（纯文字，无链接）。"""
    if not ticket_no:
        return "已为您转接人工客服～"
    if queue_position is None:
        return ONLINE_TEMPLATE_NO_QUEUE.format(ticket_no=ticket_no)
    return ONLINE_TEMPLATE.format(ticket_no=ticket_no, queue=queue_position)


def offline_user_message(
    reason: HandoffReason,
    ticket_no: str | None,
) -> str:
    """离线场景的用户提示（纯文字，无链接，不承诺时间）。"""
    template = OFFLINE_TEMPLATES.get(
        reason, OFFLINE_TEMPLATES[HandoffReason.USER_REQUEST]
    )
    return template.format(ticket_no=ticket_no or "—")


__all__ = [
    "CST",
    "FORBIDDEN_WAIT_WORDS",
    "OFFLINE_TEMPLATES",
    "ONLINE_TEMPLATE",
    "ONLINE_TEMPLATE_NO_QUEUE",
    "is_online",
    "now_cst",
    "offline_user_message",
    "online_user_message",
    "parse_online_hours",
]
