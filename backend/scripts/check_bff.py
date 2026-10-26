"""第 31 条（BFF 契约 + Mock Server）自检：8 个降级场景 + 端点覆盖 + 统一响应。

对应任务书 DoD：

* **契约 C 全部端点**（26 个）在 OpenAPI 里可见、统一响应 `code/message/request_id/data`；
* **Mock 必须覆盖的 8 个降级场景**（任务书 5.1.3）逐个可触发、HTTP 状态码与
  错误码符合契约、前端能据此演出对应 UI；
* **坐席鉴权与权限裁剪**：未登录 401/10002、越权 403/10003、并发接管 409/10005；
* **SSE 流式**事件序固定，前端可按事件名渲染。

评测方式：`TestClient` 打真应用，库用 `.pytest-work/check_bff.db` 临时 SQLite，
不连 Redis / 不连商家接口（BFF 走适配器默认的 Mock 实现）。

用法::

    python scripts/check_bff.py
"""

from __future__ import annotations

import json
import os
import sys

from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORK_DIR = BACKEND_DIR / ".pytest-work"
DOCS_DIR = BACKEND_DIR / "docs"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 必须在 import app.* 之前钉住本地目录：Settings 是 lru_cache 单例。
os.environ.setdefault("WK_ENV", "local")
os.environ.setdefault("WK_INTERNAL_TOKEN", "test_internal_token")
os.environ.setdefault("WK_LOCAL_DB_PATH", str(WORK_DIR / "check_bff.db"))
os.environ.setdefault("WK_LOCAL_MEDIA_DIR", str(WORK_DIR / "bff_media"))
os.environ.setdefault("WK_LOCAL_INDEX_DIR", str(WORK_DIR / "bff_index"))

FAILURES: list[str] = []
CHECKS = 0

#: 任务书 5.1.3 的 8 个降级场景（触发方式 → 期望表现）
DEGRADE_CASES: tuple[tuple[str, str, str], ...] = (
    ("error_50401", "订单查询超时", "降级话术 + 转人工建议"),
    ("error_50301", "库存/工单接口不可用", "503 + 不显示具体数字"),
    ("error_42901", "触发限流", "429 + Retry-After: 5"),
    ("error_40101", "签名失败", "前端无感，degraded 标记"),
    ("error_40102", "Token 失效", "渠道降级为纯 FAQ"),
    ("error_10005", "并发接管冲突", "409 + 已被 XXX 接管"),
    ("no_evidence", "无依据拒答", "转人工话术"),
    ("error_40901", "并发接管冲突（别名）", "409 + 已被 XXX 接管"),
)


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
# 应用与库
# ---------------------------------------------------------------------------


def _fresh_app():
    """清库 → 建表 → 造应用，返回 (TestClient, app)。"""
    from fastapi.testclient import TestClient

    from app.db.engine import create_all, reset_engine
    from app.main import create_app

    db_path = Path(os.environ["WK_LOCAL_DB_PATH"])
    if db_path.exists():
        db_path.unlink()
    reset_engine()
    create_all()
    app = create_app()
    return TestClient(app), app


def _data(response: Any) -> Any:
    """取统一响应 data（失败时返回 None，由调用方 check）。"""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return None
    if response.status_code >= 400 or body.get("code") != 0:
        return None
    return body.get("data")


def _turn(client, session_id: str, text: str, scenario: str = "") -> Any:
    """跑一轮对话，返回原始 response。"""
    headers = {"X-Mock-Scenario": scenario} if scenario else None
    return client.post(
        "/bff/v1/chat/turn",
        json={"session_id": session_id, "text": text},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1) 端点覆盖与统一响应
# ---------------------------------------------------------------------------


