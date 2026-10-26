"""BE-13 / BE-14 / BE-15 自检：转人工触发判定 + 工单创建 + 坐席路由 + 离线兜底。

对应任务书 DoD：

* **BE-13** 转人工触发判定引擎：6 条规则、优先级仲裁、①④ 不可停用、
  与 `orchestrator.decide_action` 结论一致；
* **BE-14** 工单创建与坐席路由：工单落库、**上下文卡片七要素完整率 100%**、
  幂等（同会话只一张单）、按 reason/意图/渠道分流到 G1~G6；
* **BE-15** 人工不在线兜底：在线/离线话术**零链接**、不承诺等待时长、
  商家接口失败/限流降级为本地队列且不阻断用户。

评测底料用商家真实语料 `历史客服会话语料.csv`（324 条，其中标注「是」42 条）。
**注意标注口径**：该列记的是「这通会话最终是否转给了人工」，不是
「该不该转」。所以「我要投诉」「欺诈维权」这类文本被标成「否」是正常的——
当时客服在线直接把问题按住了。脚本里把这类样本单列为「口径差异」，
不计入误报，避免把业务口径当成算法缺陷。

用法::

    python scripts/check_handoff.py
"""

from __future__ import annotations

import csv
import re
import sys

from datetime import time as dt_time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DATA_DIR = REPO_ROOT / "商家材料包" / "data"

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
# 环境：临时 SQLite（不碰开发库）
# ---------------------------------------------------------------------------


def _bind_temp_db() -> Any:
    """把全局引擎指到一个临时 SQLite，返回该 Engine。

    临时库放在 `backend/.pytest-work/` 下而不是系统 Temp：Windows 沙箱
    可能不允许写系统临时目录，`sqlite3` 会直接报 "unable to open database file"。
    """
    from app.db import engine as engine_mod

    tmp_dir = BACKEND_DIR / ".pytest-work"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "check_handoff.db"
    if db_path.exists():
        db_path.unlink()
    engine = engine_mod.make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    engine_mod.create_all(engine)
    engine_mod._ENGINE = engine
    engine_mod._SESSION_FACTORY = None
    return engine


def _load_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _card(**overrides: Any) -> dict[str, Any]:
    """七要素齐全的卡片。"""
    card: dict[str, Any] = {
        "summary": "用户反馈玩偶破损，要求换货",
        "verified": [{"type": "entity", "key": "order_no", "value": "123456789012"}],
        "tried_actions": [{"action": "order_query", "result": "已查到订单"}],
        "unmet_demand": "要求换货并承担运费",
        "suggested_action": "核实照片后走换货流程",
        "emotion": {"level": "不满", "intensity": "medium"},
        "turn_count": 4,
    }
    card.update(overrides)
    return card


def _pin_online() -> None:
    """把在线判定钉死为在线，让自检结果与真实时钟解耦。

    分组在线时段是 `09:00–22:00`，`is_online` 读真实时间——晚上跑脚本会被判
    离线，于是「商家同步 / `SYNCED` 事件 / `queue_position`」这些检查会假失败。
    只替换服务层引用的那个名字；`check_online_offline` 直接用
    `app.services.handoff.online.is_online` 并显式传时刻，不受影响；
    需要离线场景的检查显式传 `force_offline=True`。
    """
    from app.services.handoff import service as service_mod

    service_mod.is_online = lambda *a, **k: True


# ---------------------------------------------------------------------------
# BE-13：规则引擎
# ---------------------------------------------------------------------------


