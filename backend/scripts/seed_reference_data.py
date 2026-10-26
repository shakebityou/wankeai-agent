"""种子数据：坐席分组（`客服坐席分组表.csv`）+ 转人工规则 + 渠道配置。

**为什么要有种子数据**：`agent_groups` 的 6 个分组是商家真实编制，工单路由
（`BE-14`）必须按它分配；`handoff_rules` 的 6 条规则是 `BE-13` 的判定依据。
这两组数据不靠爬，靠**一次性导入**——改配置走 `FE-12`/`FE-13` 界面。

用法::

    # 先跑迁移
    python -m alembic upgrade head
    # 再导入种子（幂等：重复执行只会更新，不会重复插入）
    python scripts/seed_reference_data.py

设计约束：
* **幂等**：用「按主键 upsert」而不是「先 delete 再 insert」。因为
  `agent_groups` / `handoff_rules` 可能已被运营在界面上改过（例如改了在线时段），
  无脑重灌会把运营的改动冲掉。这里只灌**首次创建**的行 + 把缺失字段补齐，
  已存在的行不动 `enabled` 等运营可控字段。
* **可空安全**：脚本不依赖 `WK_*` 之外的任何环境变量。
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.base import utcnow
from app.db.models import AgentGroup, ChannelConfig, HandoffRule

# ---------------------------------------------------------------------------
# 6 条转人工规则（对应任务书 3.8 节 BE-13 的 6 条规则）
# ---------------------------------------------------------------------------
# priority 数值越大越优先。规则 1（显式请求）与规则 4（高风险意图）priority
# 最高——DoD 要求「显式请求与高风险意图不得被挽留」，它们必须先于挽留类规则命中。
# 规则 2（情绪）阈值取 negative_emotion_streak=3：连续 3 轮负面情绪才转。
# 规则 3（未解决）阈值取 unresolved_threshold=2：连续 2 轮没解决就转。
HANDOFF_RULES: list[dict] = [
    {
        "rule_id": 1,
        "name": "用户显式请求人工",
        "enabled": True,
        "reason": "USER_REQUEST",
        "threshold": 0.0,
        "keywords": [
            "转人工", "人工客服", "找人工", "真人", "人工服务", "接人工",
            "我要找人", "不要机器人", "不要机器人", "智能客服不行",
        ],
        "priority": 100,
        "description": "用户明确要求转人工。DoD 要求零挽留、100% 触发，禁止任何挽留话术。",
    },
    {
        "rule_id": 2,
        "name": "用户情绪激动",
        "enabled": True,
        "reason": "EMOTION",
        "threshold": 3.0,
        "keywords": [
            "投诉", "垃圾", "骗子", "假货", "差评", "举报", "315",
            "消协", "工商", "气死", "愤怒", "太差了", "什么玩意",
        ],
        "priority": 90,
        "description": "连续 3 轮负面情绪（ANGRY/DISSATISFIED）触发。阈值对应 negative_emotion_streak。",
    },
    {
        "rule_id": 3,
        "name": "连续未解决",
        "enabled": True,
        "reason": "UNRESOLVED",
        "threshold": 2.0,
        "keywords": [],
        "priority": 80,
        "description": "连续 2 轮未能解决用户问题。阈值对应 unresolved_threshold。",
    },
    {
        "rule_id": 4,
        "name": "高风险意图",
        "enabled": True,
        "reason": "HIGH_RISK",
        "threshold": 0.0,
        "keywords": [
            "赔偿", "起诉", "律师", "法院", "报警", "立案", "曝光",
            "媒体", "记者", "打假", "职业打假", "消保委", "12315",
        ],
        "priority": 100,
        "description": "命中商家《售后政策.md》278/279 行「涉及职业打假/法律诉讼一律上报法务，客服不得自行答复」。",
    },
    {
        "rule_id": 5,
        "name": "检索无依据",
        "enabled": True,
        "reason": "UNRESOLVED",
        "threshold": 0.55,
        "keywords": [],
        "priority": 60,
        "description": "RAG top_score < 0.55（NO_EVIDENCE）且为业务类问题。对应契约 2.4.5 规则⑤。",
    },
    {
        "rule_id": 6,
        "name": "系统异常",
        "enabled": True,
        "reason": "SYSTEM_ERROR",
        "threshold": 0.0,
        "keywords": [],
        "priority": 50,
        "description": "编排链路自身异常（LLM 超时、工具连续失败、熔断开）。兜底规则，禁止静默失败。",
    },
]

# ---------------------------------------------------------------------------
# 5 个渠道初始配置
# ---------------------------------------------------------------------------
# authorized=False + auth_expire_at 为空：项目尚未完成各平台授权（见
# 商家《渠道接入资质说明.md》），种子数据只登记「有哪些渠道」。
# callback_url 需与平台后台配置一致，此处留空由运维在 FE-11 界面补。
CHANNELS: list[dict] = [
    {
        "channel": "TAOBAO",
        "name": "淘宝千牛",
        "enabled": True,
        "authorized": False,
        "callback_url": "",
        "app_key": "",
        "note": "淘宝千牛消息 API。天猫同淘宝，共用同一套适配器。",
        "sort_order": 1,
    },
    {
        "channel": "TMALL",
        "name": "天猫",
        "enabled": True,
        "authorized": False,
        "callback_url": "",
        "app_key": "",
        "note": "与淘宝共用千牛消息 API，授权可复用。",
        "sort_order": 2,
    },
    {
        "channel": "PDD",
        "name": "拼多多",
        "enabled": True,
        "authorized": False,
        "callback_url": "",
        "app_key": "",
        "note": "客服消息 API。长连接 WebSocket，需单独实现心跳重连（BE-01 单列 2 人日缓冲）。",
        "sort_order": 3,
    },
    {
        "channel": "DOUYIN",
        "name": "抖音小店",
        "enabled": True,
        "authorized": False,
        "callback_url": "",
        "app_key": "",
        "note": "抖音小店 IM API，需服务商认证（资质周期较长，见渠道接入资质说明）。",
        "sort_order": 4,
    },
    {
        "channel": "WECHAT",
        "name": "微信客服",
        "enabled": True,
        "authorized": False,
        "callback_url": "",
        "app_key": "",
        "note": "企业微信微信客服 API（kf）。",
        "sort_order": 5,
    },
]

# 商家 CSV 列名 → 模型字段名。**刻意显式列出而不是靠约定**：CSV 表头里
# 有全角空格、有中文括号，靠猜必错。
CSV_TO_MODEL = {
    "分组编号": "group_id",
    "分组名称": "group_name",
    "负责范围": "scope",
    "坐席人数": "agent_count",
    "主管": "supervisor",
    "在线时段": "online_hours",
    "服务渠道": "channels_raw",
    "日均接待量": "daily_volume",
    "峰值接待量": "peak_volume",
    "备注": "note",
}

# 服务渠道 CSV 里用「、」分隔，需要转成列表
CHANNEL_NAME_TO_ENUM = {
    "淘宝": "TAOBAO",
    "天猫": "TMALL",
    "拼多多": "PDD",
    "抖音小店": "DOUYIN",
    "微信客服": "WECHAT",
    "全渠道": "ALL",
}


def _to_int(raw: str, default: int = 0) -> int:
    """CSV 数字列容错：空串/非数字回落默认值，不抛异常。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _split_channels(raw: str) -> list[str]:
    """「淘宝、天猫、拼多多、抖音小店、微信客服」→ 枚举列表。"""
    out: list[str] = []
    for piece in (raw or "").replace("，", "、").split("、"):
        name = piece.strip()
        if not name:
            continue
        enum = CHANNEL_NAME_TO_ENUM.get(name, name.upper())
        if enum not in out:
            out.append(enum)
    return out


