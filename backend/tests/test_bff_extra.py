"""BFF 补充端点测试（第 8 条：补齐 mock 专有的 20 条路由）。

覆盖 5 组共 20 条「前端 mock 有、后端原先没有」的端点：

| 组 | 端点 |
| --- | --- |
| 账号 | `POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、`POST /auth/password` |
| 用户 | `GET/POST /users`、`PUT /users/{id}`、`POST /users/{id}/password`、`GET /audit` |
| 字典/指标 | `GET /meta/groups`、`GET /sessions/summary`、`GET /metrics/distribution` |
| 知识库 | `POST /kb/import/preview`、`GET /kb/import/{id}/progress`、`POST /kb/chunks/{id}/rollback`、`GET/PUT /kb/feedback` |
| 配置 | `PUT /config/channels/{ch}`、`GET /config/agent-groups/conflicts`、`PUT /config/handoff-rules/test` |

**重点验证四件会真出事故的事**：

1. **登录失败计数要落库**——`session_scope()` 遇异常会 rollback，若在事务里直接
   `raise`，`failed_count` 永远不增，账号锁死形同虚设（实现时踩过这个坑）；
2. **纠错上报幂等**——同一 `message_id` 连点两次不能产生两条待办；
3. **导入预览→提交的任务台账**——剔除的单元必须如实进 `failures`，状态变
   `PARTIAL_FAILED`，否则运营以为全部导入成功；
4. **回滚是「新增一个版本」而不是「删版本」**——审计链不能断，回滚后还能再回滚。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db

DEMO_PASSWORD = "Wk@2026demo"


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Any]:
    """每用例一个干净临时库（成对替换 `_ENGINE` 与 `_SESSION_FACTORY`）。"""
    from app.db import engine as engine_mod

    db_path = tmp_path / "bff_extra.db"
    engine = engine_mod.make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    engine_mod.create_all(engine)
    saved = engine_mod._ENGINE
    saved_factory = engine_mod._SESSION_FACTORY
    engine_mod._ENGINE = engine
    engine_mod._SESSION_FACTORY = None
    try:
        from app.services.auth.seed import ensure_seeded

        ensure_seeded()
        yield engine
    finally:
        engine_mod._ENGINE = saved
        engine_mod._SESSION_FACTORY = saved_factory
        engine.dispose()


@pytest.fixture()
def client(isolated_db: Any) -> Iterator[TestClient]:
    """未登录客户端（鉴权开关默认关，等价于「坐席 agent」）。"""
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def admin(client: TestClient) -> TestClient:
    """管理员视角客户端（账号管理接口需要 admin）。"""
    client.headers.update({"X-Agent-Role": "admin", "X-Agent-Id": "U0004"})
    return client


@pytest.fixture()
def supervisor(client: TestClient) -> TestClient:
    """主管视角客户端（知识导入/规则测试需要 supervisor）。"""
    client.headers.update({"X-Agent-Role": "supervisor", "X-Agent-Id": "U0002"})
    return client


def _data(response: Any) -> Any:
    """取统一响应 `data`，失败时把完整响应塞进断言消息。"""
    body = response.json()
    assert response.status_code < 400, f"{response.status_code} {body}"
    assert body["code"] == 0, body
    return body["data"]


# ---------------------------------------------------------------------------
# 账号：登录 / 登出 / 当前用户 / 改密
# ---------------------------------------------------------------------------


class TestAuthEndpoints:
    def test_login_success_sets_cookie(self, client: TestClient) -> None:
        response = client.post(
            "/bff/v1/auth/login",
            json={"username": "wangfang", "password": DEMO_PASSWORD},
        )
        data = _data(response)
        assert data["user"]["display_name"] == "王芳"
        assert data["user"]["role"] == "AGENT"
        assert data["token"]
        assert "wk_session" in response.cookies

    def test_login_username_case_insensitive(self, client: TestClient) -> None:
        data = _data(
            client.post(
                "/bff/v1/auth/login",
                json={"username": "WangFang", "password": DEMO_PASSWORD},
            )
        )
        assert data["user"]["username"] == "wangfang"

    def test_login_wrong_password(self, client: TestClient) -> None:
        response = client.post(
            "/bff/v1/auth/login",
            json={"username": "wangfang", "password": "bad-password"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == 10002

    def test_login_unknown_user(self, client: TestClient) -> None:
        response = client.post(
            "/bff/v1/auth/login",
            json={"username": "nobody", "password": DEMO_PASSWORD},
        )
        assert response.status_code == 401

    def test_five_failures_lock_account(self, client: TestClient) -> None:
        """5 次错误必须锁 10 分钟——**失败计数要落库**（事务内 raise 会被回滚）。"""
        messages: list[str] = []
        for _ in range(5):
            response = client.post(
                "/bff/v1/auth/login",
                json={"username": "liuyang", "password": "wrong"},
            )
            messages.append(response.json()["message"])
        assert "4" in messages[0] and "1" in messages[3]
        assert "锁定" in messages[4]

        # 锁定期间即使密码正确也进不去
        locked = client.post(
            "/bff/v1/auth/login",
            json={"username": "liuyang", "password": DEMO_PASSWORD},
        )
        assert locked.status_code == 429
        assert locked.json()["code"] == 20001

    def test_me_and_logout(self, client: TestClient) -> None:
        _data(
            client.post(
                "/bff/v1/auth/login",
                json={"username": "wangfang", "password": DEMO_PASSWORD},
            )
        )
        me = _data(client.get("/bff/v1/auth/me"))
        assert me["user"]["username"] == "wangfang"

        _data(client.post("/bff/v1/auth/logout"))
        after = client.get("/bff/v1/auth/me")
        assert after.status_code == 401

    def test_change_password(self, client: TestClient) -> None:
        _data(
            client.post(
                "/bff/v1/auth/login",
                json={"username": "sunyue", "password": DEMO_PASSWORD},
            )
        )
        data = _data(
            client.post(
                "/bff/v1/auth/password",
                json={
                    "old_password": DEMO_PASSWORD,
                    "new_password": "NewPass2026",
                },
            )
        )
        assert data["ok"] is True
        # 旧密码失效、新密码可用
        assert (
            client.post(
                "/bff/v1/auth/login",
                json={"username": "sunyue", "password": DEMO_PASSWORD},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/bff/v1/auth/login",
                json={"username": "sunyue", "password": "NewPass2026"},
            ).status_code
            == 200
        )

    def test_meta_groups(self, client: TestClient) -> None:
        data = _data(client.get("/bff/v1/meta/groups"))
        assert len(data["items"]) == 6
        first = data["items"][0]
        assert {"group_id", "group_name", "channels"} <= set(first)


# ---------------------------------------------------------------------------
# 用户管理 + 审计
# ---------------------------------------------------------------------------


class TestUserEndpoints:
    def test_list_users_summary(self, admin: TestClient) -> None:
        data = _data(admin.get("/bff/v1/users"))
        assert data["total"] == 7
        assert data["summary"]["total"] == 7
        assert data["summary"]["by_role"]["ADMIN"] == 1
        assert all("password_hash" not in item for item in data["items"])

    def test_list_users_filter(self, admin: TestClient) -> None:
        data = _data(admin.get("/bff/v1/users?role=AGENT"))
        assert data["total"] == 4
        # 统计不受筛选影响
        assert data["summary"]["total"] == 7

    def test_create_user(self, admin: TestClient) -> None:
        data = _data(
            admin.post(
                "/bff/v1/users",
                json={
                    "username": "newagent",
                    "display_name": "新坐席",
                    "role": "AGENT",
                    "group_id": "G3",
                },
            )
        )
        assert data["user"]["username"] == "newagent"
        assert data["user"]["must_change"] is True

    def test_create_user_duplicate(self, admin: TestClient) -> None:
        response = admin.post(
            "/bff/v1/users",
            json={"username": "WangFang", "display_name": "重名"},
        )
        assert response.status_code == 400
        assert "已存在" in response.json()["message"]

    def test_update_user(self, admin: TestClient) -> None:
        data = _data(
            admin.put(
                "/bff/v1/users/U0001",
                json={"display_name": "王芳（组长）", "role": "SUPERVISOR"},
            )
        )
        assert data["user"]["display_name"] == "王芳（组长）"
        assert data["user"]["role"] == "SUPERVISOR"

    def test_reset_password(self, admin: TestClient) -> None:
        data = _data(
            admin.post(
                "/bff/v1/users/U0005/password",
                json={"new_password": "Reset2026a"},
            )
        )
        assert data["username"] == "liuyang"
        assert (
            admin.post(
                "/bff/v1/auth/login",
                json={"username": "liuyang", "password": "Reset2026a"},
            ).status_code
            == 200
        )

    def test_agent_cannot_manage_users(self, client: TestClient) -> None:
        response = client.get("/bff/v1/users")
        assert response.status_code == 403
        assert response.json()["code"] == 10003

    def test_audit_log(self, admin: TestClient) -> None:
        _data(
            admin.post(
                "/bff/v1/users",
                json={"username": "audituser", "display_name": "审计"},
            )
        )
        data = _data(admin.get("/bff/v1/audit"))
        assert data["items"]
        assert data["items"][0]["action"] == "USER_CREATE"


# ---------------------------------------------------------------------------
# 会话汇总 + 指标分布
# ---------------------------------------------------------------------------


class TestAggregateEndpoints:
    def test_sessions_summary_not_shadowed(self, client: TestClient) -> None:
        """/sessions/summary 必须排在 /sessions/{session_id} 之前，否则被当 ID 吃进详情。"""
        data = _data(client.get("/bff/v1/sessions/summary"))
        assert set(data) == {"total", "waiting", "taken_over", "handoff"}

    def test_metrics_distribution(self, client: TestClient) -> None:
        data = _data(client.get("/bff/v1/metrics/distribution"))
        assert set(data) == {"channels", "intents", "funnel"}
        assert data["funnel"][0]["stage"] == "会话总量"


# ---------------------------------------------------------------------------
# 知识库：导入预览 / 进度 / 回滚 / 纠错
# ---------------------------------------------------------------------------


class TestKbImportEndpoints:
    def _preview(self, client: TestClient, name: str, data: bytes) -> dict:
        response = client.post(
            "/bff/v1/kb/import/preview",
            files={"file": (name, data, "text/csv")},
        )
        return _data(response)

    def test_preview_csv(self, supervisor: TestClient) -> None:
        csv_text = "问题,答案\n怎么退货,七天无理由\n运费谁承担,满99包邮\n"
        data = self._preview(supervisor, "faq.csv", csv_text.encode())
        assert data["detected_type"] == "FAQ"
        assert data["total_chunks"] == 2
        chunk = data["chunks"][0]
        assert set(chunk) == {
            "index",
            "title",
            "content",
            "char_count",
            "length_flag",
            "source_ref",
        }
        assert data["import_id"].startswith("IMP-")

    def test_preview_empty_file(self, supervisor: TestClient) -> None:
        response = supervisor.post(
            "/bff/v1/kb/import/preview",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        assert response.status_code == 400
        assert response.json()["code"] == 10001

    def test_preview_bad_extension(self, supervisor: TestClient) -> None:
        response = supervisor.post(
            "/bff/v1/kb/import/preview",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert response.status_code == 400
        assert ".pdf" in response.json()["message"]

    def test_preview_markdown(self, supervisor: TestClient) -> None:
        text = (
            "# 售后政策\n\n## 1. 七天无理由\n自签收之日起七天内可无理由退货、换货、退款。\n"
            "## 2. 保修\n保修期内免费维修，运费由商家承担，详见售后政策条款。\n"
        )
        data = self._preview(supervisor, "policy.md", text.encode())
        assert data["detected_type"] == "POLICY"
        assert data["total_chunks"] >= 1

    def test_progress_unknown_task(self, client: TestClient) -> None:
        response = client.get("/bff/v1/kb/import/IMP-NOTEXIST/progress")
        assert response.status_code == 404
        assert response.json()["code"] == 10004

    def test_commit_with_excluded(self, supervisor: TestClient) -> None:
        csv_text = "问题,答案\n怎么退货,七天无理由\n运费谁承担,满99包邮\n尺码怎么选,按身高体重\n"
        preview = self._preview(supervisor, "faq.csv", csv_text.encode())
        progress = _data(
            supervisor.post(
                "/bff/v1/kb/import",
                json={
                    "import_id": preview["import_id"],
                    "kb_type": "FAQ",
                    "category": "售后",
                    "authority": "2",
                    "channel": "TAOBAO",
                    "excluded_indexes": [2],
                },
            )
        )
        assert progress["status"] == "PARTIAL_FAILED"
        assert progress["succeeded"] == 2
        assert progress["failed"] == 1
        assert progress["failures"][0]["row"] == 2

        after = _data(
            supervisor.get(f"/bff/v1/kb/import/{preview['import_id']}/progress")
        )
        assert after["status"] == "PARTIAL_FAILED"
        assert after["finished_at"]

    def test_commit_all_excluded(self, supervisor: TestClient) -> None:
        preview = self._preview(supervisor, "one.csv", b"a,b\n1,2\n")
        response = supervisor.post(
            "/bff/v1/kb/import",
            json={"import_id": preview["import_id"], "excluded_indexes": [1]},
        )
        assert response.status_code == 400
        assert "剔除" in response.json()["message"]

    def test_commit_unknown_task(self, supervisor: TestClient) -> None:
        response = supervisor.post(
            "/bff/v1/kb/import",
            json={"import_id": "IMP-NOPE", "excluded_indexes": []},
        )
        assert response.status_code == 404


class TestKbRollbackEndpoint:
    def _make_chunk(self, client: TestClient) -> str:
        data = _data(
            client.post(
                "/bff/v1/kb/chunks",
                json={
                    "content": "七天无理由退货，商品需保持完好不影响二次销售。",
                    "title": "退货政策",
                    "metadata": {
                        "category": "售后",
                        "knowledge_type": "POLICY",
                        "authority": 2,
                    },
                },
            )
        )
        return data["chunk_id"]

    def test_rollback_restores_content(self, supervisor: TestClient) -> None:
        chunk_id = self._make_chunk(supervisor)
        _data(
            supervisor.put(
                f"/bff/v1/kb/chunks/{chunk_id}",
                json={"content": "修改后的正文，退货需保留吊牌。", "change_reason": "补充吊牌"},
            )
        )
        data = _data(
            supervisor.post(
                f"/bff/v1/kb/chunks/{chunk_id}/rollback", json={"version": 1}
            )
        )
        assert data["version"] > 1
        assert data["chunk_id"] == chunk_id

        detail = _data(supervisor.get(f"/bff/v1/kb/chunks/{chunk_id}"))
        assert "七天无理由" in detail["content"]

    def test_rollback_unknown_version(self, supervisor: TestClient) -> None:
        chunk_id = self._make_chunk(supervisor)
        response = supervisor.post(
            f"/bff/v1/kb/chunks/{chunk_id}/rollback", json={"version": 99}
        )
        assert response.status_code == 404
        assert "未找到版本" in response.json()["message"]

    def test_rollback_unknown_chunk(self, supervisor: TestClient) -> None:
        response = supervisor.post(
            "/bff/v1/kb/chunks/kb_nope/rollback", json={"version": 1}
        )
        assert response.status_code == 404

    def test_rollback_requires_supervisor(self, client: TestClient) -> None:
        chunk_id = self._make_chunk(client)
        response = client.post(
            f"/bff/v1/kb/chunks/{chunk_id}/rollback", json={"version": 1}
        )
        assert response.status_code == 403


class TestKbFeedbackEndpoints:
    def _create(self, client: TestClient, message_id: str = "msg_1") -> dict:
        return _data(
            client.post(
                "/bff/v1/kb/feedback",
                json={
                    "message_id": message_id,
                    "session_id": "sess_1",
                    "chunk_ids": ["kb_faq_a", "kb_faq_b"],
                    "error_type": "FACT_ERROR",
                    "comment": "政策写错了",
                },
            )
        )

    def test_create_feedback(self, client: TestClient) -> None:
        item = self._create(client)
        assert item["feedback_id"].startswith("FB-")
        assert item["status"] == "OPEN"
        assert item["chunk_ids"] == ["kb_faq_a", "kb_faq_b"]
        assert item["assignee"] is None

    def test_create_feedback_idempotent(self, client: TestClient) -> None:
        first = self._create(client)
        second = self._create(client)
        assert second["feedback_id"] == first["feedback_id"]
        assert _data(client.get("/bff/v1/kb/feedback"))["total"] == 1

    def test_create_feedback_requires_fields(self, client: TestClient) -> None:
        response = client.post(
            "/bff/v1/kb/feedback", json={"error_type": "FACT_ERROR"}
        )
        assert response.status_code == 400
        assert response.json()["code"] == 10001

    def test_list_and_filter(self, client: TestClient) -> None:
        self._create(client, "msg_1")
        self._create(client, "msg_2")
        data = _data(client.get("/bff/v1/kb/feedback?error_type=FACT_ERROR"))
        assert data["total"] == 2
        assert data["page"] == 1 and data["page_size"] == 20
        assert _data(client.get("/bff/v1/kb/feedback?error_type=OTHER"))["total"] == 0

    def test_update_feedback(self, supervisor: TestClient) -> None:
        item = self._create(supervisor)
        updated = _data(
            supervisor.put(
                f"/bff/v1/kb/feedback/{item['feedback_id']}",
                json={
                    "status": "RESOLVED",
                    "assignee": "limin",
                    "resolved_chunk_id": "kb_faq_new",
                    "resolution": "已修正政策",
                },
            )
        )
        assert updated["status"] == "RESOLVED"
        assert updated["assignee"] == "limin"
        assert updated["resolved_chunk_id"] == "kb_faq_new"

    def test_update_feedback_rejects_unknown_field(self, supervisor: TestClient) -> None:
        item = self._create(supervisor)
        response = supervisor.put(
            f"/bff/v1/kb/feedback/{item['feedback_id']}",
            json={"comment": "改证据"},
        )
        assert response.status_code == 400
        assert "comment" in response.json()["message"]

    def test_update_feedback_missing(self, supervisor: TestClient) -> None:
        response = supervisor.put(
            "/bff/v1/kb/feedback/FB-9999", json={"status": "CLOSED"}
        )
        assert response.status_code == 404

    def test_update_feedback_bad_status(self, supervisor: TestClient) -> None:
        item = self._create(supervisor)
        response = supervisor.put(
            f"/bff/v1/kb/feedback/{item['feedback_id']}",
            json={"status": "WHATEVER"},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# 配置：渠道授权 / 分组冲突 / 规则沙盘
# ---------------------------------------------------------------------------


class TestConfigExtraEndpoints:
    def test_update_channel(self, supervisor: TestClient) -> None:
        data = _data(
            supervisor.put(
                "/bff/v1/config/channels/DOUYIN",
                json={
                    "service_provider_certified": True,
                    "service_provider_note": "服务商已认证",
                },
            )
        )
        assert data["channel"] == "DOUYIN"
        assert data["service_provider_certified"] is True
        assert data["updated_by"]

    def test_update_unknown_channel(self, supervisor: TestClient) -> None:
        response = supervisor.put(
            "/bff/v1/config/channels/UNKNOWN", json={"service_provider_certified": True}
        )
        assert response.status_code == 404

    def test_update_channel_requires_supervisor(self, client: TestClient) -> None:
        response = client.put(
            "/bff/v1/config/channels/DOUYIN",
            json={"service_provider_certified": True},
        )
        assert response.status_code == 403

    def test_agent_group_conflicts(self, supervisor: TestClient) -> None:
        data = _data(supervisor.get("/bff/v1/config/agent-groups/conflicts"))
        assert "conflicts" in data
        assert isinstance(data["conflicts"], list)

    def test_handoff_rule_test(self, supervisor: TestClient) -> None:
        data = _data(
            supervisor.put(
                "/bff/v1/config/handoff-rules/test", json={"sample_size": 20}
            )
        )
        assert data["sample_size"] == 20
        assert len(data["per_rule"]) == 6
        assert isinstance(data["details"], list)

    def test_handoff_rule_test_with_draft(self, supervisor: TestClient) -> None:
        data = _data(
            supervisor.put(
                "/bff/v1/config/handoff-rules/test",
                json={
                    "sample_size": 10,
                    "rules": [{"rule_id": "2", "enabled": False}],
                },
            )
        )
        assert data["diff_count"] == 1

    def test_handoff_rule_test_requires_supervisor(self, client: TestClient) -> None:
        response = client.put("/bff/v1/config/handoff-rules/test", json={})
        assert response.status_code == 403
