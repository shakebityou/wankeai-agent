"""知识库管理路由测试（BE-06 / 任务书 2.4.3 节）。

覆盖 `app/api/v1/kb.py` 的 9 个端点，重点验证四件**会真出事故**的事：

1. **软删而非物理删**——HC-2 要求"可删除可追溯"，物理删无法证明删了什么；
2. **更新先快照**——改错了要能回滚，没快照就只能拍脑袋；
3. **导入后检索缓存必须失效**——缓存 TTL 120s，不失效就会出现
   "知识改了但用户还查到旧答案"，直接违反 BE-05 的 5 分钟生效 DoD；
4. **`dry_run` 绝不写库**——运营点"预览"结果写进去了，是最难收拾的那类事故。

数据库用 `isolated_db` fixture：每个测试模块一个临时 SQLite 文件，
**不碰开发库**（开发库里可能有 `scripts/seed_reference_data.py` 的种子数据，
测试连它会撞唯一键，且失败与否取决于开发机跑没跑过种子脚本）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.rag import RagRetrieveResponse

pytestmark = pytest.mark.db


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[Any]:
    """每用例一个干净临时库，并把全局单例指向它。

    **为什么是函数级而不是模块级**：这些用例会往库里写知识块并断言
    "总数 == 2"。若库在用例间共享，前一个用例写的块会留到下一个，
    断言就取决于执行顺序——这类测试第一遍跑绿灯、加了用例后莫名其妙
    变红，是最浪费时间的一种。每用例一个库文件，隔离代价只有建表几十毫秒。

    **为什么要改全局单例**：`app/api/v1/kb.py` 里的每个端点都通过
    `session_scope()` 拿会话，而 `session_scope()` 走的是
    `get_engine()` 的全局单例。测试不能靠"改环境变量再 reload 模块"
    （Settings 是 `lru_cache` 单例，reload 极易漏），直接把
    `app.db.engine._ENGINE` 指向临时库最可靠。

    Args:
        tmp_path: pytest 每用例独立临时目录。

    Yields:
        临时库的 `Engine`。
    """
    from app.db import engine as engine_mod

    db_path = tmp_path / "kb.db"
    engine = engine_mod.make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    engine_mod.create_all(engine)

    # **必须成对替换 `_ENGINE` 与 `_SESSION_FACTORY`**：`session_scope()` 走的是
    # `get_session_factory()`，而该工厂是全局惰性单例，第一次创建时就把
    # `bind` 钉死在那时的 `get_engine()` 上了。只换 `_ENGINE` 不换工厂，
    # 端点仍连**旧引擎**——表现为「数据库里条数比断言多」，因为旧引擎可能
    # 指向前一个用例的临时库，甚至指向开发库 `backend/.local/wkagent.db`。
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

    **为什么必须带 `X-Internal-Token`**：`tests/conftest.py` 里设了
    `WK_INTERNAL_TOKEN=test_internal_token`，一旦该值非空，
    `verify_internal_token` 就不再走"本地环境放行"分支，所有
    `/internal/v1/*` 都会 403。这不是缺陷而是正确的安全默认值——
    内部接口不该因为跑在 local 就裸奔。

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


def _create_chunk(client: TestClient, **overrides: Any) -> dict:
    """建一个知识块，返回响应里的 data。

    **注意 `knowledge_type` / `category` 属于 `metadata` 子对象**，不是顶层字段：
    `ChunkCreateRequest` 顶层只有 content / title / metadata / auto_split / source。
    放进顶层不会报错（Pydantic 静默忽略），但会落成默认值 `DOC` + `未分类`，
    表现为「明明传了 FAQ，统计里全是 DOC」——这类沉默失败比报错难查得多。

    Args:
        client: 测试客户端。
        **overrides: 覆盖默认字段；metadata 相关键自动合并进 metadata。

    Returns:
        `data` 字典。
    """
    meta: dict[str, Any] = {"knowledge_type": "POLICY", "category": "售后政策"}
    payload: dict[str, Any] = {
        "content": "毛绒玩偶支持 7 天无理由退货，拆封后保持商品完好即可申请。",
        "title": "毛绒玩偶退货规则",
    }
    for key in (
        "knowledge_type",
        "category",
        "sub_category",
        "sku_ids",
        "channel",
        "authority",
    ):
        if key in overrides:
            meta[key] = overrides.pop(key)
    payload["metadata"] = meta
    payload.update(overrides)

    response = client.post("/internal/v1/kb/chunks", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# 查询类
# ---------------------------------------------------------------------------


class TestQueryEndpoints:
    def test_list_empty(self, client: TestClient) -> None:
        """空库返回 0 条而不是报错。"""
        data = client.get("/internal/v1/kb/chunks").json()["data"]
        assert data["total"] == 0
        assert data["items"] == []
        assert data["page"] == 1
        assert data["page_size"] == 20

    def test_list_pagination_and_filters(self, client: TestClient) -> None:
        """分页与三类过滤（类型/分类/关键词）各自生效。"""
        _create_chunk(client, category="售后政策", knowledge_type="POLICY")
        _create_chunk(
            client,
            title="盲盒发货时效",
            content="盲盒下单后 48 小时内发货。",
            category="物流时效",
            knowledge_type="FAQ",
        )

        assert client.get("/internal/v1/kb/chunks").json()["data"]["total"] == 2
        assert (
            client.get("/internal/v1/kb/chunks?knowledge_type=FAQ").json()["data"]["total"] == 1
        )
        assert (
            client.get("/internal/v1/kb/chunks?category=售后政策").json()["data"]["total"] == 1
        )
        assert client.get("/internal/v1/kb/chunks?keyword=盲盒").json()["data"]["total"] == 1
        # 关键词命中正文也应算（不只是标题）
        assert client.get("/internal/v1/kb/chunks?keyword=48 小时").json()["data"]["total"] == 1

        page2 = client.get("/internal/v1/kb/chunks?page=2&page_size=1").json()["data"]
        assert page2["total"] == 2
        assert len(page2["items"]) == 1

    def test_page_size_capped(self, client: TestClient) -> None:
        """`page_size` 超过 50 被拒（契约 2.5.5 上限）。"""
        response = client.get("/internal/v1/kb/chunks?page_size=51")
        assert response.status_code in (400, 422)

    def test_get_chunk_and_404(self, client: TestClient) -> None:
        """按 ID 查得到，查不到返回业务错误码。"""
        created = _create_chunk(client)
        found = client.get(f"/internal/v1/kb/chunks/{created['chunk_id']}")
        assert found.status_code == 200
        assert found.json()["data"]["chunk_id"] == created["chunk_id"]

        missing = client.get("/internal/v1/kb/chunks/kb_manual:does-not-exist")
        assert missing.status_code == 404

    def test_stats(self, client: TestClient) -> None:
        """统计返回总数与类型分布。"""
        _create_chunk(client, knowledge_type="POLICY")
        _create_chunk(client, knowledge_type="FAQ", category="物流时效")
        data = client.get("/internal/v1/kb/stats").json()["data"]
        assert data["total_chunks"] == 2
        assert data.get("by_type", {}).get("POLICY") == 1

    def test_import_jobs_empty(self, client: TestClient) -> None:
        """导入账本默认为空列表。"""
        assert client.get("/internal/v1/kb/import/jobs").json()["data"] == []


# ---------------------------------------------------------------------------
# 写入类
# ---------------------------------------------------------------------------


class TestWriteEndpoints:
    def test_create_generates_unique_id(self, client: TestClient) -> None:
        """两次相同内容的提交生成两个不同 ID（新增语义）。"""
        a = _create_chunk(client)
        b = _create_chunk(client)
        assert a["chunk_id"] != b["chunk_id"]
        assert a["chunk_id"].startswith("kb_manual:")

    def test_update_bumps_version_and_snapshots(self, client: TestClient) -> None:
        """更新后版本 +1，且历史里留有旧值（可回滚的依据）。"""
        created = _create_chunk(client)
        chunk_id = created["chunk_id"]
        before = created["metadata"]["version"]

        updated = client.put(
            f"/internal/v1/kb/chunks/{chunk_id}",
            json={"content": "毛绒玩偶支持 15 天无理由退货（新政策）。"},
        )
        assert updated.status_code == 200, updated.text
        after = updated.json()["data"]["metadata"]["version"]
        assert after > before
        assert "15 天" in updated.json()["data"]["content"]

        history = client.get(f"/internal/v1/kb/chunks/{chunk_id}/history").json()["data"]
        assert len(history) >= 1
        # 历史里必须能找到旧正文，否则"回滚"无法实现
        assert any("7 天无理由" in (h.get("content") or "") for h in history)

    def test_update_missing_returns_404(self, client: TestClient) -> None:
        """更新不存在的块返回 404。"""
        response = client.put(
            "/internal/v1/kb/chunks/kb_manual:nope", json={"content": "x"}
        )
        assert response.status_code == 404

    def test_delete_is_soft(self, client: TestClient) -> None:
        """删除是软删：查不到（对外不可见），但记录仍在库里可追溯。"""
        from app.db.engine import session_scope
        from app.db.models.knowledge import KbChunk

        created = _create_chunk(client)
        chunk_id = created["chunk_id"]

        response = client.delete(f"/internal/v1/kb/chunks/{chunk_id}?deleted_by=tester")
        assert response.status_code == 200
        assert client.get(f"/internal/v1/kb/chunks/{chunk_id}").status_code == 404

        # 物理行还在（软删证据），且记录了操作人
        with session_scope() as session:
            row = session.get(KbChunk, chunk_id)
            assert row is not None, "软删不应用物理 DELETE"
            assert row.deleted_by == "tester"
            assert row.deleted_at is not None
            assert row.enabled is False

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        """删除不存在的块返回 404。"""
        assert client.delete("/internal/v1/kb/chunks/kb_manual:nope").status_code == 404

    def test_feedback_records(self, client: TestClient) -> None:
        """坐席纠错上报落库并返回 ID。"""
        created = _create_chunk(client)
        response = client.post(
            "/internal/v1/kb/feedback",
            json={
                "session_id": "sess-fb-1",
                "chunk_id": created["chunk_id"],
                "answer_text": "机器人给了错答案",
                "reason": "答非所问",
                "expected_answer": "应该这样答",
                "reported_by": "agent_007",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["id"]
        assert data["session_id"] == "sess-fb-1"
        assert data["chunk_id"] == created["chunk_id"]


# ---------------------------------------------------------------------------
# 导入 / 重建（真实商家材料）
# ---------------------------------------------------------------------------


class TestImportEndpoints:
    def test_dry_run_does_not_write(self, client: TestClient) -> None:
        """预览模式不得写库——这是运营最常用的按钮。"""
        from app.db.engine import session_scope
        from app.db.models.knowledge import KbChunk

        response = client.post(
            "/internal/v1/kb/import",
            json={
                "source_path": "data/FAQ知识库.csv",
                "knowledge_type": "FAQ",
                "dry_run": True,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total_chunks"] > 0
        assert data.get("dry_run") is True

        with session_scope() as session:
            assert session.query(KbChunk).count() == 0, "dry_run 不能写库"

    def test_import_faq_then_idempotent_second_run(self, client: TestClient) -> None:
        """首次导入全部 inserted，二次导入全部 skipped（按 chunk_id 判重）。"""
        from app.db.engine import session_scope
        from app.db.models.knowledge import KbChunk

        payload = {
            "source_path": "data/FAQ知识库.csv",
            "knowledge_type": "FAQ",
            "dry_run": False,
        }
        first = client.post("/internal/v1/kb/import", json=payload)
        assert first.status_code == 200, first.text
        d1 = first.json()["data"]
        assert d1["inserted"] > 0
        assert d1["failed"] == 0
        assert d1["updated"] == 0

        total_in_db = d1["inserted"]
        with session_scope() as session:
            assert session.query(KbChunk).count() == total_in_db

        second = client.post("/internal/v1/kb/import", json=payload)
        d2 = second.json()["data"]
        assert d2["inserted"] == 0
        assert d2["skipped"] == total_in_db

        # 导入账本记了两笔
        jobs = client.get("/internal/v1/kb/import/jobs").json()["data"]
        assert len(jobs) == 2

    def test_import_marks_rag_cache_dirty(self, client: TestClient) -> None:
        """导入后必须清检索缓存，否则会出现"知识已改、答案还是旧的"。"""
        from app.schemas.rag import RagRetrieveRequest
        from app.services.rag import service as rag_mod

        # 先塞一个缓存键进去，模拟"导入前有人检索过"。
        # 注意 `_cache_get/_cache_put` 收的是 **RagRetrieveRequest**，
        # 不是字符串 —— 传错类型会被它们自己的 `except Exception` 吞成
        # 「缓存未命中」，用例会误以为缓存机制没生效。
        rag = rag_mod.get_rag_service()
        probe = RagRetrieveRequest(query="导入前的旧问题")
        rag._cache_put(
            probe, RagRetrieveResponse(decision="NO_EVIDENCE", hits=[], top_score=0.0)
        )
        assert rag._cache_get(probe) is not None, "缓存写入没生效，后续断言无意义"

        client.post(
            "/internal/v1/kb/import",
            json={
                "source_path": "data/FAQ知识库.csv",
                "knowledge_type": "FAQ",
                "dry_run": False,
            },
        )

        assert rag._cache_get(probe) is None, "导入后检索缓存必须失效"

    def test_import_missing_file(self, client: TestClient) -> None:
        """源文件不存在返回 404 而不是 500。"""
        response = client.post(
            "/internal/v1/kb/import",
            json={
                "source_path": "data/不存在的文件.csv",
                "knowledge_type": "FAQ",
                "dry_run": True,
            },
        )
        assert response.status_code == 404

    def test_reindex_runs(self, client: TestClient) -> None:
        """重建索引能跑通并报告总数（生效延迟 DoD 的抓手）。"""
        client.post(
            "/internal/v1/kb/import",
            json={
                "source_path": "data/FAQ知识库.csv",
                "knowledge_type": "FAQ",
                "dry_run": False,
            },
        )
        response = client.post(
            "/internal/v1/kb/reindex",
            json={"mode": "full", "knowledge_type": None, "category": None},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data.get("total", 0) > 0
        assert data.get("failed", 0) == 0