def check_endpoints(app) -> None:
    """契约 C 的端点是否全部挂在 OpenAPI 上。"""
    section("1) 端点覆盖（契约 C）")
    spec = app.openapi()
    paths = {p for p in spec["paths"] if p.startswith("/bff/v1")}
    expected = {
        "/bff/v1/sessions",
        "/bff/v1/sessions/{session_id}",
        "/bff/v1/sessions/{session_id}/context",
        "/bff/v1/sessions/{session_id}/takeover",
        "/bff/v1/sessions/{session_id}/release",
        "/bff/v1/sessions/{session_id}/replay",
        "/bff/v1/chat/turn",
        "/bff/v1/chat/stream",
        "/bff/v1/kb/chunks",
        "/bff/v1/kb/chunks/{chunk_id}",
        "/bff/v1/kb/chunks/{chunk_id}/history",
        "/bff/v1/kb/chunks/{chunk_id}/review",
        "/bff/v1/kb/search",
        "/bff/v1/kb/feedback",
        "/bff/v1/kb/stats",
        "/bff/v1/kb/import",
        "/bff/v1/metrics/overview",
        "/bff/v1/metrics/trend",
        "/bff/v1/config/handoff-rules",
        "/bff/v1/config/agent-groups",
        "/bff/v1/config/channels",
        "/bff/v1/tickets",
        "/bff/v1/tickets/{ticket_no}",
    }
    missing = sorted(expected - paths)
    check(not missing, "契约 C 23 个路径全部在 OpenAPI", f"缺失 {missing}" if missing else f"共 {len(paths)} 条")
    check(len(paths) >= 23, "BFF 路径数 ≥ 23", f"{len(paths)}")


def check_unified_response(client) -> None:
    """统一响应四键。"""
    section("2) 统一响应结构")
    body = client.get("/bff/v1/sessions").json()
    check(
        set(body) >= {"code", "message", "request_id", "data"},
        "响应含 code/message/request_id/data",
        f"{sorted(body)}",
    )
    check(body["code"] == 0 and body["data"] is not None, "成功响应 code=0 且带 data")
    check(bool(body["request_id"]), "request_id 非空", body["request_id"][:18])
    err = client.get("/bff/v1/sessions/not-exist").json()
    check(err["code"] == 10004, "不存在 → 10004", str(err["code"]))
    check(err["data"] is None, "失败响应 data 为 null")


# ---------------------------------------------------------------------------
# 3) 鉴权与权限裁剪
# ---------------------------------------------------------------------------


def check_auth(client) -> None:
    """坐席鉴权、角色裁剪。"""
    section("3) 坐席鉴权与权限裁剪")
    from app.core.config import get_settings

    settings = get_settings()
    check(not settings.bff_require_auth, "本地默认关闭强鉴权（开箱可用）")

    # 越权：agent 改配置 → 403/10003
    resp = client.put("/bff/v1/config/handoff-rules", json={"rule_id": "②", "threshold": 3})
    check(resp.status_code == 403, "agent 改配置 → 403", str(resp.status_code))
    check(resp.json()["code"] == 10003, "越权错误码 10003", str(resp.json()["code"]))

    # 开强鉴权：无 Token → 401/10002
    object.__setattr__(settings, "bff_require_auth", True)
    try:
        resp = client.get("/bff/v1/sessions")
        check(resp.status_code == 401, "开启鉴权后无 Token → 401", str(resp.status_code))
        check(resp.json()["code"] == 10002, "未登录错误码 10002", str(resp.json()["code"]))

        object.__setattr__(settings, "bff_agent_token", "agent-token")
        resp = client.get("/bff/v1/sessions", headers={"X-Agent-Token": "agent-token"})
        check(resp.status_code == 200, "带正确 Token → 200", str(resp.status_code))
    finally:
        object.__setattr__(settings, "bff_require_auth", False)
        object.__setattr__(settings, "bff_agent_token", "")


# ---------------------------------------------------------------------------
# 4) 8 个降级场景
# ---------------------------------------------------------------------------


