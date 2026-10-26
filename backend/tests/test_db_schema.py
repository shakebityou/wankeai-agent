"""Schema 与迁移测试（第 2 步 DoD 验证）。

覆盖四类断言：

1. **表结构完整性**：14 张表全部存在，且与 `Base.metadata` 一致（防「加了模型忘
   生成迁移」——这是最容易溜过去的缺陷，因为模型改了但没跑迁移，代码照样跑，
   只是某个字段读不到）。
2. **中文字段与 JSON 往返**：中文正文无损、JSON 不是 `\\uXXXX` 转义。
3. **幂等关键唯一键**：`messages.msg_id` / `tickets.session_id` / `turns.turn_id`。
4. **外键与级联**：孤儿行被拒、删父行带走子行（GDPR 可删除）。

**为什么用 SQLite 跑而不用 SQL Server**：单测必须零外部依赖（conftest 的设计
原则 1）。SQL Server 的等价性由 `scripts/check_db_schema.py --mssql` 覆盖，
那个脚本手动跑。两者合起来才是完整的迁移验证。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect, select, text

from app.db import Base, session_scope
from app.db.models import (
    AgentGroup,
    ChannelConfig,
    HandoffRule,
    KbChunk,
    KbChunkVersion,
    KbFeedback,
    KbImportJob,
    Message,
    Session,
    SessionState,
    Ticket,
    TicketEvent,
    Turn,
)

pytestmark = pytest.mark.db

# 17 张业务表（不含 alembic_version）
EXPECTED_TABLES = {
    "agent_groups",
    "audit_logs",
    "auth_sessions",
    "channels",
    "handoff_rules",
    "kb_chunk_versions",
    "kb_chunks",
    "kb_feedback",
    "kb_import_jobs",
    "messages",
    "roles",
    "session_states",
    "sessions",
    "ticket_events",
    "tickets",
    "turns",
    "users",
}


@pytest.fixture(scope="module")
def sqlite_engine(tmp_path_factory):
    """模块级 SQLite 引擎（独立临时库，模块内共享，结束销毁）。

    **必须用独立的临时库文件**，不能用 `get_engine()` 的默认库
    （`backend/.local/wkagent.db`）：那是本地开发库，里面可能有
    `scripts/seed_reference_data.py` 灌进去的 6 条坐席分组 / 5 个渠道 /
    6 条转人工规则。测试若连它，`test_channel_config_fields` 这类
    「先插一条 TAOBAO 再断言」的用例会直接撞 `UNIQUE constraint failed`
    ——而且失败原因取决于开发机有没有跑过种子脚本，属于最难查的一类脏测试。
    """
    from app.db.engine import create_all, dispose_engine, make_engine, reset_engine

    db_path = tmp_path_factory.mktemp("dbschema") / "schema.db"
    engine = make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()
    dispose_engine()
    reset_engine()


@pytest.fixture()
def db(sqlite_engine):
    """每用例一个独立会话，结束回滚（不污染其他用例）。

    **必须绑定 `sqlite_engine`**，不能用全局 `get_session_factory()`：
    后者连的是 `Settings.resolved_db_url`（本地开发库），与上面那个
    临时引擎不是同一个库，会导致「表建在 A 库、用例去 B 库查」而报
    `no such table`。
    """
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(
        bind=sqlite_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# =============================================================================
# 1. 表结构
# =============================================================================
class TestSchemaStructure:
    """表结构断言。"""

    def test_all_expected_tables_exist(self, sqlite_engine) -> None:
        """14 张业务表全部建出（防加模型忘生成迁移）。"""
        tables = set(inspect(sqlite_engine).get_table_names())
        missing = EXPECTED_TABLES - tables
        assert not missing, f"缺少表: {sorted(missing)}"

    def test_no_unexpected_tables(self, sqlite_engine) -> None:
        """没有多余表（防 create_all 与迁移漂移）。"""
        tables = set(inspect(sqlite_engine).get_table_names()) - {"alembic_version"}
        extra = tables - EXPECTED_TABLES
        assert not extra, f"多出表: {sorted(extra)}"

    def test_metadata_matches_database(self, sqlite_engine) -> None:
        """ORM 元数据与真实库一致。"""
        db_tables = set(inspect(sqlite_engine).get_table_names()) - {"alembic_version"}
        model_tables = set(Base.metadata.tables.keys())
        assert db_tables == model_tables

    def test_engine_ping(self, sqlite_engine) -> None:
        """连接可用。"""
        with sqlite_engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1


# =============================================================================
# 2. 中文字段与 JSON 往返
# =============================================================================
class TestChineseRoundTrip:
    """中文与 JSON 字段的写入→读出一致性。"""

    def test_chinese_content_round_trip(self, db) -> None:
        """中文正文往返无损。"""
        db.add(
            KbChunk(
                chunk_id="kb_test_zh_001",
                content="奶油小熊 20cm 毛绒玩偶，水晶超柔材质，PP 棉填充。",
                title="奶油小熊 20cm",
                knowledge_type="PRODUCT",
                category="毛绒玩偶",
                sku_ids=["WK-BEAR-20"],
                keywords=["奶油小熊", "毛绒", "20cm"],
            )
        )
        db.flush()

        found = db.get(KbChunk, "kb_test_zh_001")
        assert found is not None
        assert found.content == "奶油小熊 20cm 毛绒玩偶，水晶超柔材质，PP 棉填充。"
        assert found.sku_ids == ["WK-BEAR-20"]
        assert found.keywords == ["奶油小熊", "毛绒", "20cm"]
        assert found.category == "毛绒玩偶"

    def test_json_not_ascii_escaped(self, sqlite_engine, db) -> None:
        """JSON 字段在库里不是 `\\uXXXX` 转义（ensure_ascii 坑回归）。

        这个断言的意义：`json.dumps` 默认 `ensure_ascii=True` 会把中文写成
        `\\u6bdb\\u7ed2...`。存进去再读回来**值是对的**（Python 会解回来），
        但用 SQL 的 `LIKE '%毛绒%'` 或 `JSON_VALUE` 就搜不到——这类缺陷
        只在「用 SQL 直接查 JSON 字段」时才暴露。所以这里绕开 ORM，
        用裸 SQL 读回存储原文。

        同类坑在 Redis vectorset 探测时也踩过（`scripts/probe_vset_encoding.py`），
        根因相同：默认参数在本地跑没问题，一上中文就静默失效。
        """
        db.add(
            KbChunk(
                chunk_id="kb_test_json_raw",
                content="测试",
                knowledge_type="PRODUCT",
                sku_ids=["WK-BEAR-20"],
                keywords=["奶油小熊", "毛绒"],
            )
        )
        db.flush()
        db.commit()

        with sqlite_engine.connect() as conn:
            raw = conn.execute(
                text("SELECT sku_ids, keywords FROM kb_chunks WHERE chunk_id = :cid"),
                {"cid": "kb_test_json_raw"},
            ).one()

        assert "\\u" not in raw[0], f"sku_ids 被转义: {raw[0]!r}"
        assert "\\u" not in raw[1], f"keywords 被转义: {raw[1]!r}"
        assert "奶油小熊" in raw[1], f"中文关键词应以明文入库: {raw[1]!r}"

    def test_timestamp_has_timezone(self, db) -> None:
        """时间戳带时区信息。"""
        db.add(
            Session(
                session_id="sess_test_tz",
                buyer_id="buyer_001",
                channel="TAOBAO",
            )
        )
        db.flush()
        found = db.get(Session, "sess_test_tz")
        assert found is not None
        assert found.created_at.tzinfo is not None

    def test_date_field_round_trip(self, db) -> None:
        """Date 字段往返无损。"""
        db.add(
            KbChunk(
                chunk_id="kb_test_date",
                content="测试",
                knowledge_type="PRODUCT",
                effective_from=date(2024, 3, 15),
                effective_to=date(2026, 12, 31),
            )
        )
        db.flush()
        found = db.get(KbChunk, "kb_test_date")
        assert found is not None
        assert found.effective_from == date(2024, 3, 15)
        assert found.effective_to == date(2026, 12, 31)


# =============================================================================
# 3. 幂等关键唯一键
# =============================================================================
class TestIdempotencyConstraints:
    """幂等相关唯一约束（幂等的数据库侧保障）。"""

    def test_msg_id_unique(self, db) -> None:
        """`messages.msg_id` 唯一：回调重推只落一条。"""
        db.add(Session(session_id="sess_dup", buyer_id="b1", channel="TAOBAO"))
        db.add(Message(msg_id="msg_dup_1", session_id="sess_dup"))
        db.flush()

        with pytest.raises(Exception):  # noqa: BLE001  IntegrityError 由方言决定
            db.add(Message(msg_id="msg_dup_1", session_id="sess_dup"))
            db.flush()
        db.rollback()

    def test_session_id_unique_on_tickets(self, db) -> None:
        """`tickets.session_id` 唯一：同会话重复转人工只生成 1 张工单。"""
        db.add(Session(session_id="sess_tk", buyer_id="b1", channel="TAOBAO"))
        db.add(Ticket(ticket_no="TS202601010001", session_id="sess_tk"))
        db.flush()

        with pytest.raises(Exception):  # noqa: BLE001
            db.add(Ticket(ticket_no="TS202601010002", session_id="sess_tk"))
            db.flush()
        db.rollback()

    def test_turn_id_unique(self, db) -> None:
        """`turns.turn_id` 唯一：同一轮重复写入被拒。

        注意不是 `(session_id, turn_index)` 组合唯一——`turn_id` 是业务 ID
        （含时间戳），单论它就够；组合索引只用于查询加速（见模型 docstring）。
        """
        db.add(Session(session_id="sess_turn", buyer_id="b1", channel="TAOBAO"))
        db.add(Turn(turn_id="turn_001", session_id="sess_turn", turn_index=1))
        db.flush()

        with pytest.raises(Exception):  # noqa: BLE001
            db.add(Turn(turn_id="turn_001", session_id="sess_turn", turn_index=1))
            db.flush()
        db.rollback()


# =============================================================================
# 4. 外键与级联
# =============================================================================
class TestForeignKeys:
    """外键约束与级联删除。"""

    def test_orphan_message_rejected(self, db) -> None:
        """孤儿消息被拒（会话不存在）。"""
        with pytest.raises(Exception):  # noqa: BLE001
            db.add(Message(msg_id="msg_orphan", session_id="sess_不存在"))
            db.flush()
        db.rollback()

    def test_cascade_delete_messages(self, db) -> None:
        """删会话带走消息（GDPR 可删除要求）。"""
        db.add(Session(session_id="sess_cascade", buyer_id="b1", channel="TAOBAO"))
        db.add(Message(msg_id="msg_c1", session_id="sess_cascade"))
        db.add(Message(msg_id="msg_c2", session_id="sess_cascade"))
        db.flush()

        session = db.get(Session, "sess_cascade")
        assert session is not None
        db.delete(session)
        db.flush()

        left = db.scalars(
            select(Message).where(Message.session_id == "sess_cascade")
        ).all()
        assert not left

    def test_cascade_delete_ticket_events(self, db) -> None:
        """删工单带走事件流水。"""
        db.add(Session(session_id="sess_tkev", buyer_id="b1", channel="TAOBAO"))
        db.add(Ticket(ticket_no="TS202601020001", session_id="sess_tkev"))
        db.add(TicketEvent(ticket_no="TS202601020001", event="CREATED"))
        db.flush()

        ticket = db.get(Ticket, "TS202601020001")
        assert ticket is not None
        db.delete(ticket)
        db.flush()

        left = db.scalars(
            select(TicketEvent).where(TicketEvent.ticket_no == "TS202601020001")
        ).all()
        assert not left


# =============================================================================
# 5. 参考数据模型
# =============================================================================
class TestReferenceModels:
    """参考数据模型（agent_groups / handoff_rules / channels）。"""

    def test_agent_group_fields(self, db) -> None:
        """坐席分组字段（对齐商家 CSV）。"""
        db.add(
            AgentGroup(
                group_id="G1",
                group_name="售前咨询组",
                agent_count=12,
                online_hours="09:00–22:00",
                channels=["TAOBAO", "TMALL", "PDD", "DOUYIN", "WECHAT"],
                daily_volume=1800,
                peak_volume=3200,
            )
        )
        db.flush()
        found = db.get(AgentGroup, "G1")
        assert found is not None
        assert found.group_name == "售前咨询组"
        assert found.channels == ["TAOBAO", "TMALL", "PDD", "DOUYIN", "WECHAT"]
        assert found.online_hours == "09:00–22:00"  # 全角连接号原样保留

    def test_handoff_rule_fields(self, db) -> None:
        """转人工规则字段。"""
        db.add(
            HandoffRule(
                rule_id=1,
                name="用户显式请求人工",
                reason="USER_REQUEST",
                keywords=["转人工", "人工客服"],
                priority=100,
            )
        )
        db.flush()
        found = db.get(HandoffRule, 1)
        assert found is not None
        assert found.reason == "USER_REQUEST"
        assert found.keywords == ["转人工", "人工客服"]
        assert found.priority == 100

    def test_channel_config_fields(self, db) -> None:
        """渠道配置字段。"""
        db.add(
            ChannelConfig(
                channel="TAOBAO",
                name="淘宝千牛",
                enabled=True,
                authorized=False,
                auth_expire_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )
        )
        db.flush()
        found = db.get(ChannelConfig, "TAOBAO")
        assert found is not None
        assert found.authorized is False
        assert found.auth_expire_at is not None
        assert found.auth_expire_at.tzinfo is not None

    def test_channel_no_secret_field(self, db) -> None:
        """渠道表**不得**有密钥字段（S-20 强制要求，用 schema 断言守住）。

        这不是「忘了加」，而是「刻意不加」。密钥字段一旦进了 schema，
        迟早有人往里写值，那时就违反商家《数据合规要求说明》了。
        """
        columns = {c.name for c in ChannelConfig.__table__.columns}
        forbidden = {"app_secret", "internal_token", "secret", "token", "password"}
        leaked = columns & forbidden
        assert not leaked, f"渠道表包含密钥字段: {sorted(leaked)}"


# =============================================================================
# 6. 知识库模型特有断言
# =============================================================================
class TestKnowledgeModels:
    """知识库模型的专用断言。"""

    def test_kb_import_job_stats(self, db) -> None:
        """导入任务的统计字段（BE-06 DoD 批量导入 1000 条 ≤5 分钟）。"""
        db.add(
            KbImportJob(
                source_path="商品知识卡.csv",
                knowledge_type="PRODUCT",
                total_rows=74,
                total_chunks=74,
                inserted=74,
                updated=0,
                failed=0,
                duration_ms=12000,
            )
        )
        db.flush()
        found = db.scalars(
            select(KbImportJob).where(KbImportJob.source_path == "商品知识卡.csv")
        ).one()
        assert found.total_chunks == 74
        assert found.duration_ms == 12000

    def test_kb_feedback_fields(self, db) -> None:
        """知识反馈（FE-08 纠错闭环）。"""
        db.add(
            KbFeedback(
                session_id="sess_1",
                chunk_id="kb_product_0001",
                answer_text="机器人当时的回答",
                reason="WRONG_FACT",
                expected_answer="应该是 3 岁以上",
                reported_by="客服小李",
            )
        )
        db.flush()
        found = db.scalars(
            select(KbFeedback).where(KbFeedback.session_id == "sess_1")
        ).one()
        assert found.expected_answer == "应该是 3 岁以上"
        assert found.reason == "WRONG_FACT"
        assert found.status == "PENDING"

    def test_version_history_snapshot(self, db) -> None:
        """版本历史快照（BE-06 版本管理，变更可追溯）。"""
        db.add(
            KbChunk(
                chunk_id="kb_ver_001",
                content="v2 正文",
                knowledge_type="FAQ",
                version=2,
            )
        )
        db.add(
            KbChunkVersion(
                chunk_id="kb_ver_001",
                version=1,
                action="CREATE",
                content="v1 正文",
                metadata_snapshot={"category": "洗护保养", "authority": 1},
                change_reason="初始导入",
                updated_by="system",
            )
        )
        db.flush()

        versions = db.scalars(
            select(KbChunkVersion).where(KbChunkVersion.chunk_id == "kb_ver_001")
        ).all()
        assert len(versions) == 1
        assert versions[0].metadata_snapshot["category"] == "洗护保养"
        assert versions[0].metadata_snapshot["authority"] == 1