def check_rules() -> None:
    """6 条规则、①④ 不可停用、优先级仲裁。"""
    section("BE-13 · 转人工触发规则")
    from app.core.enums import HandoffReason
    from app.services.handoff.rules import (
        DEFAULT_RULES,
        HandoffRuleEngine,
        RuleSpec,
    )

    engine = HandoffRuleEngine(DEFAULT_RULES)
    check(len(engine.rules) == 6, "默认规则 6 条", f"实际 {len(engine.rules)}")

    # ①④ 强制启用：即使传 enabled=False 也会被纠正
    forced_ok = True
    for reason in (HandoffReason.USER_REQUEST, HandoffReason.HIGH_RISK):
        spec = RuleSpec(
            rule_id=99, name="x", reason=reason, priority=1, threshold=0,
            keywords=(), enabled=False, description="",
        )
        probe = HandoffRuleEngine((spec,))
        if not probe.rules[0].enabled:
            forced_ok = False
    check(forced_ok, "规则①④ 传 enabled=False 被纠正为启用（合规底线）")

    # 显式请求
    d1 = engine.judge(text="我要转人工")
    check(d1.should_handoff and d1.rule_id == 1, "「我要转人工」命中规则①")
    check(d1.reason is HandoffReason.USER_REQUEST, "reason=USER_REQUEST")

    # 高风险压过情绪
    d4 = engine.judge(text="我要打 12315 举报", emotion="愤怒")
    check(d4.should_handoff and d4.rule_id == 4, "高风险压过情绪（仲裁顺序）")
    check(d4.reason is HandoffReason.HIGH_RISK, "reason=HIGH_RISK")

    # 情绪
    d2 = engine.judge(text="你们这什么破服务", emotion="愤怒")
    check(d2.should_handoff and d2.rule_id == 2, "愤怒情绪命中规则②")

    # 未解决
    d3 = engine.judge(text="还是没解决", unresolved_count=3)
    check(d3.should_handoff and d3.rule_id == 3, "连续兜底命中规则③")

    # 业务类无依据
    d5 = engine.judge(
        text="这个玩偶多少钱",
        intent_category="商品咨询",
        retrieval_decision="NO_EVIDENCE",
        top_score=0.3,
    )
    check(d5.should_handoff and d5.rule_id == 5, "业务类无依据命中规则⑤")

    # 系统异常
    d6 = engine.judge(text="随便", system_error=True)
    check(d6.should_handoff and d6.rule_id == 6, "系统异常命中规则⑥")

    # 不命中
    d0 = engine.judge(text="这个玩偶多高呀", intent_category="商品咨询")
    check(not d0.should_handoff and d0.rule_id is None, "普通咨询不转人工")

    # to_dict 结构
    payload = d4.to_dict()
    check(
        set(payload) >= {"should_handoff", "rule_id", "rule_name", "reason",
                          "priority", "matched_rules", "hits"},
        "to_dict 字段完整",
        f"{sorted(payload)}",
    )
    check(len(payload["hits"]) == 6, "hits 逐条记录 6 条规则的判定明细")


# ---------------------------------------------------------------------------
# BE-14：坐席路由
# ---------------------------------------------------------------------------


def check_routing() -> None:
    """reason / 意图 / 渠道 → G1~G6。"""
    section("BE-14 · 坐席路由")
    from app.core.enums import HandoffReason
    from app.services.handoff.routing import DEFAULT_GROUPS, AgentRouter

    router = AgentRouter(DEFAULT_GROUPS)
    check(len(router.groups) == 6, "默认 6 个坐席分组")
    check(all(router.get(g) is not None for g in ("G1", "G2", "G3", "G4", "G5", "G6")),
          "G1~G6 全部可查")

    cases = [
        (HandoffReason.HIGH_RISK, None, "G5"),
        (HandoffReason.EMOTION, None, "G5"),
        (HandoffReason.SYSTEM_ERROR, None, "G5"),
        (HandoffReason.UNRESOLVED, None, "G3"),
        (HandoffReason.USER_REQUEST, "商品咨询", "G1"),
        (HandoffReason.USER_REQUEST, "订单服务", "G2"),
        (HandoffReason.USER_REQUEST, "售后服务", "G3"),
        (HandoffReason.USER_REQUEST, "营销活动", "G4"),
        (HandoffReason.USER_REQUEST, "品牌信任", "G6"),
    ]
    ok = True
    for reason, intent, expected in cases:
        group = router.route(reason=reason, intent_category=intent)
        if group is None or group.group_id != expected:
            ok = False
            print(f"    {reason.value}/{intent} → {getattr(group, 'group_id', None)}"
                  f"（期望 {expected}）")
    check(ok, "reason/意图 → 分组映射正确（9 例）")

    g2 = router.get("G2")
    assert g2 is not None
    check(router.estimate_wait_seconds(g2, 1) > 0, "预估等待时间为正")
    check(
        router.estimate_wait_seconds(g2, 10) > router.estimate_wait_seconds(g2, 1),
        "排队越久预估等待越长",
    )


