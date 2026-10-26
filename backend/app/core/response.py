"""统一响应包装与分页（契约 C，任务书 2.5.5 节）。

**成功**：`{ "code": 0, "message": "success", "request_id": "...", "data": {...} }`
**失败**：`{ "code": 40001, "message": "...", "request_id": "...", "data": null }`

分页参数统一为 `page`（默认 1）/ `page_size`（默认 20，**最大 50**），
分页响应为 `{ "total", "page", "page_size", "items" }`。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.enums import StrEnum
from app.core.trace import get_trace_id

T = TypeVar("T")

#: 分页每页最大条数（与商家接口口径一致）
MAX_PAGE_SIZE = 50
#: 分页每页默认条数
DEFAULT_PAGE_SIZE = 20


class ResponseCode(StrEnum):
    """响应码。0 为成功，其余见 `app.core.errors`。"""

    SUCCESS = "success"


class ApiResponse(BaseModel, Generic[T]):
    """统一响应包装。

    Attributes:
        code: 0 表示成功；非 0 见 BFF 错误码表。
        message: 面向调用方的简短描述。
        request_id: 本次请求的全链路追踪 ID。
        data: 业务数据，失败时为 None。
    """

    code: int = 0
    message: str = "success"
    request_id: str = Field(default_factory=get_trace_id)
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "success") -> "ApiResponse[T]":
        """构造成功响应。"""
        return cls(code=0, message=message, request_id=get_trace_id(), data=data)

    @classmethod
    def fail(
        cls, code: int, message: str, data: T | None = None
    ) -> "ApiResponse[T]":
        """构造失败响应。"""
        return cls(code=code, message=message, request_id=get_trace_id(), data=data)


class PageParams(BaseModel):
    """分页请求参数。"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"每页条数，最大 {MAX_PAGE_SIZE}",
    )

    @property
    def offset(self) -> int:
        """转换为 SQL OFFSET。"""
        return (self.page - 1) * self.page_size


class PageResult(BaseModel, Generic[T]):
    """分页响应体。"""

    total: int = Field(description="总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: list[T] = Field(default_factory=list, description="当前页数据")

    @classmethod
    def create(
        cls, items: list[T], total: int, params: PageParams
    ) -> "PageResult[T]":
        """由数据与分页参数构造分页响应。"""
        return cls(
            total=total,
            page=params.page,
            page_size=params.page_size,
            items=items,
        )


def paginate(
    items: list[T], params: PageParams, *, total: int | None = None
) -> PageResult[T]:
    """对内存列表分页（本地适配器与测试用）。

    Args:
        items: 全量列表。
        params: 分页参数。
        total: 覆盖总条数；None 时取 `len(items)`。

    Returns:
        分页结果。
    """
    real_total = len(items) if total is None else total
    start = params.offset
    end = start + params.page_size
    return PageResult.create(items[start:end], real_total, params)


def ok(data: Any = None, message: str = "success") -> dict[str, Any]:
    """函数式快捷构造成功响应字典（FastAPI 路由返回值直接可用）。"""
    return {
        "code": 0,
        "message": message,
        "request_id": get_trace_id(),
        "data": data,
    }


def fail(code: int, message: str) -> dict[str, Any]:
    """函数式快捷构造失败响应字典。"""
    return {
        "code": code,
        "message": message,
        "request_id": get_trace_id(),
        "data": None,
    }


__all__ = [
    "ApiResponse",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PageParams",
    "PageResult",
    "ResponseCode",
    "fail",
    "ok",
    "paginate",
]
