"""知识库模型（`BE-06`）。

四张表：
* `kb_chunks` —— chunk 主表（当前版本，检索的真相源）
* `kb_chunk_versions` —— 版本历史（回滚依据，DoD「版本回滚演练通过」）
* `kb_import_jobs` —— 导入任务记录（DoD「74 卡 + 126 FAQ + 96 政策无丢块」的可核对账本）
* `kb_feedback` —— 纠错上报（`FE-08` 闭环）

**与向量库的关系**：向量库（Milvus / Redis vectorset / numpy）只存
`chunk_id + vector + 可过滤元数据`，**正文与版本以本表为真相源**。重建索引时
从本表读全量 chunk 重新 embedding，向量库永远可以丢弃重建。

**`metadata` 字段刻意不含库存/价格**：`FORBIDDEN_FIELDS` 在写入向量库时拦截，
本表也不设这些列——HC-3「会变的数字永远走接口」在存储层就没有落脚点。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.db.types import dt_col, json_col, str_col, text_col

#: 默认租户/品牌（一期单一商家）
DEFAULT_BRAND = "玩可爱"


class KbChunk(TimestampMixin, Base):
    """知识库 chunk（当前生效版本）。

    主键用字符串 `chunk_id` 而非自增整数，原因：`chunk_id` 是向量库与稀疏索引
    的共同主键，重建索引、增量更新、跨库比对都靠它；用自增 ID 会引入一层
    「本地 ID ↔ 向量库 ID」映射，出问题时要两边对账。

    Attributes:
        chunk_id: 业务主键，格式 `kb_{type}_{序号}`，如 `kb_faq_00231`。
        content: chunk 正文（切分后的检索单元）。
        title: 标题（商品名 / 问题 / 政策条目标题）。
        knowledge_type: 知识类型枚举（PRODUCT/POLICY/FAQ/DOC）。
        category: 类目（毛绒玩偶 / 售后政策 / 订单服务 …），检索过滤主维度。
        sub_category: 二级类目。
        sku_ids: 关联 SKU 列表（JSON 数组）。
        channel: 适用渠道列表；`["all"]` 表示全渠道。
        authority: 权威度 1–5（1 最高）。重排时的次要排序依据。
        brand: 品牌。
        keywords: 关键词列表（进 BM25 索引，提升稀疏路召回）。
        source: 来源文件（如 `FAQ知识库.csv`）。
        source_row: 来源行号（可追溯到原始材料）。
        effective_from: 生效开始日期。
        effective_to: 生效结束日期，None 表示长期有效。
        review_status: 审核状态。
        version: 当前版本号（从 1 递增）。
        enabled: 是否启用（停用不删，保留可追溯）。
        updated_by: 最后修改人。
        extra: 扩展字段（JSON），存放不便单独建列的元数据。
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        Index("ix_kb_chunks_type_category", "knowledge_type", "category"),
        Index("ix_kb_chunks_enabled_type", "enabled", "knowledge_type"),
        Index("ix_kb_chunks_source", "source"),
        # 生效期查询是检索热路径（每次 RAG 都按 effective_from <= now 过滤）
        Index("ix_kb_chunks_effective", "effective_from", "effective_to"),
        {"comment": "知识库 chunk 主表（BE-06）"},
    )

    chunk_id: Mapped[str] = mapped_column(str_col(64), primary_key=True)
    content: Mapped[str] = mapped_column(text_col(), nullable=False)
    title: Mapped[str] = mapped_column(str_col(255), default="", nullable=False)
    knowledge_type: Mapped[str] = mapped_column(
        str_col(32), default="DOC", nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(str_col(64), default="未分类", nullable=False)
    sub_category: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    sku_ids: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    channel: Mapped[list[str]] = mapped_column(
        json_col(), default=lambda: ["all"], nullable=False
    )
    authority: Mapped[int] = mapped_column(SmallInteger, default=2, nullable=False)
    brand: Mapped[str] = mapped_column(
        str_col(64), default=DEFAULT_BRAND, nullable=False
    )
    keywords: Mapped[list[str]] = mapped_column(json_col(), default=list, nullable=False)
    source: Mapped[str] = mapped_column(str_col(255), default="", nullable=False)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    review_status: Mapped[str] = mapped_column(
        str_col(16), default="APPROVED", nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[str] = mapped_column(
        str_col(64), default="system", nullable=False
    )
    #: 软删留痕。知识库的删除是「置 enabled=False + 摘索引」而不是物理 DELETE，
    #: 所以必须记下**谁在什么时候删的**——合规要求「可删除可追溯」（HC-2），
    #: 物理删除无法证明删了什么、谁删的。与 `Session`/`Ticket` 不同，本表
    #: 刻意不继承 `SoftDeleteMixin`：那个 mixin 带 `deleted_at`，
    #: 而本表的「删除」语义已经由 `enabled` 表达，再加一列会出现
    #: `enabled=True` 但 `deleted_at` 有值的矛盾状态。
    deleted_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    deleted_by: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )

    versions: Mapped[list[KbChunkVersion]] = relationship(
        back_populates="chunk",
        cascade="all, delete-orphan",
        order_by="KbChunkVersion.version.desc()",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<KbChunk {self.chunk_id} type={self.knowledge_type} v{self.version}>"


class KbChunkVersion(Base):
    """chunk 版本历史。

    **每次内容或元数据变更前，先把旧值快照到本表**，`kb_chunks` 只保留当前值。
    这样回滚只需把某一行拷回主表，不需要 diff 重算。

    Attributes:
        id: 自增主键。
        chunk_id: 关联 chunk。
        version: 该快照对应的版本号。
        action: CREATE / UPDATE / DELETE / ROLLBACK。
        content: 快照正文。
        metadata_snapshot: 快照元数据（完整 JSON）。
        change_reason: 变更原因（人工填写，审计用）。
        updated_by: 操作人。
        created_at: 快照时间。
    """

    __tablename__ = "kb_chunk_versions"
    __table_args__ = (
        UniqueConstraint("chunk_id", "version", name="uq_kb_chunk_versions_chunk_version"),
        Index("ix_kb_chunk_versions_chunk_created", "chunk_id", "created_at"),
        {"comment": "chunk 版本历史，回滚依据（BE-06）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(
        str_col(64), ForeignKey("kb_chunks.chunk_id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(str_col(16), default="UPDATE", nullable=False)
    content: Mapped[str] = mapped_column(text_col(), nullable=False)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(
        json_col(), default=dict, nullable=False
    )
    change_reason: Mapped[str] = mapped_column(
        str_col(255), default="", nullable=False
    )
    updated_by: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    chunk: Mapped[KbChunk] = relationship(back_populates="versions")

    def __repr__(self) -> str:
        return f"<KbChunkVersion {self.chunk_id} v{self.version} {self.action}>"


class KbImportJob(Base):
    """知识库导入任务记录。

    导入是「可核对」动作：DoD 要求 74 + 126 + 96 条全部入库无丢块，
    所以每次导入都留一条账本（总行数 / 总 chunk / 成功 / 失败 / 错误明细），
    事后可复核是哪一步丢了块。

    Attributes:
        id: 自增主键。
        source_path: 源文件路径。
        knowledge_type: 本次导入的知识类型。
        total_rows: 源文件数据行数。
        total_chunks: 切分后 chunk 数。
        inserted: 新增数。
        updated: 更新数。
        skipped: 跳过数（内容未变）。
        failed: 失败数。
        errors: 错误明细（JSON 数组）。
        dry_run: 是否仅预览未落库。
        duration_ms: 耗时（毫秒）。DoD「1000 条 ≤ 5 分钟」的度量依据。
        created_by: 操作人。
        created_at: 导入时间。
    """

    __tablename__ = "kb_import_jobs"
    __table_args__ = (
        Index("ix_kb_import_jobs_created", "created_at"),
        {"comment": "知识库导入任务账本（BE-06）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(str_col(500), nullable=False)
    knowledge_type: Mapped[str] = mapped_column(str_col(32), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list[Any]] = mapped_column(json_col(), default=list, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(str_col(64), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<KbImportJob {self.source_path} rows={self.total_rows} "
            f"chunks={self.total_chunks} failed={self.failed}>"
        )


class KbFeedback(Base):
    """坐席纠错上报（`FE-08` 纠错闭环）。

    坐席在会话中发现答错时一键上报，运营据此修订知识库。**这是 RAG 效果
    闭环的唯一入口**——没有它，bad case 只能靠人工翻日志发现。

    Attributes:
        id: 自增主键。
        session_id: 关联会话。
        chunk_id: 被质疑的 chunk（可能为空：答非所问时没有命中 chunk）。
        answer_text: 机器人当时的回答原文。
        reason: 纠错原因枚举（WRONG_FACT / OUTDATED / MISSING / WRONG_CATEGORY / OTHER）。
        expected_answer: 坐席认为正确的答案。
        status: 处理状态（PENDING / ACCEPTED / REJECTED）。
        reported_by: 上报人（坐席工号）。
        handled_by: 处理人。
        handled_at: 处理时间。
        created_at: 上报时间。
        message_id: 被质疑的消息 ID（BFF 契约 `FeedbackItem.message_id`），
            **幂等键**：同一消息重复上报只保留一条。
        error_type: 坐席视角的错误类型（`FACT_ERROR` / `OUTDATED` /
            `MISSING_KB` / `WRONG_CATEGORY` / `OTHER`）。与内部 `reason`
            并存：`reason` 是运营口径，`error_type` 是坐席下拉框口径。
        chunk_ids: 关联的多个 chunk（一次回答可能拼了多条知识）。
        assignee: 处理人（BFF 契约字段，运营分配给谁）。
        resolved_chunk_id: 修复后关联的新 chunk。
        resolution: 处理结论（运营填写）。
    """

    __tablename__ = "kb_feedback"
    __table_args__ = (
        Index("ix_kb_feedback_status_created", "status", "created_at"),
        Index("ix_kb_feedback_chunk", "chunk_id"),
        {"comment": "坐席纠错上报，RAG 效果闭环（BE-06 / FE-08）"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    chunk_id: Mapped[str | None] = mapped_column(str_col(64), nullable=True)
    answer_text: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    reason: Mapped[str] = mapped_column(str_col(32), default="OTHER", nullable=False)
    expected_answer: Mapped[str] = mapped_column(text_col(), default="", nullable=False)
    status: Mapped[str] = mapped_column(str_col(16), default="PENDING", nullable=False)
    reported_by: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    handled_by: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    handled_at: Mapped[datetime | None] = mapped_column(dt_col(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        dt_col(), default=utcnow, nullable=False
    )

    # ── BFF 契约补充列（FE-08 纠错闭环的坐席视角字段）──────────────
    message_id: Mapped[str] = mapped_column(
        str_col(64), default="", nullable=False
    )
    error_type: Mapped[str] = mapped_column(
        str_col(32), default="OTHER", nullable=False
    )
    chunk_ids: Mapped[list] = mapped_column(
        json_col(), default=list, nullable=False
    )
    assignee: Mapped[str] = mapped_column(str_col(64), default="", nullable=False)
    resolved_chunk_id: Mapped[str | None] = mapped_column(
        str_col(64), nullable=True
    )
    resolution: Mapped[str] = mapped_column(
        text_col(), default="", nullable=False
    )
    comment: Mapped[str] = mapped_column(
        text_col(), default="", nullable=False
    )

    def __repr__(self) -> str:
        return f"<KbFeedback {self.id} chunk={self.chunk_id} status={self.status}>"


__all__ = ["DEFAULT_BRAND", "KbChunk", "KbChunkVersion", "KbFeedback", "KbImportJob"]