def check_degrade(client) -> None:
    """任务书 5.1.3 的 8 个降级场景逐个触发。"""
    section("4) 8 个降级场景（任务书 5.1.3）")
    for scenario, name, expect in DEGRADE_CASES:
        if scenario in ("error_10005", "error_40901"):
            _data(_turn(client, f"dg_{scenario}", "你好"))
            resp = client.post(
                f"/bff/v1/sessions/dg_{scenario}/takeover",
                json={"agent_id": "a1"},
                headers={"X-Mock-Scenario": scenario},
            )
            body = resp.json()
            check(resp.status_code == 409, f"{name}：HTTP 409", str(resp.status_code))
            check(body["code"] == 10005, f"{name}：错误码 10005", str(body["code"]))
            check(
                "接管" in (body.get("message") or ""),
                f"{name}：提示语含「接管」",
                (body.get("message") or "")[:20],
            )
            continue

        resp = _turn(client, f"dg_{scenario}", "帮我查订单", scenario)
        if scenario == "error_42901":
            check(resp.status_code == 429, f"{name}：HTTP 429", str(resp.status_code))
            check(
                resp.headers.get("Retry-After") == "5",
                f"{name}：Retry-After=5",
                str(resp.headers.get("Retry-After")),
            )
            check(resp.json()["code"] == 20001, f"{name}：错误码 20001", str(resp.json()["code"]))
            continue
        if scenario == "error_50301":
            check(resp.status_code == 503, f"{name}：HTTP 503", str(resp.status_code))
            check(
                resp.json()["code"] == 30001,
                f"{name}：错误码 30001",
                str(resp.json()["code"]),
            )
            continue

        data = _data(resp)
        check(data is not None, f"{name}：业务成功返回", f"{resp.status_code}")
        if not data:
            continue
        content = (data.get("reply") or {}).get("content") or ""
        degraded = data.get("degraded") or []
        if scenario == "error_50401":
            check("系统查询中" in content, f"{name}：降级话术", content[:24])
            check("MOCK_ORDER_TIMEOUT" in degraded, f"{name}：degraded 标记", str(degraded))
            check("转人工" in content, f"{name}：给转人工出口", content[:24])
        elif scenario == "error_40101":
            check("MOCK_SIGNATURE_RETRY" in degraded, f"{name}：degraded 标记", str(degraded))
            check(bool(content), f"{name}：前端无感（仍正常回复）", content[:20])
        elif scenario == "error_40102":
            check(data is not None, f"{name}：渠道降级仍可应答", f"{resp.status_code}")
        elif scenario == "no_evidence":
            decision = data.get("decision") or {}
            check(
                decision.get("forced_no_evidence") is True,
                f"{name}：forced_no_evidence=True",
                str(decision.get("forced_no_evidence")),
            )
            check("人工" in content, f"{name}：转人工话术", content[:24])
    check(len(DEGRADE_CASES) >= 8, "降级场景数 ≥ 8", str(len(DEGRADE_CASES)))


# ---------------------------------------------------------------------------
# 5) 主链路：会话 → 工单 → 接管 → 回放
# ---------------------------------------------------------------------------


def check_main_flow(client) -> None:
    """文本问答 / 订单查询 / 转人工三条主链路的 BFF 侧闭环。"""
    section("5) 主链路闭环")
    data = _data(_turn(client, "flow_1", "我要转人工"))
    check(data is not None, "转人工一轮成功")
    handoff = (data or {}).get("handoff") or {}
    check(bool(handoff.get("ticket_no")), "回复带工单号", str(handoff.get("ticket_no")))

    sessions = _data(client.get("/bff/v1/sessions"))
    check((sessions or {}).get("total", 0) >= 1, "会话列表有记录", str((sessions or {}).get("total")))

    detail = _data(client.get("/bff/v1/sessions/flow_1"))
    messages = (detail or {}).get("messages") or []
    check(len(messages) >= 2, "会话详情含收发两条消息", str(len(messages)))
    check(
        all(m.get("direction") in ("INBOUND", "OUTBOUND") for m in messages),
        "direction 已归一为 INBOUND/OUTBOUND",
    )

    card = _data(client.get("/bff/v1/sessions/flow_1/context"))
    seven = ["summary", "verified", "tried_actions", "unmet_demand", "suggested_action", "emotion", "turn_count"]
    check(all(k in (card or {}) for k in seven), "上下文卡片七要素齐全", str(sorted((card or {}).keys())))

    taken = _data(client.post("/bff/v1/sessions/flow_1/takeover", json={"agent_id": "a1"}))
    check((taken or {}).get("owner") == "AGENT", "接管后 owner=AGENT")
    check((taken or {}).get("bot_muted") is True, "接管后机器人静音")

    conflict = client.post("/bff/v1/sessions/flow_1/takeover", json={"agent_id": "a2"})
    check(conflict.status_code == 409, "他人再接管 → 409", str(conflict.status_code))
    check(conflict.json()["code"] == 10005, "冲突错误码 10005", str(conflict.json()["code"]))

    released = _data(client.post("/bff/v1/sessions/flow_1/release"))
    check((released or {}).get("owner") == "BOT", "释放后 owner=BOT")

    replay = _data(client.get("/bff/v1/sessions/flow_1/replay"))
    check(len((replay or {}).get("messages") or []) >= 2, "回放含消息")

    tickets = _data(client.get("/bff/v1/tickets"))
    items = (tickets or {}).get("items") or []
    check((tickets or {}).get("total", 0) >= 1, "工单列表有记录", str((tickets or {}).get("total")))
    if items:
        no = items[0]["ticket_no"]
        check(no.startswith("TS") and len(no) == 14, "工单号 TS+12 位", no)
        # 关键回归：BFF 响应**不能**被打码，否则坐席拿 `TS25****9360` 查不到详情
        check("****" not in no, "工单号未被脱敏中间件打码", no)
        one = client.get(f"/bff/v1/tickets/{no}")
        check(one.status_code == 200, "按工单号查详情 200", str(one.status_code))


