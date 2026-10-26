"""BFF · 账号管理与操作日志（`/bff/v1/users*`、`/bff/v1/audit`）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/bff/v1/users` | 账号列表（含全量统计） |
| POST | `/bff/v1/users` | 新建账号 |
| PUT | `/bff/v1/users/{user_id}` | 编辑账号 |
| POST | `/bff/v1/users/{user_id}/password` | 重置密码 |
| GET | `/bff/v1/audit` | 操作日志 |

**仅系统管理员可访问**（权限矩阵 P14）：账号是权限的源头，让「能改权限的人」
比「拥有权限的人」多一层，否则自我提权只需一步。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from app.api.bff.deps import AgentPrincipal, require_role
from app.core.response import ok
from app.services.auth.service import get_auth_service

router = APIRouter(tags=["bff-user"])

__all__ = ["router"]

#: 仅管理员
_AdminOnly = Annotated[AgentPrincipal, Depends(require_role("admin"))]


def _client_ip(request: Request) -> str:
    """客户端 IP。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


class CreateUserRequest(BaseModel):
    """新建账号请求。"""

    username: str = Field(default="", max_length=64, description="登录账号")
    display_name: str = Field(default="", max_length=64, description="姓名")
    role: str = Field(default="AGENT", max_length=32, description="角色编码")
    group_id: str | None = Field(default=None, max_length=16, description="所属分组")
    password: str = Field(default="", max_length=128, description="初始密码（留空用默认口令）")


class UpdateUserRequest(BaseModel):
    """编辑账号请求（只改传了的字段）。"""

    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, max_length=32)
    group_id: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16, description="ACTIVE / DISABLED")


class ResetPasswordRequest(BaseModel):
    """重置密码请求。"""

    new_password: str = Field(default="", max_length=128)


@router.get("/users", summary="账号列表")
def list_users(
    principal: _AdminOnly,
    keyword: Annotated[str, Query(max_length=64, description="账号或姓名模糊匹配")] = "",
    role: Annotated[str, Query(max_length=32, description="角色编码过滤")] = "",
    status: Annotated[str, Query(max_length=16, description="状态过滤")] = "",
    group_id: Annotated[str, Query(max_length=16, description="分组过滤")] = "",
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=50, description="每页条数")] = 20,
) -> dict[str, Any]:
    """账号列表 + 全量统计（统计不受筛选影响）。"""
    _ = principal
    items, total, summary = get_auth_service().list_users(
        keyword=keyword,
        role=role,
        status=status,
        group_id=group_id,
        page=page,
        page_size=page_size,
    )
    return ok(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
            "summary": summary,
        }
    )


@router.post("/users", summary="新建账号")
def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: _AdminOnly,
) -> dict[str, Any]:
    """新建账号（默认 `must_change=True`，首登需改密）。"""
    view = get_auth_service().create_user(
        username=payload.username,
        display_name=payload.display_name,
        role=payload.role,
        group_id=payload.group_id,
        password=payload.password,
        actor_id=principal.agent_id,
        actor_name=principal.display_name,
        ip=_client_ip(request),
    )
    return ok({"user": view}, "账号已创建")


@router.put("/users/{user_id}", summary="编辑账号")
def update_user(
    payload: UpdateUserRequest,
    request: Request,
    principal: _AdminOnly,
    user_id: Annotated[str, Path(min_length=1, max_length=16)],
) -> dict[str, Any]:
    """编辑账号（不能改自己的角色、不能禁用自己）。"""
    view = get_auth_service().update_user(
        user_id,
        payload.model_dump(exclude_unset=True),
        actor_id=principal.agent_id,
        actor_name=principal.display_name,
        ip=_client_ip(request),
    )
    return ok({"user": view}, "账号已更新")


@router.post("/users/{user_id}/password", summary="重置密码")
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    principal: _AdminOnly,
    user_id: Annotated[str, Path(min_length=1, max_length=16)],
) -> dict[str, Any]:
    """重置他人密码（强制下次登录改密，并吊销该账号全部会话）。"""
    username = get_auth_service().reset_password(
        user_id,
        payload.new_password,
        actor_id=principal.agent_id,
        actor_name=principal.display_name,
        ip=_client_ip(request),
    )
    return ok({"username": username}, "密码已重置，该账号下次登录需修改密码")


@router.get("/audit", summary="操作日志")
def list_audit(
    principal: _AdminOnly,
    limit: Annotated[int, Query(ge=1, le=500, description="最多返回条数")] = 100,
) -> dict[str, Any]:
    """操作日志（倒序，最多 500 条）。"""
    _ = principal
    return ok({"items": get_auth_service().list_audit(limit)})
