"""BE-13 / BE-14 转人工 HTTP 接口测试。

覆盖 `app/api/v1/handoff.py` 的 6 个端点，重点验证四件**会真出事故**的事：

1. **规则 ①④ 不可停用**——显式请求与高风险转人工是合规底线，能停就能出事；
2. **规则改动要留审计**——HC-2 要求可追溯，改了阈值没记录等于没改；
3. **建单幂等**——前端重试、网络重发不能给同一会话开两张工单；
4. **工单不存在要 404 而不是 500**——前端要能区分「查不到」和「服务挂了」。

数据库用 `isolated_db` fixture：每用例一个临时 SQLite 文件，不碰开发库。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _pin_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「在线判定」钉死为在线，让接口用例与真实时钟解耦。

    分组在线时段 `09:00–22:00`，`is_online` 读真实时间——晚上跑测试会判离线，
    `queue_position` 变成 `None`、商家同步被跳过，断言假失败。
    只替换服务层引用的名字，离线场景靠 `force_offline=True` 显式触发。
    """
    from app.services.handoff import service as service_mod

    monkeypatch.setattr(service_mod, "is_online", lambda *a, **k: True)


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Any]:
    """每用例一个干净临时库，并把全局单例指向它。

    **必须成对替换 `_ENGINE` 与 `_SESSION_FACTORY`**：`session_scope()` 走
    `get_session_factory()`，该工厂是全局惰性单例，创建时就把 `bind` 钉死在
    当时的引擎上。只换 `_ENGINE` 不换工厂，端点仍连旧引擎。

    Args:
        tmp_path: pytest 每用例独立临时目录。

    Yields:
        临时库的 `Engine`。
    """
    from app.db import engine as engine_mod

    db_path = tmp_path / "handoff.db"
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
    """已挂载全部路由、且带内部服务凭证的测试客户端。

    Args:
        isolated_db: 临时库引擎（确保端点连的是它）。

    Yields:
        `TestClient`（默认请求头已带凭证）。
    """
    from app.main import create_app

    with TestClient(
        create_app(),
        headers={"X-Internal-Token": "test_internal_token"},
    ) as c:
        yield c


def _card(**overrides: Any) -> dict[str, Any]:
    """七要素齐全的上下文卡片。"""
    card: dict[str, Any] = {
        "summary": "用户反馈收到的玩偶有破损，要求换货",
        "verified": [{"type": "entity", "key": "order_no", "value": "123456789012"}],
        "tried_actions": [{"action": "order_query", "result": "已查到订单"}],
        "unmet_demand": "要求立即换货并承担运费",
        "suggested_action": "核实破损照片后走换货流程",
        "emotion": {"level": "不满", "intensity": "medium"},
        "turn_count": 4,
    }
    card.update(overrides)
    return card


