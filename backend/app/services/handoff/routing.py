"""BE-14 · 工单坐席路由（reason + 渠道 + 意图 → 坐席分组）。

## 分组从哪来

`商家材料包/data/客服坐席分组表.csv` 的 6 个组，由
`backend/scripts/seed_reference_data.py` 写入 `agent_groups` 表：

| 组 | 名称 | 人数 | 主管 | 服务时段 | 渠道 |
| --- | --- | --- | --- | --- | --- |
| G1 | 售前咨询组 | 12 | 李敏 | 09:00–22:00 | 淘宝/天猫/拼多多/抖音小店/微信客服 |
| G2 | 订单物流组 | 8 | 李敏 | 09:00–21:00 | 淘宝/天猫/拼多多/抖音小店 |
| G3 | 售后服务组 | 10 | 李敏 | 09:00–21:00 | 五渠道 |
| G4 | 营销活动组 | 6 | 陈静 | 09:00–22:00 | 淘宝/天猫/拼多多/抖音小店 |
| G5 | 投诉与升级组 | 5 | 李敏 | 09:00–20:00 | 全渠道 |
| G6 | 品牌与信任组 | 3 | 周琳 | 10:00–19:00 | 全渠道 |

## 路由口径

1. **高风险 / 情绪 / 系统异常** → G5 投诉与升级组（不按意图分，按「要不要
   升级」分）；
2. **连续未解决** → G3 售后服务组（未解决九成是售后）；
3. **用户显式请求** → 按意图大类分：商品咨询 G1、订单服务 G2、售后服务 G3、
   营销活动 G4、品牌信任 G6、兜底 G1。

## 渠道兜底

G2/G4 不服务微信客服。若命中组不覆盖当前渠道，按
`G1 → G5 → G3 → G2 → G4 → G6` 顺序找第一个**启用且覆盖该渠道**的组。
找不到任何组时返回 None，由上层按「离线」处理（宁可兜底也不硬塞给一个
不存在的组）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.core.enums import Channel, HandoffReason, IntentCategory

#: 渠道枚举 → 坐席分组表里的中文渠道名
CHANNEL_CN: dict[Channel, str] = {
    Channel.TAOBAO: "淘宝",
    Channel.TMALL: "天猫",
    Channel.PDD: "拼多多",
    Channel.DOUYIN: "抖音小店",
    Channel.WECHAT: "微信客服",
}

#: `reason` → 固定分组（None 表示按意图大类分）
GROUP_BY_REASON: dict[HandoffReason, str | None] = {
    HandoffReason.USER_REQUEST: None,
    HandoffReason.EMOTION: "G5",
    HandoffReason.UNRESOLVED: "G3",
    HandoffReason.HIGH_RISK: "G5",
    HandoffReason.SYSTEM_ERROR: "G5",
}

#: 意图大类 → 分组（仅 `reason=USER_REQUEST` 时使用）
GROUP_BY_INTENT: dict[IntentCategory, str] = {
    IntentCategory.PRODUCT_CONSULT: "G1",
    IntentCategory.ORDER_SERVICE: "G2",
    IntentCategory.AFTER_SALE: "G3",
    IntentCategory.MARKETING: "G4",
    IntentCategory.BRAND_TRUST: "G6",
    IntentCategory.FALLBACK: "G1",
}

#: 渠道不匹配时的兜底查找顺序
FALLBACK_ORDER: tuple[str, ...] = ("G1", "G5", "G3", "G2", "G4", "G6")

#: 单张工单平均处理时长（秒），用于估算等待时间
AVG_HANDLE_SECONDS: int = 180


@dataclass
class GroupInfo:
    """坐席分组（`agent_groups` 行的只读视图）。"""

    group_id: str
    group_name: str = ""
    agent_count: int = 0
    supervisor: str = ""
    online_hours: str = ""
    channels: list[str] = field(default_factory=list)
    enabled: bool = True
    scope: str = ""

    def serves(self, channel_cn: str | None) -> bool:
        """该组是否服务指定渠道（空 `channels` 视为全渠道）。"""
        if not channel_cn:
            return True
        if not self.channels:
            return True
        return channel_cn in self.channels

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 友好字典。"""
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "agent_count": self.agent_count,
            "supervisor": self.supervisor,
            "online_hours": self.online_hours,
            "channels": list(self.channels),
            "enabled": self.enabled,
            "scope": self.scope,
        }


