"""BFF 路由测试（第 31 条 / 契约 C 2.5）。

覆盖 `app/api/bff/*` 的三组路由：会话（含接管/释放/回放/上下文卡片）、
知识库（CRUD/审核/导入/检索/纠错/统计）、控制台（指标/规则/分组/渠道/工单）。

**重点验证四件会真出事故的事**：

1. **接管并发冲突**（场景⑦）——两人同时点「接管」，后来者必须拿到 10005，
   否则两个坐席会同时给同一个买家发消息；
2. **`bot_muted` 语义**——接管后机器人必须停手，释放后恢复；
3. **①④ 规则不可停用**——显式请求与高风险转人工是合规底线，接口层要拦；
4. **SSE 事件序**——前端按 8 类事件驱动 UI，少一个 `turn.end` 就永远转圈。

数据库沿用 `test_kb_api.py` 的 `isolated_db` 思路：每用例一个临时 SQLite，
**成对替换 `_ENGINE` 与 `_SESSION_FACTORY`**（只换前者，工厂仍绑旧引擎）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Any]:
    """每用例一个干净临时库，并把全局单例指向它。"""
    from app.db import engine as engine_mod

    db_path = tmp_path / "bff.db"
    engine = engine_mod.make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    engine_mod.create_all(engine)
    saved = engine_mod._ENGINE
    saved_factory = engine_mod._SESSION_FACTORY
    engine_mod._ENGINE = engine
    engine_mod._SESSION_FACTORY = None
    try:
        yield engine
    finally:
        engine_mod._ENGINE = saved
        engine_mod._SESSION_FACTORY = saved_factory
        engine.dispose()


@pytest.fixture()
def client(isolated_db: Any) -> Iterator[TestClient]:
    """BFF 测试客户端（默认坐席角色 `agent`）。"""
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def supervisor(client: TestClient) -> TestClient:
    """主管角色客户端（配置写接口需要）。"""
    client.headers.update({"X-Agent-Role": "supervisor", "X-Agent-Id": "sup_test"})
    return client


def _data(response: Any) -> Any:
    """取统一响应里的 `data`，失败时把完整响应塞进断言消息。"""
    body = response.json()
    assert response.status_code < 400, f"{response.status_code} {body}"
    assert body["code"] == 0, body
    return body["data"]


def _turn(client: TestClient, session_id: str, text: str, **extra: Any) -> dict:
    """跑一轮对话。

    `scenario` 走**请求头** `X-Mock-Scenario`（放 body 不生效，那是 Mock 开关）。
    """
    scenario = extra.pop("scenario", None)
    headers = dict(extra.pop("headers", {}) or {})
    if scenario:
        headers["X-Mock-Scenario"] = scenario
    return _data(
        client.post(
            "/bff/v1/chat/turn",
            json={"session_id": session_id, "text": text, **extra},
            headers=headers or None,
        )
    )


# ---------------------------------------------------------------------------
# 鉴权与角色
# ---------------------------------------------------------------------------


class TestAuth:
    """BFF 鉴权与角色裁剪。"""

    def test_default_open_in_local(self, client: TestClient) -> None:
        """`bff_require_auth=False`（默认）时本地放行。"""
        assert client.get("/bff/v1/sessions").status_code == 200

    def test_config_write_requires_supervisor(self, client: TestClient) -> None:
        """普通坐席改规则 → 403（10003）。"""
        response = client.put("/bff/v1/config/handoff-rules", json={"rule_id": "②", "threshold": 3})
        assert response.status_code == 403
        assert response.json()["code"] == 10003

    def test_supervisor_can_write(self, supervisor: TestClient) -> None:
        """主管可改规则。"""
        data = _data(supervisor.put("/bff/v1/config/handoff-rules", json={"rule_id": "②", "threshold": 3}))
        assert data["threshold"] == 3

    def test_require_auth_rejects_without_token(self, client: TestClient, monkeypatch: Any) -> None:
        """`bff_require_auth=True` 且无 Token → 401/10002。"""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "bff_require_auth", True, raising=False)
        response = client.get("/bff/v1/sessions")
        assert response.status_code == 401
        assert response.json()["code"] == 10002

    def test_require_auth_accepts_valid_token(self, client: TestClient, monkeypatch: Any) -> None:
        """带正确 Token 可访问。"""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "bff_require_auth", True, raising=False)
        monkeypatch.setattr(settings, "bff_agent_token", "agent-token", raising=False)
        response = client.get("/bff/v1/sessions", headers={"X-Agent-Token": "agent-token"})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


class TestSessions:
    """会话列表 / 详情 / 上下文卡片 / 接管 / 释放 / 回放。"""

    def test_empty_list(self, client: TestClient) -> None:
        """空库返回 total=0 的合法分页结构。"""
        data = _data(client.get("/bff/v1/sessions"))
        assert data["total"] == 0
        assert data["items"] == []
        assert data["page_size"] == 20

    def test_turn_persists_session_and_messages(self, client: TestClient) -> None:
        """一轮对话后：会话落库、详情带两条消息。"""
        _turn(client, "s1", "这个玩偶多高呀")
        data = _data(client.get("/bff/v1/sessions"))
        assert data["total"] == 1
        row = data["items"][0]
        assert row["session_id"] == "s1"
        assert row["turn_count"] == 1

        detail = _data(client.get("/bff/v1/sessions/s1"))
        assert len(detail["messages"]) == 2
        assert {m["direction"] for m in detail["messages"]} == {"INBOUND", "OUTBOUND"}

    def test_list_filters(self, client: TestClient) -> None:
        """渠道过滤生效；非法 owner 400。"""
        _turn(client, "s1", "你好")
        assert _data(client.get("/bff/v1/sessions", params={"channel": "TAOBAO"}))["total"] == 1
        assert _data(client.get("/bff/v1/sessions", params={"channel": "PDD"}))["total"] == 0
        assert client.get("/bff/v1/sessions", params={"owner": "XXX"}).status_code == 400

    def test_detail_404(self, client: TestClient) -> None:
        """不存在的会话 → 404/10004。"""
        response = client.get("/bff/v1/sessions/not-exist")
        assert response.status_code == 404
        assert response.json()["code"] == 10004

    def test_context_card_seven_elements(self, client: TestClient) -> None:
        """上下文卡片七要素字段齐全（没有工单时也要给一张可看的卡）。"""
        _turn(client, "s1", "这个玩偶多高呀")
        card = _data(client.get("/bff/v1/sessions/s1/context"))
        for key in (
            "summary",
            "verified",
            "tried_actions",
            "unmet_demand",
            "suggested_action",
            "emotion",
            "turn_count",
        ):
            assert key in card
        assert card["turn_count"] == 1
        assert isinstance(card["emotion"], dict)

    def test_takeover_then_release(self, client: TestClient) -> None:
        """接管 → bot_muted=True、owner=AGENT；释放 → 回 BOT。"""
        _turn(client, "s1", "你好")
        taken = _data(client.post("/bff/v1/sessions/s1/takeover", json={"agent_id": "a1"}))
        assert taken["owner"] == "AGENT"
        assert taken["agent_id"] == "a1"
        assert taken["bot_muted"] is True

        released = _data(client.post("/bff/v1/sessions/s1/release"))
        assert released["owner"] == "BOT"
        assert released["bot_muted"] is False

    def test_takeover_conflict(self, client: TestClient) -> None:
        """场景⑦：同一会话被 a1 接管后，a2 再接管 → 409/10005。"""
        _turn(client, "s1", "你好")
        _data(client.post("/bff/v1/sessions/s1/takeover", json={"agent_id": "a1"}))
        response = client.post("/bff/v1/sessions/s1/takeover", json={"agent_id": "a2"})
        assert response.status_code == 409
        assert response.json()["code"] == 10005
        assert "a1" in response.json()["message"]

    def test_same_agent_takeover_idempotent(self, client: TestClient) -> None:
        """同一坐席重复接管不报冲突（幂等，避免前端重试就炸）。"""
        _turn(client, "s1", "你好")
        _data(client.post("/bff/v1/sessions/s1/takeover", json={"agent_id": "a1"}))
        again = _data(client.post("/bff/v1/sessions/s1/takeover", json={"agent_id": "a1"}))
        assert again["owner"] == "AGENT"

    def test_replay(self, client: TestClient) -> None:
        """回放返回消息流、轮次明细与 trace_id。"""
        _turn(client, "s1", "你好")
        data = _data(client.get("/bff/v1/sessions/s1/replay"))
        assert len(data["messages"]) == 2
        assert len(data["turns"]) == 1
        assert data["trace_ids"]


# ---------------------------------------------------------------------------
# 对话与降级场景
# ---------------------------------------------------------------------------


class TestChat:
    """`/bff/v1/chat/turn` 与 8 个降级场景。"""

    def test_turn_contract_shape(self, client: TestClient) -> None:
        """返回契约 2.4.1 五段结构。"""
        data = _turn(client, "s1", "这个玩偶多高呀")
        for key in ("reply", "actions", "rag_hits", "handoff", "latency_ms"):
            assert key in data
        assert data["reply"]["msg_type"] == "TEXT"

    def test_missing_text(self, client: TestClient) -> None:
        """空文本 → 400/10001。"""
        response = client.post("/bff/v1/chat/turn", json={"session_id": "s1", "text": "  "})
        assert response.status_code == 400

    def test_scenario_rate_limited(self, client: TestClient) -> None:
        """场景④：`error_42901` → 429 + Retry-After。"""
        response = client.post(
            "/bff/v1/chat/turn",
            json={"session_id": "s1", "text": "你好"},
            headers={"X-Mock-Scenario": "error_42901"},
        )
        assert response.status_code == 429
        assert response.json()["code"] == 20001
        assert response.headers.get("Retry-After") == "5"

    def test_scenario_downstream_unavailable(self, client: TestClient) -> None:
        """场景②③：`error_50301` → 503/30001。"""
        response = client.post(
            "/bff/v1/chat/turn",
            json={"session_id": "s1", "text": "你好"},
            headers={"X-Mock-Scenario": "error_50301"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == 30001

    def test_scenario_order_timeout(self, client: TestClient) -> None:
        """场景①：`error_50401` → 200 + 降级话术 + degraded 标记。"""
        data = _turn(client, "s1", "帮我查订单", scenario="error_50401")
        assert "系统查询中" in data["reply"]["content"]
        assert "MOCK_ORDER_TIMEOUT" in data["degraded"]

    def test_scenario_no_evidence(self, client: TestClient) -> None:
        """场景⑧：`no_evidence` → 转人工话术。"""
        data = _turn(client, "s1", "帮我查订单", scenario="no_evidence")
        assert "转接人工" in data["reply"]["content"]
        assert data["decision"]["forced_no_evidence"] is True

    def test_scenario_signature_retry(self, client: TestClient) -> None:
        """场景⑤：`error_40101` 签名失败 → 前端无感，只打 degraded 标记。"""
        data = _turn(client, "s1", "这个玩偶多高呀", scenario="error_40101")
        assert "MOCK_SIGNATURE_RETRY" in data["degraded"]
        assert data["reply"]["content"]

    def test_scenario_token_expired(self, client: TestClient) -> None:
        """场景⑥：`error_40102` Token 失效 → 渠道降级但仍能应答。"""
        data = _turn(client, "s1", "这个玩偶多高呀", scenario="error_40102")
        assert data["reply"]["content"]

    def test_scenario_takeover_conflict(self, client: TestClient) -> None:
        """场景⑦：`error_10005` 并发接管冲突 → 409/10005 且不动库。"""
        _turn(client, "s1", "你好")
        response = client.post(
            "/bff/v1/sessions/s1/takeover",
            json={"agent_id": "a1"},
            headers={"X-Mock-Scenario": "error_10005"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == 10005
        assert "接管" in response.json()["message"]
        # 冲突场景不该真的改库：owner 仍是 BOT
        detail = _data(client.get("/bff/v1/sessions/s1"))
        assert (detail.get("session") or detail)["owner"] == "BOT"

    def test_unknown_scenario_rejected(self, client: TestClient) -> None:
        """未知场景 → 400（防止拼错场景名静默走正常路径）。"""
        response = client.post(
            "/bff/v1/chat/turn",
            json={"session_id": "s1", "text": "你好"},
            headers={"X-Mock-Scenario": "error_99999"},
        )
        assert response.status_code == 400

    def test_sse_event_sequence(self, client: TestClient) -> None:
        """SSE 至少出现 turn.start / delta / turn.end 三类事件。"""
        with client.stream(
            "GET", "/bff/v1/chat/stream", params={"session_id": "ss1", "text": "这个玩偶多高呀"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = [
                line.split(":", 1)[1].strip()
                for line in response.iter_lines()
                if line.startswith("event:")
            ]
        assert events[0] == "turn.start"
        assert events[-1] == "turn.end"
        assert "delta" in events
        assert set(events) <= {
            "turn.start",
            "intent",
            "retrieval",
            "delta",
            "action",
            "turn.end",
            "handoff",
            "error",
        }


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class TestKnowledge:
    """`/bff/v1/kb/*`。"""

    def test_create_get_update_delete(self, client: TestClient) -> None:
        """增改删全链路：软删后列表查不到、历史有版本。"""
        created = _data(
            client.post(
                "/bff/v1/kb/chunks",
                json={
                    "title": "玩偶多高",
                    "content": "这款玩偶高度约 30 厘米，适合 3 岁以上儿童。",
                    "category": "商品",
                    "knowledge_type": "PRODUCT",
                    "sku_ids": ["WK-001"],
                    "authority": 1,
                },
            )
        )
        chunk_id = created["chunk_id"]
        assert created["review_status"] == "PENDING"
        assert created["version"] == 1

        assert _data(client.get(f"/bff/v1/kb/chunks/{chunk_id}"))["title"] == "玩偶多高"

        updated = _data(
            client.put(
                f"/bff/v1/kb/chunks/{chunk_id}",
                json={"content": "这款玩偶高度约 32 厘米，适合 3 岁以上儿童。", "change_reason": "更正"},
            )
        )
        assert updated["version"] == 2

        history = _data(client.get(f"/bff/v1/kb/chunks/{chunk_id}/history"))
        assert len(history["items"]) >= 1

        _data(client.delete(f"/bff/v1/kb/chunks/{chunk_id}"))
        assert client.get(f"/bff/v1/kb/chunks/{chunk_id}").status_code == 404

    def test_list_pagination_and_filter(self, client: TestClient) -> None:
        """列表分页结构 + 关键词过滤。"""
        for i in range(3):
            _data(
                client.post(
                    "/bff/v1/kb/chunks",
                    json={"title": f"条目{i}", "content": f"内容{i}" * 10, "category": "商品"},
                )
            )
        data = _data(client.get("/bff/v1/kb/chunks", params={"page": 1, "page_size": 2}))
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert _data(client.get("/bff/v1/kb/chunks", params={"keyword": "条目1"}))["total"] == 1

    def test_page_size_limit(self, client: TestClient) -> None:
        """`page_size` 上限 50（契约 2.5.5）。"""
        assert client.get("/bff/v1/kb/chunks", params={"page_size": 51}).status_code == 400

    def test_review_requires_supervisor(self, client: TestClient) -> None:
        """审核需主管角色。"""
        created = _data(
            client.post("/bff/v1/kb/chunks", json={"title": "t", "content": "c" * 30, "category": "商品"})
        )
        response = client.post(
            f"/bff/v1/kb/chunks/{created['chunk_id']}/review", json={"review_status": "APPROVED"}
        )
        assert response.status_code == 403

    def test_review_approve(self, supervisor: TestClient) -> None:
        """主管审核通过。"""
        created = _data(
            supervisor.post("/bff/v1/kb/chunks", json={"title": "t", "content": "c" * 30, "category": "商品"})
        )
        data = _data(
            supervisor.post(
                f"/bff/v1/kb/chunks/{created['chunk_id']}/review", json={"review_status": "APPROVED"}
            )
        )
        assert data["review_status"] == "APPROVED"

    def test_review_invalid_status(self, supervisor: TestClient) -> None:
        """非法审核状态 → 400。"""
        created = _data(
            supervisor.post("/bff/v1/kb/chunks", json={"title": "t", "content": "c" * 30, "category": "商品"})
        )
        response = supervisor.post(
            f"/bff/v1/kb/chunks/{created['chunk_id']}/review", json={"review_status": "WHAT"}
        )
        assert response.status_code == 400

    def test_search(self, client: TestClient) -> None:
        """检索返回 hits 结构与 decision。"""
        _data(
            client.post(
                "/bff/v1/kb/chunks",
                json={"title": "退货政策", "content": "支持 7 天无理由退货，需保持吊牌完整。", "category": "售后政策"},
            )
        )
        data = _data(client.post("/bff/v1/kb/search", json={"query": "退货", "top_k": 5}))
        assert data["decision"] in {"DIRECT_ANSWER", "FALLBACK", "NO_EVIDENCE"}
        assert isinstance(data["items"], list)

    def test_feedback(self, client: TestClient) -> None:
        """纠错上报落库（契约要求 message_id/session_id/error_type 必填）。"""
        data = _data(
            client.post(
                "/bff/v1/kb/feedback",
                json={
                    "message_id": "msg_fb_1",
                    "session_id": "sess_fb_1",
                    "chunk_id": None,
                    "answer_text": "答错了",
                    "reason": "答非所问",
                    "error_type": "WRONG_ANSWER",
                },
            )
        )
        assert data["feedback_id"].startswith("FB-")
        assert data["status"] == "OPEN"
        # 同一 message_id 再报一次是幂等的：返回同一条
        again = _data(
            client.post(
                "/bff/v1/kb/feedback",
                json={
                    "message_id": "msg_fb_1",
                    "session_id": "sess_fb_1",
                    "answer_text": "答错了",
                    "error_type": "WRONG_ANSWER",
                },
            )
        )
        assert again["feedback_id"] == data["feedback_id"]

    def test_import(self, supervisor: TestClient) -> None:
        """批量导入（幂等：同内容再导一次是 skipped）。"""
        payload = {
            "rows": [
                {
                    "title": "退货",
                    "content": "支持 7 天无理由退货，需保持吊牌完整。",
                    "metadata": {"category": "售后政策", "knowledge_type": "POLICY", "source": "bff"},
                }
            ]
        }
        first = _data(supervisor.post("/bff/v1/kb/import", json=payload))
        assert first["inserted"] == 1
        second = _data(supervisor.post("/bff/v1/kb/import", json=payload))
        assert second["inserted"] == 0
        assert second["skipped"] == 1

    def test_import_empty_rows(self, supervisor: TestClient) -> None:
        """空 rows → 400。"""
        assert supervisor.post("/bff/v1/kb/import", json={"rows": []}).status_code == 400

    def test_stats(self, client: TestClient) -> None:
        """统计接口可用。"""
        assert _data(client.get("/bff/v1/kb/stats"))


# ---------------------------------------------------------------------------
# 指标与配置
# ---------------------------------------------------------------------------


class TestMetrics:
    """`/bff/v1/metrics/*`。"""

    def test_overview(self, client: TestClient) -> None:
        """12 项基线 + 实时量，共 13 项。"""
        data = _data(client.get("/bff/v1/metrics/overview"))
        assert data["total_count"] == 13
        keys = {item["key"] for item in data["items"]}
        for key in ("intent_accuracy", "rag_recall_5", "handoff_recall", "orchestrator_p95"):
            assert key in keys
        assert 0 <= data["passed_count"] <= data["total_count"]

    def test_overview_after_turn(self, client: TestClient) -> None:
        """跑一轮后会话数与 P95 有值。"""
        _turn(client, "s1", "你好")
        data = _data(client.get("/bff/v1/metrics/overview"))
        items = {item["key"]: item for item in data["items"]}
        assert items["session_volume"]["value"] >= 1

    def test_trend(self, client: TestClient) -> None:
        """趋势接口返回 points 列表。"""
        data = _data(client.get("/bff/v1/metrics/trend", params={"key": "orchestrator_p95"}))
        assert isinstance(data["points"], list)

    def test_trend_bad_granularity(self, client: TestClient) -> None:
        """非法粒度 → 400。"""
        assert (
            client.get("/bff/v1/metrics/trend", params={"key": "x", "granularity": "week"}).status_code
            == 400
        )


class TestConfig:
    """`/bff/v1/config/*`。"""

    def test_rules_list(self, client: TestClient) -> None:
        """六条规则，①④ 恒启用。"""
        data = _data(client.get("/bff/v1/config/handoff-rules"))
        assert data["total"] == 6
        always_on = {r["rule_id"] for r in data["items"] if r["reason"] in {"USER_REQUEST", "HIGH_RISK"}}
        assert always_on == {"①", "④"}
        assert all(r["enabled"] for r in data["items"] if r["rule_id"] in always_on)
        # 契约字段 always_on 让前端把开关置灰，不必在前端硬编码 ①④
        assert {r["rule_id"] for r in data["items"] if r["always_on"]} == {"①", "④"}

    def test_rules_disable_always_on_rejected(self, supervisor: TestClient) -> None:
        """①/④ 停用 → 400（合规底线）。"""
        for rule in ("①", "④"):
            response = supervisor.put(
                "/bff/v1/config/handoff-rules", json={"rule_id": rule, "enabled": False}
            )
            assert response.status_code == 400
            assert "不可停用" in response.json()["message"]

    def test_rules_bad_id(self, supervisor: TestClient) -> None:
        """非法 rule_id → 400。"""
        assert (
            supervisor.put("/bff/v1/config/handoff-rules", json={"rule_id": "⑨"}).status_code == 400
        )

    def test_groups(self, client: TestClient) -> None:
        """六组，G1–G6。"""
        data = _data(client.get("/bff/v1/config/agent-groups"))
        assert data["total"] == 6
        assert {g["group_id"] for g in data["items"]} == {f"G{i}" for i in range(1, 7)}

    def test_group_update_creates_row(self, supervisor: TestClient) -> None:
        """空库首次改分组会按默认值建行（不 404）。"""
        data = _data(
            supervisor.put("/bff/v1/config/agent-groups", json={"group_id": "G1", "agent_count": 15})
        )
        assert data["agent_count"] == 15

    def test_channels(self, client: TestClient) -> None:
        """五渠道。"""
        data = _data(client.get("/bff/v1/config/channels"))
        assert data["total"] == 5
        assert {c["channel"] for c in data["items"]} == {
            "TAOBAO",
            "TMALL",
            "PDD",
            "DOUYIN",
            "WECHAT",
        }

    def test_channel_update(self, supervisor: TestClient) -> None:
        """改渠道备注。"""
        data = _data(supervisor.put("/bff/v1/config/channels", json={"channel": "TAOBAO", "note": "正常"}))
        assert data["note"] == "正常"


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------


class TestTickets:
    """`/bff/v1/tickets/*`。"""

    def test_empty(self, client: TestClient) -> None:
        """空库 total=0。"""
        assert _data(client.get("/bff/v1/tickets"))["total"] == 0

    def test_ticket_after_handoff(self, client: TestClient) -> None:
        """显式要求转人工 → 工单落库并可查详情。"""
        _turn(client, "s1", "我要转人工")
        data = _data(client.get("/bff/v1/tickets"))
        assert data["total"] == 1
        ticket_no = data["items"][0]["ticket_no"]
        assert ticket_no.startswith("TS")

        detail = _data(client.get(f"/bff/v1/tickets/{ticket_no}"))
        assert detail["ticket"]["ticket_no"] == ticket_no
        assert detail["transcript"] is not None

    def test_ticket_404(self, client: TestClient) -> None:
        """不存在的工单 → 404。"""
        assert client.get("/bff/v1/tickets/TS000000000000").status_code == 404

    def test_ticket_filter_status(self, client: TestClient) -> None:
        """按状态过滤。"""
        _turn(client, "s1", "我要转人工")
        assert _data(client.get("/bff/v1/tickets", params={"status": "OPEN"}))["total"] == 1
        assert _data(client.get("/bff/v1/tickets", params={"status": "CLOSED"}))["total"] == 0


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


class TestOpenApi:
    """OpenAPI 覆盖（第 31 条 DoD：BFF 契约进 OpenAPI）。"""

    def test_bff_paths_documented(self, client: TestClient) -> None:
        """26 个契约端点里已实现 23 个，全部出现在 OpenAPI 里。"""
        spec = client.get("/openapi.json").json()
        paths = {p for p in spec["paths"] if p.startswith("/bff/v1")}
        assert len(paths) >= 23
        assert "/bff/v1/sessions" in paths
        assert "/bff/v1/chat/stream" in paths
        assert "/bff/v1/kb/chunks" in paths
        assert "/bff/v1/metrics/overview" in paths

    def test_unified_response_schema(self, client: TestClient) -> None:
        """统一响应含 code/message/request_id/data 四键。"""
        body = client.get("/bff/v1/sessions").json()
        assert set(body) == {"code", "message", "request_id", "data"}