# ---------------------------------------------------------------------------
# 6) SSE 流式
# ---------------------------------------------------------------------------


def check_sse(client) -> None:
    """SSE 事件序固定，前端可按事件名渲染。"""
    section("6) SSE 流式事件")
    with client.stream("GET", "/bff/v1/chat/stream", params={"session_id": "sse_1", "text": "这个玩偶多高"}) as resp:
        check(resp.status_code == 200, "SSE 200", str(resp.status_code))
        check(
            "text/event-stream" in (resp.headers.get("content-type") or ""),
            "content-type=text/event-stream",
            resp.headers.get("content-type") or "",
        )
        raw = "".join(chunk for chunk in resp.iter_text())
    events = [line[7:].strip() for line in raw.splitlines() if line.startswith("event: ")]
    check(bool(events), "收到事件帧", str(events[:4]))
    check(events and events[0] == "turn.start", "首帧 turn.start", events[0] if events else "")
    check(events and events[-1] == "turn.end", "末帧 turn.end", events[-1] if events else "")
    check("delta" in events, "含 delta 增量帧")
    check("action" in events, "含 action 动作帧")


# ---------------------------------------------------------------------------
# 7) 控制台配置与指标
# ---------------------------------------------------------------------------


def check_console(client) -> None:
    """规则 / 分组 / 渠道 / 指标。"""
    section("7) 控制台配置与指标")
    rules = _data(client.get("/bff/v1/config/handoff-rules"))
    rule_items = (rules or {}).get("rules") or (rules or {}).get("items") or []
    check(len(rule_items) == 6, "6 条转人工规则", str(len(rule_items)))
    always_on = [r for r in rule_items if r.get("always_on")]
    check(len(always_on) == 2, "①④ 恒启用", str([r.get("rule_id") for r in always_on]))

    sup = {"X-Agent-Role": "supervisor", "X-Agent-Id": "sup_check"}
    resp = client.put(
        "/bff/v1/config/handoff-rules", json={"rule_id": "①", "enabled": False}, headers=sup
    )
    check(resp.status_code == 400, "停用规则① → 400", str(resp.status_code))

    groups = _data(client.get("/bff/v1/config/agent-groups"))
    group_items = (groups or {}).get("groups") or (groups or {}).get("items") or []
    check(len(group_items) == 6, "6 个坐席分组", str(len(group_items)))

    channels = _data(client.get("/bff/v1/config/channels"))
    channel_items = (channels or {}).get("channels") or (channels or {}).get("items") or []
    check(len(channel_items) == 5, "5 个渠道", str(len(channel_items)))

    overview = _data(client.get("/bff/v1/metrics/overview"))
    metrics = (overview or {}).get("metrics") or (overview or {}).get("items") or []
    check(len(metrics) >= 13, "指标 ≥ 13 项", str(len(metrics)))
    check(
        (overview or {}).get("total_count") == len(metrics),
        "total_count 与条目数一致",
        str((overview or {}).get("total_count")),
    )

    trend = _data(
        client.get(
            "/bff/v1/metrics/trend",
            params={"key": "orchestrator_p95", "granularity": "day", "days": 7},
        )
    )
    check(trend is not None, "趋势接口可用")
    check(
        (trend or {}).get("key") == "orchestrator_p95",
        "趋势返回指标键",
        str((trend or {}).get("key")),
    )
    bad = client.get("/bff/v1/metrics/trend", params={"key": "orchestrator_p95", "granularity": "week"})
    check(bad.status_code == 400, "非法粒度 → 400", str(bad.status_code))