# ---------------------------------------------------------------------------
# BE-15：在线 / 离线话术
# ---------------------------------------------------------------------------


def check_online_offline() -> None:
    """时段解析 + 话术合规。"""
    section("BE-15 · 在线判定与兜底话术")
    from app.core.enums import HandoffReason
    from app.services.handoff.online import (
        FORBIDDEN_WAIT_WORDS,
        is_online,
        offline_user_message,
        online_user_message,
        parse_online_hours,
    )

    parsed = parse_online_hours("09:00–22:00")
    check(parsed == (dt_time(9, 0), dt_time(22, 0)), "解析全角连接号时段")
    check(parse_online_hours("全天") is None, "无法解析返回 None（保守离线）")
    check(is_online("09:00–22:00", now=dt_time(12, 0)) is True, "时段内在线")
    check(is_online("09:00–22:00", now=dt_time(23, 0)) is False, "时段外离线")
    check(is_online("22:00–02:00", now=dt_time(23, 30)) is True, "跨零点班次在线")

    ok = True
    for reason in HandoffReason:
        text = offline_user_message(reason, "TS123456789012")
        if "http" in text.lower() or "点击" in text or "跳转" in text:
            ok = False
            print(f"    {reason.value} 含链接：{text}")
        for word in FORBIDDEN_WAIT_WORDS:
            if word in text:
                ok = False
                print(f"    {reason.value} 含禁词「{word}」：{text}")
    check(ok, "5 种 reason 离线话术：零链接、无等待承诺词")

    online = online_user_message("TS123456789012", 3)
    check("http" not in online.lower(), "在线话术零链接")
    check("TS123456789012" in online, "在线话术带工单号")


# ---------------------------------------------------------------------------
# BE-14：建单、幂等、七要素
# ---------------------------------------------------------------------------


def check_ticket_creation() -> None:
    """建单落库、上下文完整率 100%、幂等、详情/列表。"""
    section("BE-14 · 工单创建与上下文卡片")
    _pin_online()
    from app.adapters.cache import InMemoryCache
    from app.adapters.merchant_ticket import MockMerchantTicketPort
    from app.core.enums import Channel, HandoffReason
    from app.core.ratelimit import TokenBucketRateLimiter
    from app.services.handoff.service import HandoffService

    # 宽桶：建单本身 1 QPS，批量自检会被自己的限流挡住，那不是被测行为
    service = HandoffService(
        cache=InMemoryCache(),
        ticket_client=MockMerchantTicketPort(),
        limiter=TokenBucketRateLimiter(rate=1000, capacity=1000),
    )

    out = service.handoff(
        session_id="chk-1",
        reason=HandoffReason.USER_REQUEST,
        buyer_id="b-1",
        channel=Channel.TAOBAO,
        context_card=_card(),
    )
    check(out.ticket_no.startswith("TS") and len(out.ticket_no) == 14,
          "工单号格式 TS+12 位", out.ticket_no)

    detail = service.get_ticket(out.ticket_no)
    check(detail is not None, "工单可回读")
    assert detail is not None
    card = detail["context_card"]
    seven = ("summary", "verified", "tried_actions", "unmet_demand",
             "suggested_action", "emotion", "turn_count")
    check(all(card.get(k) for k in seven), "上下文卡片七要素齐全")
    check(detail["context_complete"] is True, "context_complete=True")
    events = [e["event"] for e in detail["events"]]
    check("CREATED" in events and "SYNCED" in events, "事件含 CREATED+SYNCED",
          ",".join(events))

    # 完整率 100%：30 个会话，卡片从「全空」到「齐全」混合
    complete = 0
    total = 30
    for i in range(total):
        payload = _card() if i % 3 else {}
        if i % 3 == 2:
            payload = {"summary": f"第{i}个会话的问题"}
        res = service.handoff(
            session_id=f"chk-bulk-{i}",
            reason=HandoffReason.UNRESOLVED,
            context_card=payload,
        )
        if res.context_complete:
            complete += 1
    check(complete == total, "上下文卡片完整率 100%", f"{complete}/{total}")

    # 幂等：同会话重复建单
    a = service.handoff(session_id="chk-idem", reason=HandoffReason.UNRESOLVED,
                        context_card=_card())
    b = service.handoff(session_id="chk-idem", reason=HandoffReason.UNRESOLVED,
                        context_card=_card())
    check(b.idempotent_hit is True and b.ticket_no == a.ticket_no,
          "同会话重复建单命中幂等")

    # 缓存清空后 DB 唯一约束兜底
    import asyncio

    asyncio.run(service.cache.delete_prefix("ticket:"))
    c = service.handoff(session_id="chk-idem", reason=HandoffReason.UNRESOLVED,
                        context_card=_card())
    check(c.ticket_no == a.ticket_no, "缓存丢失后 DB 唯一约束仍挡住重复建单")

    items, count = service.list_tickets()
    check(count == total + 2, "工单总数正确（批量 30 + 首次 1 + 幂等 1）",
          f"{count}")
    check(all("ticket_no" in i for i in items), "列表项含工单号")