def _read_agent_groups_csv(path: Path) -> list[dict]:
    """读商家《客服坐席分组表.csv》并映射为模型字段。"""
    if not path.exists():
        print(f"[warn] 未找到 {path}，跳过坐席分组导入（规则与渠道仍会导入）")
        return []

    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            mapped: dict = {}
            for csv_col, model_field in CSV_TO_MODEL.items():
                value = raw.get(csv_col)
                if value is None:
                    continue
                if model_field == "channels_raw":
                    mapped["channels"] = _split_channels(value)
                elif model_field in {"agent_count", "daily_volume", "peak_volume"}:
                    mapped[model_field] = _to_int(value)
                else:
                    mapped[model_field] = (value or "").strip()
            if mapped.get("group_id"):
                rows.append(mapped)
    return rows


def seed_handoff_rules(session: Session) -> tuple[int, int]:
    """灌 6 条转人工规则。返回 (created, updated)。"""
    created = updated = 0
    for rule in HANDOFF_RULES:
        existing = session.get(HandoffRule, rule["rule_id"])
        if existing is None:
            session.add(HandoffRule(**rule))
            created += 1
            continue
        # 只补齐运营不应改的字段；enabled 留给 FE-13 界面控制
        existing.name = rule["name"]
        existing.reason = rule["reason"]
        existing.threshold = rule["threshold"]
        existing.keywords = rule["keywords"]
        existing.priority = rule["priority"]
        existing.description = rule["description"]
        updated += 1
    return created, updated