#: 内置默认分组（DB 不可用/未 seed 时使用，与种子数据同值）
DEFAULT_GROUPS: tuple[GroupInfo, ...] = (
    GroupInfo("G1", "售前咨询组", 12, "李敏", "09:00–22:00",
              ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]),
    GroupInfo("G2", "订单物流组", 8, "李敏", "09:00–21:00",
              ["淘宝", "天猫", "拼多多", "抖音小店"]),
    GroupInfo("G3", "售后服务组", 10, "李敏", "09:00–21:00",
              ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]),
    GroupInfo("G4", "营销活动组", 6, "陈静", "09:00–22:00",
              ["淘宝", "天猫", "拼多多", "抖音小店"]),
    GroupInfo("G5", "投诉与升级组", 5, "李敏", "09:00–20:00",
              ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]),
    GroupInfo("G6", "品牌与信任组", 3, "周琳", "10:00–19:00",
              ["淘宝", "天猫", "拼多多", "抖音小店", "微信客服"]),
)


def groups_from_rows(rows: Iterable[Any]) -> tuple[GroupInfo, ...]:
    """把 `agent_groups` 表行转成 `GroupInfo`（跳过 `group_id` 为空的行）。"""
    out: list[GroupInfo] = []
    for row in rows:
        get = row.get if isinstance(row, dict) else lambda k, r=row: getattr(r, k, None)
        gid = str(get("group_id") or "").strip()
        if not gid:
            continue
        channels = get("channels") or []
        if isinstance(channels, str):
            channels = [c.strip() for c in channels.split(",") if c.strip()]
        out.append(
            GroupInfo(
                group_id=gid,
                group_name=str(get("group_name") or ""),
                agent_count=int(get("agent_count") or 0),
                supervisor=str(get("supervisor") or ""),
                online_hours=str(get("online_hours") or ""),
                channels=[str(c) for c in channels],
                enabled=bool(get("enabled", True)),
                scope=str(get("scope") or ""),
            )
        )
    return tuple(sorted(out, key=lambda g: g.group_id))


def load_groups() -> tuple[GroupInfo, ...]:
    """从 `agent_groups` 表加载分组；表不存在/为空/DB 不可用时回退默认值。"""
    try:
        from app.db.engine import session_scope
        from app.db.models.ticket import AgentGroup

        with session_scope() as session:
            rows = session.query(AgentGroup).order_by(AgentGroup.group_id).all()
            groups = groups_from_rows(rows)
        return groups or DEFAULT_GROUPS
    except Exception:  # noqa: BLE001 - DB 抖动不该让转人工整体失效
        return DEFAULT_GROUPS