# ---------------------------------------------------------------------------
# BE-15：商家失败 / 限流降级
# ---------------------------------------------------------------------------


def check_degrade() -> None:
    """商家不可用时不阻断用户。"""
    section("BE-15 · 商家同步失败与限流降级")
    _pin_online()
    from app.adapters.cache import InMemoryCache
    from app.adapters.merchant_ticket import MockMerchantTicketPort
    from app.core.enums import HandoffReason
    from app.core.ratelimit import RateLimitExceeded, TokenBucketRateLimiter
    from app.services.handoff.service import HandoffService

    # 1) 商家接口失败
    failing = HandoffService(
        cache=InMemoryCache(),
        ticket_client=MockMerchantTicketPort(always_fail=True),
        limiter=TokenBucketRateLimiter(rate=100, capacity=100),
    )
    out = failing.handoff(session_id="chk-fail", reason=HandoffReason.USER_REQUEST,
                          context_card=_card())
    check("TICKET_SYNC_FAILED" in out.degraded, "商家失败标记降级", str(out.degraded))
    check(out.merchant_ticket_no is None, "失败时无商家工单号")
    check(bool(out.text), "失败时用户仍收到话术")
    detail = failing.get_ticket(out.ticket_no)
    assert detail is not None
    check("FAILED" in [e["event"] for e in detail["events"]], "失败写 FAILED 事件")
    check(detail["merchant_ticket_no"] is None, "本地工单保留待补同步")

    # 2) 令牌桶限流
    class _AlwaysLimited:
        def acquire(self, key: str, tokens: float = 1.0) -> None:
            raise RateLimitExceeded(retry_after=1)

    limited = HandoffService(
        cache=InMemoryCache(),
        ticket_client=MockMerchantTicketPort(),
        limiter=_AlwaysLimited(),
    )
    out2 = limited.handoff(session_id="chk-rl", reason=HandoffReason.USER_REQUEST,
                           context_card=_card())
    check("TICKET_RATE_LIMITED" in out2.degraded, "限流标记降级", str(out2.degraded))
    detail2 = limited.get_ticket(out2.ticket_no)
    assert detail2 is not None
    check("RATE_LIMITED" in [e["event"] for e in detail2["events"]],
          "限流写 RATE_LIMITED 事件")

    # 3) 离线：不调商家接口
    offline_client = MockMerchantTicketPort()
    offline = HandoffService(
        cache=InMemoryCache(),
        ticket_client=offline_client,
        limiter=TokenBucketRateLimiter(rate=100, capacity=100),
    )
    out3 = offline.handoff(session_id="chk-off", reason=HandoffReason.USER_REQUEST,
                           context_card=_card(), force_offline=True)
    check(out3.offline is True and offline_client.calls == [],
          "离线兜底不调用商家接口")
    check("http" not in out3.text.lower(), "离线话术零链接")