def seed_channels(session: Session) -> tuple[int, int]:
    """灌 5 个渠道配置。返回 (created, updated)。"""
    created = updated = 0
    for channel in CHANNELS:
        existing = session.get(ChannelConfig, channel["channel"])
        if existing is None:
            session.add(ChannelConfig(**channel))
            created += 1
            continue
        existing.name = channel["name"]
        existing.note = channel["note"]
        existing.sort_order = channel["sort_order"]
        updated += 1
    return created, updated


def seed_agent_groups(session: Session, rows: list[dict]) -> tuple[int, int]:
    """灌坐席分组。返回 (created, updated)。"""
    created = updated = 0
    for row in rows:
        existing = session.get(AgentGroup, row["group_id"])
        if existing is None:
            session.add(AgentGroup(**row))
            created += 1
            continue
        existing.group_name = row.get("group_name", existing.group_name)
        existing.scope = row.get("scope", existing.scope)
        existing.agent_count = row.get("agent_count", existing.agent_count)
        existing.supervisor = row.get("supervisor", existing.supervisor)
        existing.online_hours = row.get("online_hours", existing.online_hours)
        existing.channels = row.get("channels", existing.channels)
        existing.daily_volume = row.get("daily_volume", existing.daily_volume)
        existing.peak_volume = row.get("peak_volume", existing.peak_volume)
        existing.note = row.get("note", existing.note)
        updated += 1
    return created, updated


def verify(session: Session) -> bool:
    """自检：确认关键行真的落库了。"""
    groups = session.scalar(select(AgentGroup.group_id))
    rules = session.scalar(select(HandoffRule.rule_id))
    channels = session.scalar(select(ChannelConfig.channel))

    ok = True
    if groups is None:
        print("[fail] agent_groups 为空 —— 工单路由（BE-14）会拿不到分组")
        ok = False
    else:
        count = len(session.query(AgentGroup).all())
        print(f"[ok] agent_groups: {count} 组（预期 6）")
        if count != 6:
            ok = False

    if rules is None:
        print("[fail] handoff_rules 为空 —— 转人工判定（BE-13）无规则可用")
        ok = False
    else:
        count = len(session.query(HandoffRule).all())
        print(f"[ok] handoff_rules: {count} 条（预期 6）")
        if count != 6:
            ok = False

    if channels is None:
        print("[fail] channels 为空 —— 渠道接入网关（BE-01）无渠道可路由")
        ok = False
    else:
        count = len(session.query(ChannelConfig).all())
        print(f"[ok] channels: {count} 个（预期 5）")
        if count != 5:
            ok = False
    return ok


def main() -> int:
    setup_logging("INFO")
    settings = get_settings()
    engine = create_engine(settings.resolved_db_url, pool_pre_ping=True)

    csv_path = settings.merchant_package_dir / "data" / "客服坐席分组表.csv"
    rows = _read_agent_groups_csv(csv_path)

    with Session(engine) as session:
        with session.begin():
            r1 = seed_handoff_rules(session)
            r2 = seed_channels(session)
            r3 = seed_agent_groups(session, rows)
        print(f"handoff_rules: created={r1[0]} updated={r1[1]}")
        print(f"channels:      created={r2[0]} updated={r2[1]}")
        print(f"agent_groups:  created={r3[0]} updated={r3[1]}")
        ok = verify(session)

    if not ok:
        print("SEED INCOMPLETE")
        return 1
    print("SEED OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
