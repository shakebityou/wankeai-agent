"""BFF · 账号登录与自助改密（`/bff/v1/auth/*`、`/bff/v1/meta/groups`）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/bff/v1/auth/login` | 登录（签发登录态 Cookie + token） |
| POST | `/bff/v1/auth/logout` | 登出 |
| GET | `/bff/v1/auth/me` | 当前登录用户 |
| POST | `/bff/v1/auth/password` | 修改自己的密码 |
| GET | `/bff/v1/meta/groups` | 分组字典（表单下拉用） |

**登录态放 Cookie 而不是让前端存 localStorage**：HttpOnly 让脚本读不到 token，
一次 XSS 不至于直接丢会话；代价是前端判断不了「我登录了没」，所以另配
`/auth/me`（与前端 `frontend/mock/services/cookie.ts` 的取舍一致）。

**错误码口径**（与前端 `handlers-auth.ts` 逐条对齐）：

* 缺账号/密码 → `10001`/400；
* 账号不存在、密码错、账号停用 → `10002`/401；
* 账号锁定 → `20001`/429 + `Retry-After` 头（前端据此倒计时禁用按钮）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Header, Request, Response
from pydantic import BaseModel, Field

from app.api.bff.deps import (
    AGENT_TOKEN_HEADER,
    SESSION_COOKIE,
    AgentPrincipalDep,
    _extract_token,
)
from app.core.errors import AppError, BffErrorCode
from app.core.response import ok
from app.services.auth.service import SESSION_TTL_HOURS, get_auth_service

router = APIRouter(tags=["bff-auth"])

__all__ = ["router"]


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(default="", max_length=64, description="登录账号")
    password: str = Field(default="", max_length=128, description="明文口令")


class ChangePasswordRequest(BaseModel):
    """修改自己的密码。"""

    old_password: str = Field(default="", max_length=128, description="原密码")
    new_password: str = Field(default="", max_length=128, description="新密码")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """客户端 IP（优先 `X-Forwarded-For` 第一段，其次直连地址）。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _client_ua(request: Request) -> str:
    """客户端 UA。"""
    return (request.headers.get("user-agent") or "")[:255]


def _set_session_cookie(response: Response, token: str) -> None:
    """写登录态 Cookie（HttpOnly + SameSite=Lax，与前端一致）。"""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    """清登录态 Cookie。"""
    response.delete_cookie(key=SESSION_COOKIE, path="/")


def _session_user(
    wk_session: str | None,
    authorization: str | None,
) -> dict[str, Any]:
    """按登录态取当前用户；无效抛 10002。"""
    token = _extract_token(authorization, wk_session)
    view = get_auth_service().resolve_token(token)
    if not view:
        raise AppError(BffErrorCode.UNAUTHENTICATED, "未登录或登录已过期")
    return view


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post("/auth/login", summary="登录")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """账号密码登录，成功写登录态 Cookie 并返回 token。"""
    result = get_auth_service().login(
        payload.username,
        payload.password,
        ip=_client_ip(request),
        user_agent=_client_ua(request),
    )
    _set_session_cookie(response, result.token)
    return ok(
        {
            "token": result.token,
            "expires_at": result.expires_at.isoformat(),
            "user": result.user,
        },
        "登录成功",
    )


@router.post("/auth/logout", summary="登出")
def logout(
    request: Request,
    response: Response,
    wk_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """吊销当前登录态并清 Cookie（未登录也返回成功）。"""
    token = _extract_token(authorization, wk_session)
    get_auth_service().logout(token, ip=_client_ip(request))
    _clear_session_cookie(response)
    return ok({"ok": True}, "已退出登录")


@router.get("/auth/me", summary="当前登录用户")
def me(
    wk_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """返回当前登录用户（前端据此判断是否需要跳登录页）。"""
    return ok({"user": _session_user(wk_session, authorization)})


@router.post("/auth/password", summary="修改自己的密码")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    wk_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """改密成功后吊销全部会话，前端应跳回登录页。"""
    view = _session_user(wk_session, authorization)
    get_auth_service().change_password(
        str(view["user_id"]),
        payload.old_password,
        payload.new_password,
        ip=_client_ip(request),
    )
    _clear_session_cookie(response)
    return ok({"ok": True}, "密码已修改，请用新密码重新登录")


@router.get("/meta/groups", summary="分组字典")
def meta_groups(principal: AgentPrincipalDep) -> dict[str, Any]:
    """坐席分组字典（账号表单与筛选下拉用）。"""
    _ = principal
    return ok({"items": get_auth_service().list_groups()})