# ---------------------------------------------------------------------------
# 真实语料：触发判定召回与误报
# ---------------------------------------------------------------------------


def check_corpus_trigger() -> None:
    """用 324 条商家真实语料回归规则召回率与误报率。"""
    section("真实语料回归 · 转人工触发判定（324 条）")
    from app.services.handoff.rules import DEFAULT_RULES, HandoffRuleEngine

    engine = HandoffRuleEngine(DEFAULT_RULES)
    rows = _load_csv("历史客服会话语料.csv")
    check(len(rows) == 324, "语料条数 324", f"{len(rows)}")

    yes = [r for r in rows if r["是否转人工"] == "是"]
    no = [r for r in rows if r["是否转人工"] == "否"]

    def _judge(row: dict[str, str]):
        return engine.judge(
            text=row["用户问题"],
            intent_category=row["意图类目"],
            emotion=row["用户情绪"],
        )

    # 显式请求规则：正例必须全中，反例必须不中
    explicit_pat = re.compile("转人工|人工客服|真人客服|转接人工|找人工|要人工")
    sub_yes = [r for r in yes if explicit_pat.search(r["用户问题"])]
    hit = sum(1 for r in sub_yes if 1 in _judge(r).matched_rules)
    check(len(sub_yes) >= 5, "语料中显式请求样本充足", f"{len(sub_yes)} 条")
    check(hit == len(sub_yes), "规则① 显式请求召回 100%", f"{hit}/{len(sub_yes)}")
    fp1 = sum(1 for r in no if explicit_pat.search(r["用户问题"])
              and 1 in _judge(r).matched_rules)
    check(fp1 == 0, "规则① 对未转人工样本零误触发", f"{fp1}")

    # 高风险规则
    high_pat = re.compile("12315|投诉|维权|消协|消保委|举报|起诉|法庭|曝光|媒体|工商|欺诈|骗子|法律")
    sub4 = [r for r in yes if high_pat.search(r["用户问题"])]
    hit4 = sum(1 for r in sub4 if 4 in _judge(r).matched_rules)
    check(hit4 == len(sub4), "规则④ 高风险召回 100%", f"{hit4}/{len(sub4)}")
    # 标注为「否」但含高风险词的样本，是「客服在线直接按住」的口径差异
    noise4 = [r for r in no if high_pat.search(r["用户问题"])]
    check(len(noise4) > 0, "语料含高风险但未转人工的口径差异样本（不计误报）",
          f"{len(noise4)} 条")

    # 情绪规则：中性用户不该被情绪规则命中
    neutral_no = [r for r in no if r["用户情绪"] == "中性"]
    fp2 = sum(1 for r in neutral_no if 2 in _judge(r).matched_rules)
    check(
        fp2 / max(1, len(neutral_no)) <= 0.01,
        "规则② 对中性情绪误触发率 ≤1%",
        f"{fp2}/{len(neutral_no)}",
    )

    # 整体：召回 ≥60%，误报 ≤25%
    tp = fp = fn = 0
    for r in rows:
        pred = _judge(r).should_handoff
        gold = r["是否转人工"] == "是"
        if pred and gold:
            tp += 1
        elif pred and not gold:
            fp += 1
        elif not pred and gold:
            fn += 1
    recall = tp / max(1, tp + fn)
    fpr = fp / max(1, fp + len(no) - fp)
    check(recall >= 0.60, "整体召回率 ≥60%", f"{recall:.1%}（{tp}/{tp + fn}）")
    check(fpr <= 0.25, "整体误报率 ≤25%", f"{fpr:.1%}（{fp}/{fp + len(no) - fp}）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    """入口。"""
    _bind_temp_db()
    check_rules()
    check_routing()
    check_online_offline()
    check_ticket_creation()
    check_degrade()
    check_corpus_trigger()

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
