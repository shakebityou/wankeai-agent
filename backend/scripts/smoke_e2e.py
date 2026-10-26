"""端到端冒烟：真实 HTTP 打容器里的 app（一键部署验证用）。

这是「一键部署验证」的验收入口——13 个 `check_*.py` 都是**进程内**自检
（进程内造引擎、进程内调服务），证明不了「容器真的起来了、链路真的通」。
本脚本只走 HTTP，把四条主链路全打一遍：

  A. 文本问答 → 知识库检索（Milvus + ES 双路）+ 生成回复（不出站链接）
  B. 订单查询 → 业务工具（数字走接口）
  C. 投诉 → 转人工 + 建工单
  D. BFF 坐席台：登录 → 会话列表 / 工单列表 / 指标 / 规则 / 知识库统计

用法（容器内，推荐）：
    docker compose exec -T -w /app -e PYTHONPATH=/app app python /app/scripts/smoke_e2e.py

前置条件：容器已 healthy、`alembic upgrade head` 已跑、`seed_reference_data.py` 已跑、
知识库已导入并 reindex（否则 A5 会红，提示「检索 0 命中」）。
可用环境变量：`WK_SMOKE_BASE`（默认 http://127.0.0.1:8000）、`WK_INTERNAL_TOKEN`。
退出码 0 = 全绿，1 = 有 FAIL（可直接进 CI / 上线前检查单）。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

BASE = os.environ.get("WK_SMOKE_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("WK_INTERNAL_TOKEN", "test_internal_token")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言。"""
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def call(method: str, path: str, body: object = None, *, token: bool = True,
         opener: urllib.request.OpenerDirector | None = None) -> tuple[int, object]:
    """发一次 JSON 请求。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Internal-Token"] = TOKEN
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:400]}


def umo(session_id: str, msg_id: str, text: str, buyer: str = "buyer_e2e") -> dict:
    """构造一条文本 UMO。"""
    return {
        "msg_id": msg_id,
        "session_id": session_id,
        "trace_id": f"trace_{msg_id}",
        "buyer_id": buyer,
        "channel": "TAOBAO",
        "direction": "INBOUND",
        "sender": "USER",
        "sent_at": datetime.now().isoformat(),
        "raw": {"msg_type": "TEXT", "extra": {}},
        "normalized": {"text": text, "text_source": "ORIGINAL", "confidence": 1.0},
    }


def turn(session_id: str, msg_id: str, text: str, buyer: str = "buyer_e2e") -> dict:
    """跑一轮对话，返回 data。"""
    status, payload = call(
        "POST", "/internal/v1/chat/turn",
        {"umo": umo(session_id, msg_id, text, buyer), "options": {}},
    )
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != 0:
        return {"__error__": f"status={status} payload={json.dumps(payload, ensure_ascii=False)[:400]}"}
    return payload.get("data") or {}


def main() -> int:
    """跑完全部冒烟。"""
    print("=" * 70)
    print("A. 文本问答（知识库检索）")
    print("=" * 70)
    d = turn("s_e2e_a", "m_e2e_a1", "这个搪胶玩偶能机洗吗")
    if "__error__" in d:
        check("A1 问答接口可用", False, d["__error__"])
    else:
        reply = (d.get("reply") or {}).get("content", "")
        check("A1 有回复正文", bool(reply.strip()), reply[:60])
        check("A2 回复 ≤120 字（HC 单条限制）", len(reply) <= 120, f"{len(reply)} 字")
        check("A3 无链接（HC-1 零外链）", "http" not in reply and "www." not in reply, reply[:40])
        check("A4 无卡片（cards 恒空）", (d.get("reply") or {}).get("cards") == [], str((d.get("reply") or {}).get("cards")))
        hits = d.get("rag_hits") or []
        check("A5 命中知识库", len(hits) > 0, f"{len(hits)} 条")
        lat = d.get("latency_ms") or {}
        print(f"        actions={d.get('actions')} latency={lat}")

    print()
    print("=" * 70)
    print("B. 订单查询（业务工具）")
    print("=" * 70)
    d = turn("s_e2e_b", "m_e2e_b1", "帮我查下订单 202601010001 到哪了")
    if "__error__" in d:
        check("B1 工具链路可用", False, d["__error__"])
    else:
        acts = d.get("actions") or []
        reply = (d.get("reply") or {}).get("content", "")
        check("B1 调用了业务工具", any("order" in a.lower() or "logistics" in a.lower() for a in acts), str(acts))
        check("B2 有回复", bool(reply.strip()), reply[:70])
        print(f"        reply={reply[:90]}")
        print(f"        actions={acts} latency={d.get('latency_ms')}")

    print()
    print("=" * 70)
    print("C. 投诉 → 转人工 + 建工单")
    print("=" * 70)
    d = turn("s_e2e_c", "m_e2e_c1", "太差了，我要投诉，转人工！")
    if "__error__" in d:
        check("C1 转人工链路可用", False, d["__error__"])
    else:
        hf = d.get("handoff") or {}
        reply = (d.get("reply") or {}).get("content", "")
        check("C1 触发转人工", bool(hf), json.dumps(hf, ensure_ascii=False)[:120])
        check("C2 有转人工原因", bool((hf or {}).get("reason")), str((hf or {}).get("reason")))
        check("C3 建了工单号", bool((hf or {}).get("ticket_no")), str((hf or {}).get("ticket_no")))
        check("C4 话术告知工单号", "TS" in reply or "工单" in reply, reply[:70])
        print(f"        handoff={json.dumps(hf, ensure_ascii=False)}")
        print(f"        reply={reply[:90]}")

    print()
    print("=" * 70)
    print("D. BFF 坐席台")
    print("=" * 70)
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, payload = call(
        "POST", "/bff/v1/auth/login",
        {"username": "wangfang", "password": "Wk@2026demo"}, token=False, opener=op,
    )
    logged = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
    check("D1 坐席登录成功", logged, f"status={status}")
    if logged:
        user = (payload.get("data") or {}).get("user") or {}
        print(f"        user={user.get('display_name')} role={user.get('role')} group={user.get('group_id')}")

        status, payload = call("GET", "/bff/v1/sessions?page=1&page_size=20", token=False, opener=op)
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
        data = (payload.get("data") or {}) if ok else {}
        items = data.get("items") or []
        check("D2 会话列表可读", ok, f"status={status} 共 {data.get('total')} 条")
        check("D3 能看到刚才的会话", any(i.get("session_id", "").startswith("s_e2e") for i in items),
              str([i.get("session_id") for i in items][:5]))

        status, payload = call("GET", "/bff/v1/tickets?page=1&page_size=20", token=False, opener=op)
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
        data = (payload.get("data") or {}) if ok else {}
        items = data.get("items") or []
        check("D4 工单列表可读", ok, f"status={status} 共 {data.get('total')} 条")
        check("D5 有转人工产生的工单", len(items) > 0, str([i.get("ticket_no") for i in items][:3]))

        status, payload = call("GET", "/bff/v1/metrics/overview", token=False, opener=op)
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
        check("D6 指标概览可读", ok, f"status={status}")
        if ok:
            print(f"        metrics={json.dumps(payload.get('data'), ensure_ascii=False)[:220]}")

        status, payload = call("GET", "/bff/v1/config/handoff-rules", token=False, opener=op)
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
        check("D7 转人工规则可读", ok, f"status={status}")

        status, payload = call("GET", "/bff/v1/kb/stats", token=False, opener=op)
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0
        total = ((payload.get("data") or {}) if ok else {}).get("total_chunks")
        check("D8 知识库统计可读", ok and total == 336, f"total_chunks={total}")

    print()
    print("=" * 70)
    print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for name in FAIL:
            print(f"  ✗ {name}")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