class AgentRouter:
    """坐席路由与排队估算。"""

    def __init__(self, groups: Sequence[GroupInfo] | None = None) -> None:
        self._groups: tuple[GroupInfo, ...] = (
            tuple(groups) if groups else DEFAULT_GROUPS
        )
        self._index: dict[str, GroupInfo] = {g.group_id: g for g in self._groups}

    # -- 属性 ---------------------------------------------------------------

    @property
    def groups(self) -> tuple[GroupInfo, ...]:
        """当前分组（按 `group_id` 升序）。"""
        return self._groups

    def get(self, group_id: str) -> GroupInfo | None:
        """按 ID 取分组。"""
        return self._index.get(group_id)

    def reload(self) -> None:
        """从数据库重新加载分组。"""
        self._groups = load_groups()
        self._index = {g.group_id: g for g in self._groups}

    # -- 路由 ---------------------------------------------------------------

    def route(
        self,
        *,
        reason: HandoffReason,
        channel: Channel | str | None = None,
        intent_category: IntentCategory | str | None = None,
    ) -> GroupInfo | None:
        """按原因/渠道/意图选组。

        Args:
            reason: 转人工原因。
            channel: 渠道（枚举或原始值）。
            intent_category: 意图大类（仅 `USER_REQUEST` 时参与）。

        Returns:
            命中的分组；无任何可用组时返回 None。
        """
        channel_cn = self._channel_cn(channel)
        primary = self._primary_group_id(reason, intent_category)
        candidates: list[str] = []
        if primary:
            candidates.append(primary)
        candidates.extend(g for g in FALLBACK_ORDER if g != primary)

        for gid in candidates:
            group = self._index.get(gid)
            if group is None or not group.enabled:
                continue
            if not group.serves(channel_cn):
                continue
            return group
        return None

    def queue_position(self, group_id: str) -> int:
        """该组当前排队数（OPEN/PROCESSING 工单数）。"""
        try:
            from app.db.engine import session_scope
            from app.db.models.ticket import Ticket

            with session_scope() as session:
                return int(
                    session.query(Ticket)
                    .filter(
                        Ticket.group_id == group_id,
                        Ticket.status.in_(["OPEN", "PROCESSING"]),
                    )
                    .count()
                )
        except Exception:  # noqa: BLE001 - 无 DB 时按 0 计
            return 0

    @staticmethod
    def estimate_wait_seconds(group: GroupInfo | None, position: int) -> int:
        """按「排队位 / 坐席数 × 单均处理时长」估算等待秒数。"""
        if group is None or group.agent_count <= 0:
            return 0
        waves = math.ceil(max(position, 1) / group.agent_count)
        return int(waves * AVG_HANDLE_SECONDS)

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _primary_group_id(
        reason: HandoffReason,
        intent_category: IntentCategory | str | None,
    ) -> str | None:
        fixed = GROUP_BY_REASON.get(reason)
        if fixed:
            return fixed
        category = _norm_category(intent_category)
        if category is None:
            return "G1"
        return GROUP_BY_INTENT.get(category, "G1")

    @staticmethod
    def _channel_cn(channel: Channel | str | None) -> str | None:
        if channel is None:
            return None
        if isinstance(channel, Channel):
            return CHANNEL_CN.get(channel)
        raw = str(channel).strip()
        if not raw:
            return None
        # 已经是中文渠道名（前端直传）
        if raw in CHANNEL_CN.values():
            return raw
        try:
            return CHANNEL_CN.get(Channel(raw))
        except ValueError:
            return None


def _norm_category(
    value: IntentCategory | str | None,
) -> IntentCategory | None:
    """归一意图大类；未知值返回 None。"""
    if value is None:
        return None
    if isinstance(value, IntentCategory):
        return value
    try:
        return IntentCategory(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_ROUTER: AgentRouter | None = None


def get_agent_router() -> AgentRouter:
    """进程内坐席路由单例（首次调用时从 DB 加载）。"""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = AgentRouter(load_groups())
    return _ROUTER


def set_agent_router(router: AgentRouter | None) -> None:
    """替换路由单例（单测注入用；传 None 复位）。"""
    global _ROUTER
    _ROUTER = router


__all__ = [
    "AVG_HANDLE_SECONDS",
    "CHANNEL_CN",
    "DEFAULT_GROUPS",
    "FALLBACK_ORDER",
    "GROUP_BY_INTENT",
    "GROUP_BY_REASON",
    "AgentRouter",
    "GroupInfo",
    "get_agent_router",
    "groups_from_rows",
    "load_groups",
    "set_agent_router",
]
