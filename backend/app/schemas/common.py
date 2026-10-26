"""通用模型：筛选条件、时间范围、健康检查。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import HealthStatus, KnowledgeType


class TimeRange(BaseModel):
    """时间范围筛选。"""

    start: datetime | None = Field(default=None, description="起始时间（含）")
    end: datetime | None = Field(default=None, description="结束时间（含）")


class CommonFilters(BaseModel):
    """知识 / 会话 / 工单列表的通用筛选条件。

    Attributes:
        category: 知识分类（商品/售后/物流/营销/品牌）。
        sku_ids: 关联 SKU 编码，任一命中即返回。
        channel: 渠道筛选。
        knowledge_type: 知识类型，决定切分口径。
        keyword: 关键词（用于坐席侧模糊搜索）。
        only_enabled: 是否只返回启用中的条目。
    """

    category: str | None = Field(default=None, description="知识分类")
    sku_ids: list[str] = Field(default_factory=list, description="关联 SKU 编码")
    channel: list[str] = Field(default_factory=list, description="渠道")
    knowledge_type: KnowledgeType | None = Field(
        default=None, description="知识类型"
    )
    keyword: str | None = Field(default=None, description="关键词")
    only_enabled: bool = Field(default=True, description="是否只返回启用中的条目")


class DependencyHealth(BaseModel):
    """单个外部依赖的健康状态。"""

    name: str = Field(description="依赖名，如 postgres / redis / milvus / es")
    status: HealthStatus = Field(description="健康状态")
    latency_ms: float | None = Field(default=None, description="探测耗时（毫秒）")
    detail: str | None = Field(default=None, description="附加说明（不得含敏感信息）")
    impl: str | None = Field(default=None, description="实际使用的实现类名")


class HealthReport(BaseModel):
    """`/health` 与 `/health/deps` 响应体。

    Attributes:
        status: 总体状态。任一依赖 DOWN 则 DEGRADED（降级可用），
            关键依赖（Postgres）DOWN 则为 DOWN。
        version: 应用版本。
        env: 运行环境。
        uptime_seconds: 进程运行时长。
        dependencies: 各依赖明细。
        circuit_breakers: 熔断器快照。
    """

    status: HealthStatus = HealthStatus.UP
    version: str = "1.0.0"
    env: str = "local"
    uptime_seconds: float = 0.0
    dependencies: list[DependencyHealth] = Field(default_factory=list)
    circuit_breakers: list[dict] = Field(default_factory=list)


__all__ = [
    "CommonFilters",
    "DependencyHealth",
    "HealthReport",
    "TimeRange",
]
