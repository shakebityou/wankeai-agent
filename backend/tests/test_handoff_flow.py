"""BE-13 → BE-14 闭环：一轮对话触发转人工并真实建单。

这是唯一一条**穿过编排器**的转人工测试。前面的 `test_handoff_service.py`
和 `test_handoff_api.py` 分别验证「服务能建单」和「接口能建单」，但都不回答
最关键的那个问题：**用户说一句「我要转人工」，从 `/chat/turn` 进来，
到 `tickets` 表里真的多出一行，中间这段路是通的吗？**

不测这条，最典型的线上事故就会漏掉：判定引擎说「该转」，编排层却拿了
另一套结论、或者建单时卡片字段类型对不上被 Pydantic 静默丢掉——
两边单测全绿，用户那头却收到一句「已转人工」而工单根本没落库。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Any]:
    """每用例一个干净临时库，并把全局单例指向它。"""
    from app.db import engine as engine_mod

    db_path = tmp_path / "flow.db"
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
    """带内部凭证的测试客户端。"""
    from app.main import create_app

    with TestClient(
        create_app(), headers={"X-Internal-Token": "test_internal_token"}
    ) as c:
        yield c


def _turn(client: TestClient, text: str, *, session_id: str) -> dict[str, Any]:
    """发一轮对话，返回 `data`。"""
    payload = {
        "umo": {
            "msg_id": f"m-{session_id}-{text[:6]}",
            "session_id": session_id,
            "trace_id": f"trace-{session_id}",
            "buyer_id": "buyer-flow",
            "channel": "TAOBAO",
            "sent_at": datetime.now(UTC).isoformat(),
            "raw": {"msg_type": "TEXT"},
            "normalized": {"text": text, "text_source": "ORIGINAL", "confidence": 0.99},
        }
    }
    resp = client.post("/internal/v1/chat/turn", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


class TestHandoffFlow:
    """从对话入口到工单落库。"""

    def test_explicit_request_creates_ticket(self, client: TestClient) -> None:
        """「我要转人工」→ 回复带工单号 + 库里真的多一张单。"""
        data = _turn(client, "我要转人工", session_id="flow-explicit")
        handoff = data["handoff"]
        assert handoff is not None, data
        assert handoff["reason"] == "USER_REQUEST"
        assert handoff["ticket_no"]
        assert handoff["rule_id"] == 1
        # 回复是纯文字话术，不能出现链接（HC-1）
        assert "http" not in data["reply"]["content"].lower()

        from app.db.engine import session_scope
        from app.db.models.ticket import Ticket

        with session_scope() as session:
            rows = session.query(Ticket).all()
            assert len(rows) == 1
            assert rows[0].ticket_no == handoff["ticket_no"]
            assert rows[0].session_id == "flow-explicit"
            assert rows[0].reason == "USER_REQUEST"
            assert rows[0].context_complete is True

    def test_card_has_seven_elements(self, client: TestClient) -> None:
        """落库的卡片七要素齐全（编排层传的 str/list 已归一成契约形状）。"""
        data = _turn(client, "我要转人工", session_id="flow-card")
        from app.db.engine import session_scope
        from app.db.models.ticket import Ticket

        with session_scope() as session:
            row = session.query(Ticket).one()
            card = row.context_card
            for key in (
                "summary",
                "verified",
                "tried_actions",
                "unmet_demand",
                "suggested_action",
                "emotion",
                "turn_count",
            ):
                assert card.get(key), key
            # 编排层传的是 str，落库必须是 dict（契约要求）
            assert isinstance(card["emotion"], dict)
            assert isinstance(card["verified"], list)
            assert isinstance(card["tried_actions"], list)
        assert data["handoff"]["ticket_no"] == row.ticket_no

    def test_high_risk_reason(self, client: TestClient) -> None:
        """高风险文本 → HIGH_RISK + URGENT。"""
        data = _turn(client, "我要打 12315 举报你们", session_id="flow-risk")
        assert data["handoff"] is not None
        assert data["handoff"]["reason"] == "HIGH_RISK"
        assert data["handoff"]["rule_id"] == 4

        from app.db.engine import session_scope
        from app.db.models.ticket import Ticket

        with session_scope() as session:
            row = session.query(Ticket).one()
            assert row.priority == "URGENT"

    def test_normal_question_no_ticket(self, client: TestClient) -> None:
        """普通咨询不建单。"""
        data = _turn(client, "这个玩偶多高呀", session_id="flow-normal")
        assert data["handoff"] is None
        from app.db.engine import session_scope
        from app.db.models.ticket import Ticket

        with session_scope() as session:
            assert session.query(Ticket).count() == 0

    def test_repeat_turn_idempotent(self, client: TestClient) -> None:
        """同一会话连续两轮要求转人工，仍只有一张单。"""
        first = _turn(client, "我要转人工", session_id="flow-idem")
        second = _turn(client, "转人工", session_id="flow-idem")
        assert first["handoff"]["ticket_no"] == second["handoff"]["ticket_no"]
        from app.db.engine import session_scope
        from app.db.models.ticket import Ticket

        with session_scope() as session:
            assert session.query(Ticket).count() == 1