def _create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """调建单接口，返回 `data`。"""
    payload: dict[str, Any] = {
        "session_id": "s-api-1",
        "reason": "USER_REQUEST",
        "buyer_id": "b-1",
        "channel": "TAOBAO",
        "context_card": _card(),
    }
    payload.update(overrides)
    resp = client.post("/internal/v1/handoff", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return body["data"]


# ---------------------------------------------------------------------------
# 规则管理
# ---------------------------------------------------------------------------


class TestRulesApi:
    """`GET /rules`、`PATCH /rules/{id}`、`POST /rules/evaluate`。"""

    def test_list_rules(self, client: TestClient) -> None:
        """默认 6 条规则，①④ 标记 always_on。"""
        resp = client.get("/internal/v1/handoff/rules")
        assert resp.status_code == 200, resp.text
        rules = resp.json()["data"]
        assert len(rules) == 6
        by_id = {r["rule_id"]: r for r in rules}
        assert by_id[1]["always_on"] is True
        assert by_id[4]["always_on"] is True
        assert by_id[2]["always_on"] is False

    @pytest.mark.parametrize("rule_id", [1, 4])
    def test_cannot_disable_always_on(self, client: TestClient, rule_id: int) -> None:
        """规则 ①④ 停用请求必须被拒（400），且库里的 enabled 不变。"""
        resp = client.patch(f"/internal/v1/handoff/rules/{rule_id}", json={"enabled": False})
        assert resp.status_code == 400, resp.text
        rules = {r["rule_id"]: r for r in client.get("/internal/v1/handoff/rules").json()["data"]}
        assert rules[rule_id]["enabled"] is True

    def test_patch_threshold(self, client: TestClient) -> None:
        """改阈值生效，并写一条 RULE_UPDATE 审计。"""
        resp = client.patch("/internal/v1/handoff/rules/2", json={"threshold": 2})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["threshold"] == 2

        rules = {r["rule_id"]: r for r in client.get("/internal/v1/handoff/rules").json()["data"]}
        assert rules[2]["threshold"] == 2

        from app.db.engine import session_scope
        from app.db.models.conversation import AuditLog

        with session_scope() as session:
            rows = (
                session.query(AuditLog)
                .filter(AuditLog.action == "RULE_UPDATE")
                .all()
            )
            assert len(rows) == 1
            assert rows[0].target_id == "2"
            assert rows[0].detail["before"]["threshold"] != 2
            assert rows[0].detail["after"]["threshold"] == 2

    def test_patch_priority_and_keywords(self, client: TestClient) -> None:
        """优先级与关键词一起改。"""
        resp = client.patch(
            "/internal/v1/handoff/rules/3",
            json={"priority": 95, "keywords": ["没解决", "还是不行"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["priority"] == 95
        assert data["keywords"] == ["没解决", "还是不行"]

    def test_patch_disable_optional(self, client: TestClient) -> None:
        """非底线规则可以停用，判定时被跳过。"""
        resp = client.patch("/internal/v1/handoff/rules/3", json={"enabled": False})
        assert resp.status_code == 200, resp.text
        eval_resp = client.post(
            "/internal/v1/handoff/rules/evaluate",
            json={
                "text": "这个问题还是没解决",
                "intent_category": "售后服务",
                "unresolved_count": 3,
                "turn_count": 12,
            },
        )
        assert eval_resp.status_code == 200, eval_resp.text
        assert eval_resp.json()["data"]["matched_rules"] == []

    def test_patch_missing_rule_404(self, client: TestClient) -> None:
        """引擎里不存在的规则号返回 404（防御性路径）。

        默认 6 条规则都在，所以这里注入一个「只有 5 条」的引擎来复现。
        """
        from app.adapters.cache import InMemoryCache
        from app.services.handoff.rules import DEFAULT_RULES, HandoffRuleEngine, set_rule_engine
        from app.services.handoff.service import HandoffService, set_handoff_service

        partial = HandoffRuleEngine(tuple(r for r in DEFAULT_RULES if r.rule_id != 6))
        set_rule_engine(partial)
        set_handoff_service(
            HandoffService(cache=InMemoryCache(), rule_engine=partial)
        )
        resp = client.patch("/internal/v1/handoff/rules/6", json={"threshold": 2})
        assert resp.status_code == 404, resp.text

    def test_patch_out_of_range_422(self, client: TestClient) -> None:
        """规则号超出 1..6 由路径校验拦住（400）。"""
        resp = client.patch("/internal/v1/handoff/rules/0", json={"threshold": 2})
        assert resp.status_code == 400, resp.text

    def test_evaluate_hits(self, client: TestClient) -> None:
        """`evaluate` 返回命中的规则与仲裁结果。"""
        resp = client.post(
            "/internal/v1/handoff/rules/evaluate",
            json={"text": "我要转人工", "intent_category": "商品咨询"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["should_handoff"] is True
        assert 1 in data["matched_rules"]
        assert data["reason"] == "USER_REQUEST"
        assert data["rule_id"] == 1

    def test_evaluate_no_hit(self, client: TestClient) -> None:
        """普通咨询不命中任何规则。"""
        resp = client.post(
            "/internal/v1/handoff/rules/evaluate",
            json={"text": "这个玩偶多高呀", "intent_category": "商品咨询"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["should_handoff"] is False
        assert data["matched_rules"] == []


# ---------------------------------------------------------------------------
# 建单
# ---------------------------------------------------------------------------


class TestCreateTicketApi:
    """`POST /handoff`。"""

    def test_create(self, client: TestClient) -> None:
        """建单返回契约要求的字段（工单号/状态/优先级/排队位次/话术）。"""
        data = _create(client)
        assert data["ticket_no"].startswith("TS")
        assert data["status"] == "OPEN"
        assert data["priority"]
        assert data["queue_position"] >= 1
        assert data["context_complete"] is True
        assert data["user_message"]
        # 契约外字段：`handoff_url` 只给坐席内网用，对用户话术里不能出现
        assert "http" not in data["user_message"].lower()

    def test_idempotent(self, client: TestClient) -> None:
        """同一会话重复建单返回同一工单号。"""
        first = _create(client, session_id="s-idem")
        second = _create(client, session_id="s-idem")
        assert second["ticket_no"] == first["ticket_no"]
        assert second["idempotent_hit"] is True
        listing = client.get("/internal/v1/handoff/tickets").json()["data"]
        assert listing["total"] == 1

    def test_create_high_risk_priority(self, client: TestClient) -> None:
        """高风险建单默认 URGENT。"""
        data = _create(client, reason="HIGH_RISK")
        assert data["priority"] == "URGENT"

    def test_create_missing_session_400(self, client: TestClient) -> None:
        """缺 `session_id` 返回 400。"""
        resp = client.post(
            "/internal/v1/handoff",
            json={"reason": "USER_REQUEST", "context_card": _card()},
        )
        assert resp.status_code == 400, resp.text

    def test_create_bad_reason_400(self, client: TestClient) -> None:
        """未知 reason 返回 400。"""
        resp = client.post(
            "/internal/v1/handoff",
            json={"session_id": "s-x", "reason": "NOT_A_REASON", "context_card": _card()},
        )
        assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 工单查询
# ---------------------------------------------------------------------------


class TestTicketQueryApi:
    """`GET /tickets`、`GET /tickets/{ticket_no}`。"""

    def test_list(self, client: TestClient) -> None:
        """列表返回分页结构。"""
        _create(client, session_id="s-l1")
        data = client.get("/internal/v1/handoff/tickets").json()["data"]
        assert data["total"] >= 1
        assert isinstance(data["items"], list)
        assert "page" in data and "page_size" in data

    def test_list_filter_group(self, client: TestClient) -> None:
        """按分组过滤（分组只在详情/列表项里，建单响应按契约不下发）。"""
        created = _create(client, session_id="s-l2", reason="HIGH_RISK")
        detail = client.get(
            f"/internal/v1/handoff/tickets/{created['ticket_no']}"
        ).json()["data"]
        data = client.get(
            "/internal/v1/handoff/tickets", params={"group_id": detail["group_id"]}
        ).json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["ticket_no"] == created["ticket_no"]

    def test_list_filter_status(self, client: TestClient) -> None:
        """按状态过滤。"""
        _create(client, session_id="s-l3")
        data = client.get(
            "/internal/v1/handoff/tickets", params={"status": "OPEN"}
        ).json()["data"]
        assert data["total"] == 1
        empty = client.get(
            "/internal/v1/handoff/tickets", params={"status": "CLOSED"}
        ).json()["data"]
        assert empty["total"] == 0

    def test_list_page_size_capped(self, client: TestClient) -> None:
        """`page_size` 上限 50，超过被路径校验拒绝（400）。"""
        resp = client.get("/internal/v1/handoff/tickets", params={"page_size": 500})
        assert resp.status_code == 400, resp.text

    def test_detail(self, client: TestClient) -> None:
        """详情带卡片、事件时间线、对话摘录。"""
        created = _create(
            client,
            session_id="s-d1",
            transcript=[{"role": "user", "content": "我要换货"}],
        )
        data = client.get(f"/internal/v1/handoff/tickets/{created['ticket_no']}").json()["data"]
        assert data["ticket_no"] == created["ticket_no"]
        assert data["context_card"]["summary"]
        assert data["events"]
        assert data["transcript"]

    def test_detail_404(self, client: TestClient) -> None:
        """不存在的工单返回 404（不是 500）。"""
        resp = client.get("/internal/v1/handoff/tickets/TS000000000000")
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] != 0


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


class TestAuth:
    """内部接口鉴权。"""

    def test_no_token_403(self, client: TestClient) -> None:
        """不带内部凭证一律 403。"""
        resp = client.get("/internal/v1/handoff/rules", headers={"X-Internal-Token": ""})
        assert resp.status_code == 403, resp.text

    def test_wrong_token_403(self, client: TestClient) -> None:
        """错误凭证 403。"""
        resp = client.get(
            "/internal/v1/handoff/rules", headers={"X-Internal-Token": "wrong"}
        )
        assert resp.status_code == 403, resp.text