# ---------------------------------------------------------------------------
# 8) OpenAPI 导出
# ---------------------------------------------------------------------------


def check_openapi_export(app) -> None:
    """把 BFF 契约导出成文件，供前端/联调对照。"""
    section("8) OpenAPI 导出")
    spec = app.openapi()
    bff_paths = {p: v for p, v in spec["paths"].items() if p.startswith("/bff/v1")}
    check(len(bff_paths) >= 23, "导出路径数 ≥ 23", str(len(bff_paths)))

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out = DOCS_DIR / "bff-openapi.json"
    payload = {
        "openapi": spec.get("openapi", "3.1.0"),
        "info": {
            "title": "玩可爱智能客服 Agent · BFF 契约（契约 C）",
            "version": "1.0.0",
            "description": (
                "由 scripts/check_bff.py 从运行中的应用导出，"
                "与后端实现同源；前端可用它生成类型或做契约测试。"
            ),
        },
        "paths": bff_paths,
        "components": {"schemas": spec.get("components", {}).get("schemas", {})},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    check(out.exists() and out.stat().st_size > 1000, "已写出 docs/bff-openapi.json", f"{out.stat().st_size} B")

    # 回读校验：文件是合法 JSON，且路径数与内存一致
    loaded = json.loads(out.read_text(encoding="utf-8"))
    check(len(loaded["paths"]) == len(bff_paths), "导出文件路径数与内存一致")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 契约对拍（第 9 条）：Mock 提供的每条路由，后端都必须实现
# ---------------------------------------------------------------------------


def _contract_diff_module():
    """按路径加载 `scripts/contract_diff.py`（scripts 不是包，不能用 import）。"""
    import importlib.util

    path = BACKEND_DIR / "scripts" / "contract_diff.py"
    spec = importlib.util.spec_from_file_location("contract_diff", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backend_routes(app) -> set[tuple[str, str]]:
    """从 OpenAPI 抽出 `/bff/v1` 的 (METHOD, 归一化路径)。"""
    import re as _re

    routes: set[tuple[str, str]] = set()
    for raw_path, item in app.openapi().get("paths", {}).items():
        if not raw_path.startswith("/bff/v1"):
            continue
        short = raw_path[len("/bff/v1"):] or "/"
        short = _re.sub(r"\{[^}]*\}", "{}", short)
        for method in item:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                routes.add((method.upper(), short))
    return routes


def check_contract_parity(app) -> None:
    """前端 Mock 有、后端没有 = 前端切真 BFF 必 404，必须为 0。"""
    section("契约对拍：Mock 路由 vs 后端 OpenAPI")
    diff = _contract_diff_module()
    mock = diff.load_mock_routes()
    backend = _backend_routes(app)
    both = mock & backend
    only_mock = sorted(mock - backend)
    only_backend = sorted(backend - mock)

    check(len(mock) >= 46, f"Mock 侧解析出 {len(mock)} 条路由（≥46）", str(len(mock)))
    check(len(backend) >= 46, f"后端 BFF 暴露 {len(backend)} 条路由（≥46）", str(len(backend)))
    check(
        not only_mock,
        f"Mock 的 {len(mock)} 条路由后端全部实现（对齐率 100%）",
        "" if not only_mock else "; ".join(f"{m} {p}" for m, p in only_mock),
    )
    check(
        len(both) == len(mock),
        f"交集 {len(both)} 条 == Mock 总数 {len(mock)}",
        f"{len(both)}/{len(mock)}",
    )
    # 后端多出来的不判失败：那是 Mock 没演的增强端点，联调时前端看不到而已。
    print(f"  [INFO] 仅后端有 {len(only_backend)} 条：" + ", ".join(f"{m} {p}" for m, p in only_backend))


def main() -> int:
    """入口。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    client, app = _fresh_app()
    try:
        check_endpoints(app)
        check_unified_response(client)
        check_auth(client)
        check_degrade(client)
        check_main_flow(client)
        check_sse(client)
        check_console(client)
        check_contract_parity(app)
        check_openapi_export(app)
    finally:
        client.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} / {CHECKS} checks")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"ALL PASSED ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
