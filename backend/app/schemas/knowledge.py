"""契约 B · 知识库服务（任务书 2.4.3 节）。

**切分单元按商家《商品分类体系表.csv》「知识库切分单元」列，非项目组自拟**：

| 知识类型 | 切分单元 | 目标长度 |
| --- | --- | --- |
| 商品知识 | 一款一单元（属性化） | 200–400 字 |
| 售后政策 | 一条政策一单元 | 100–300 字 |
| FAQ | 一问一答一单元 | 50–200 字 |
| 长文档 | 标题层级递归 | 300–500 字 |
| 重叠 | — | 15% |
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import KnowledgeType, ReviewStatus

#: 各知识类型的目标长度区间（字），来自商家分类体系表
LENGTH_RANGES: dict[KnowledgeType, tuple[int, int]] = {
    KnowledgeType.PRODUCT: (200, 400),
    KnowledgeType.POLICY: (100, 300),
    KnowledgeType.FAQ: (50, 200),
    KnowledgeType.DOC: (300, 500),
}


class ChunkMetadata(BaseModel):
    """知识块元数据。**完整率必须 100%**（BE-06 DoD）。

    Attributes:
        category: 知识分类（商品/洗护保养/物流时效/售后政策/营销活动/品牌信任…）。
        sub_category: 二级分类。
        sku_ids: 关联 SKU 编码列表。
        knowledge_type: 知识类型，决定切分口径。
        source: 来源文件与位置（可追溯，合规要求）。
        authority: 权威等级，1 最高（政策/官方口径为 1，语料归纳为 3）。
        channel: 适用渠道，`["all"]` 表示全渠道。
        effective_from: 生效日期。
        effective_to: 失效日期，None 表示长期有效。
        brand: 品牌。
        keywords: 检索关键词（提升 BM25 召回）。
        updated_by: 最后修改人。
        version: 版本号，每次更新自增。
        review_status: 审核状态。
        source_row: 源文件行号（可追溯）。
    """

    category: str = Field(default="未分类", description="知识分类")
    sub_category: str | None = Field(default=None, description="二级分类")
    sku_ids: list[str] = Field(default_factory=list, description="关联 SKU")
    knowledge_type: KnowledgeType = Field(
        default=KnowledgeType.DOC, description="知识类型"
    )
    source: str = Field(default="", description="来源文件与位置")
    authority: int = Field(default=2, ge=1, le=5, description="权威等级，1 最高")
    channel: list[str] = Field(default_factory=lambda: ["all"], description="适用渠道")
    effective_from: date | None = Field(default=None, description="生效日期")
    effective_to: date | None = Field(default=None, description="失效日期")
    brand: str = Field(default="玩可爱", description="品牌")
    keywords: list[str] = Field(default_factory=list, description="检索关键词")
    updated_by: str = Field(default="system", description="最后修改人")
    version: int = Field(default=1, ge=1, description="版本号")
    review_status: ReviewStatus = Field(
        default=ReviewStatus.APPROVED, description="审核状态"
    )
    source_row: int | None = Field(default=None, description="源文件行号")

    def completeness(self) -> float:
        """元数据完整率（关键字段非空的占比）。用于 BE-06 DoD 校验。"""
        checks = [
            bool(self.category and self.category != "未分类"),
            self.knowledge_type is not None,
            bool(self.source),
            self.authority is not None,
            bool(self.channel),
            bool(self.updated_by),
            self.version is not None,
            self.review_status is not None,
        ]
        return sum(1 for c in checks if c) / len(checks)


class ChunkRecord(BaseModel):
    """知识块完整记录。

    ⚠️ **本模型严禁包含库存数量与价格字段**：库存 / 价格是会变的数字，
    必须走接口实时查询，进向量库会导致「过期价格被当作事实回答」
    （HC-3 硬约束 + BE-05 DoD 自动化校验项）。
    """

    chunk_id: str = Field(description="知识块 ID，格式 kb_{type}_{序号}")
    content: str = Field(min_length=1, description="知识块正文")
    title: str | None = Field(default=None, description="标题（商品名 / 问题）")
    metadata: ChunkMetadata = Field(
        default_factory=ChunkMetadata, description="元数据"
    )
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")
    enabled: bool = Field(default=True, description="是否启用（软删标记）")

    @property
    def length(self) -> int:
        """正文长度（字符数）。"""
        return len(self.content)

    def length_in_range(self) -> bool:
        """正文长度是否落在该知识类型的目标区间内。"""
        low, high = LENGTH_RANGES.get(
            self.metadata.knowledge_type, (0, 10**9)
        )
        return low <= self.length <= high


class ChunkCreateRequest(BaseModel):
    """新增知识块请求。

    Attributes:
        content: 正文。若提供 `auto_split=True`，则按知识类型的切分口径自动切分。
        title: 标题。
        metadata: 元数据。
        auto_split: 是否自动切分。
        source: 来源文件路径（导入场景）。
    """

    content: str = Field(min_length=1, description="知识块正文")
    title: str | None = Field(default=None, description="标题")
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)
    auto_split: bool = Field(default=True, description="是否自动切分")
    source: str | None = Field(default=None, description="来源文件")


class ChunkUpdateRequest(BaseModel):
    """更新知识块请求。仅传入的字段被更新；`version` 由服务端自增。"""

    content: str | None = Field(default=None, description="新正文")
    title: str | None = Field(default=None, description="新标题")
    metadata: ChunkMetadata | None = Field(default=None, description="新元数据")
    updated_by: str = Field(default="system", description="修改人")
    change_reason: str | None = Field(default=None, description="变更原因")


class ChunkHistoryEntry(BaseModel):
    """版本历史条目（支持回滚）。"""

    chunk_id: str = Field(description="知识块 ID")
    version: int = Field(description="版本号")
    content: str = Field(description="该版本正文")
    metadata: ChunkMetadata = Field(description="该版本元数据")
    updated_by: str = Field(description="修改人")
    updated_at: datetime = Field(description="修改时间")
    change_reason: str | None = Field(default=None, description="变更原因")
    action: str = Field(default="update", description="操作类型 create/update/delete")


class ImportRequest(BaseModel):
    """批量导入请求。

    Attributes:
        source_path: 源文件路径（CSV / XLSX / MD）。相对路径以商家材料包为基准。
        knowledge_type: 知识类型，决定切分口径。
        category: 覆盖分类（源文件无分类列时使用）。
        authority: 覆盖权威等级。
        dry_run: 只做切分预览不落库。
        created_by: 操作人。
    """

    source_path: str = Field(description="源文件路径")
    knowledge_type: KnowledgeType = Field(
        default=KnowledgeType.FAQ, description="知识类型"
    )
    category: str | None = Field(default=None, description="覆盖分类")
    authority: int | None = Field(default=None, ge=1, le=5, description="覆盖权威等级")
    dry_run: bool = Field(default=False, description="只预览不落库")
    created_by: str = Field(default="system", description="操作人")


class ImportPreviewResponse(BaseModel):
    """切分预览响应（BFF `/kb/import/preview` 导入前必看）。"""

    source_path: str = Field(description="源文件路径")
    total_rows: int = Field(default=0, ge=0, description="源文件数据行数")
    total_chunks: int = Field(default=0, ge=0, description="切分后知识块数")
    chunks: list[ChunkRecord] = Field(default_factory=list, description="预览块（截断）")
    warnings: list[str] = Field(default_factory=list, description="切分告警")
    length_out_of_range: int = Field(
        default=0, ge=0, description="长度超出目标区间的块数"
    )


class ImportResult(BaseModel):
    """批量导入结果。"""

    source_path: str = Field(description="源文件路径")
    total_rows: int = Field(default=0, ge=0, description="源行数")
    total_chunks: int = Field(default=0, ge=0, description="切分块数")
    inserted: int = Field(default=0, ge=0, description="新增数")
    updated: int = Field(default=0, ge=0, description="更新数")
    skipped: int = Field(default=0, ge=0, description="跳过数（重复/无效）")
    failed: int = Field(default=0, ge=0, description="失败数")
    errors: list[str] = Field(default_factory=list, description="失败原因（脱敏）")
    duration_ms: int = Field(default=0, ge=0, description="耗时 ms")
    dry_run: bool = Field(default=False, description="是否仅预览")


class ReindexRequest(BaseModel):
    """重建索引请求。

    Attributes:
        rebuild_vectors: 是否重建向量索引。
        rebuild_sparse: 是否重建稀疏索引。
        chunk_ids: 只重建指定块；空表示全量。
    """

    rebuild_vectors: bool = Field(default=True, description="重建向量索引")
    rebuild_sparse: bool = Field(default=True, description="重建稀疏索引")
    chunk_ids: list[str] = Field(default_factory=list, description="指定块 ID")
    created_by: str = Field(default="system", description="操作人")


class ReindexResponse(BaseModel):
    """重建索引响应。**生效延迟 ≤ 5 分钟**（NFR-05）。"""

    total: int = Field(default=0, ge=0, description="处理块数")
    vectors_indexed: int = Field(default=0, ge=0, description="向量写入数")
    sparse_indexed: int = Field(default=0, ge=0, description="稀疏写入数")
    failed: int = Field(default=0, ge=0, description="失败数")
    duration_ms: int = Field(default=0, ge=0, description="耗时 ms")
    effective_at: datetime | None = Field(default=None, description="生效时间")


class ChunkListResponse(BaseModel):
    """知识块列表响应（分页）。"""

    total: int = Field(default=0, ge=0, description="总数")
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=50, description="每页条数")
    items: list[ChunkRecord] = Field(default_factory=list, description="数据")


class KbStats(BaseModel):
    """知识库统计（用于验收核对：74 知识卡 / 126 FAQ / 96 政策）。"""

    total_chunks: int = Field(default=0, ge=0, description="总块数")
    by_type: dict[str, int] = Field(default_factory=dict, description="按知识类型统计")
    by_category: dict[str, int] = Field(default_factory=dict, description="按分类统计")
    enabled_chunks: int = Field(default=0, ge=0, description="已启用块数")
    avg_length: float = Field(default=0.0, description="平均长度")
    metadata_completeness: float = Field(
        default=0.0, description="元数据完整率，目标 1.0"
    )
    last_updated_at: datetime | None = Field(default=None, description="最后更新时间")


class SearchRequest(BaseModel):
    """坐席侧知识检索请求（BFF `/kb/search`）。"""

    query: str = Field(min_length=1, description="查询词")
    filters: dict[str, Any] = Field(default_factory=dict, description="过滤条件")
    top_k: int = Field(default=10, ge=1, le=50, description="返回条数")

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


__all__ = [
    "ChunkCreateRequest",
    "ChunkHistoryEntry",
    "ChunkListResponse",
    "ChunkMetadata",
    "ChunkRecord",
    "ChunkUpdateRequest",
    "ImportPreviewResponse",
    "ImportRequest",
    "ImportResult",
    "KbStats",
    "LENGTH_RANGES",
    "ReindexRequest",
    "ReindexResponse",
    "SearchRequest",
]

