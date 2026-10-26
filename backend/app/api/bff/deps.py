"""BFF 依赖：坐席登录态、权限裁剪、降级场景。

契约 C（任务书 2.5）要求 BFF 负责三件事：**鉴权、权限裁剪、字段裁剪**。
本模块只做前两件；字段裁剪由 `app.schemas.bff` 的模型天然完成
（模型里没有的字段不会被下发）。

坐席登录态的真实形态是「统一登录（SSO）签发的会话」，本机没有 SSO，
因此用固定 Token + 请求头声明身份。**生产必须把 `bff_require_auth` 打开**，
并把 `verify_agent` 换成校验 SSO JWT 的实现——接口形状不用改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, Header

from app.core.config import get_settings
from app.core.errors import AppError, BffErrorCode, ForbiddenError


def _secret_value(value: object) -> str:
    """把 `SecretStr` / 裸 `str` 统一取成明文。

    Settings 里声明的是 `SecretStr`，但测试常用 `monkeypatch.setattr` 直接换成
    裸字符串——不兼容会让鉴权路径抛 `AttributeError` 而不是 401。
    """
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(value or "")


__all__ = [
    "AGENT_GROUP_HEADER",
    "AGENT_ID_HEADER",
    "AGENT_ROLE_HEADER",
    "AGENT_TOKEN_HEADER",
    "CONFIG_WRITE_ROLES",
    "ROLES",
    "SESSION_COOKIE",
    "AgentPrincipal",
    "AgentPrincipalDep",
    "BffAuthGuard",
    "require_role",
]

#: 请求头：坐席登录态 Token（契约 C 未规定头名，项目组自定，前端 `client.ts` 统一注入）
AGENT_TOKEN_HEADER = "X-Agent-Token"
#: 请求头：坐席 ID（前端从登录态带出）
AGENT_ID_HEADER = "X-Agent-Id"
#: 请求头：坐席角色（agent / supervisor / kb_operator / admin）
AGENT_ROLE_HEADER = "X-Agent-Role"
#: 请求头：坐席所属分组（G1–G6）
AGENT_GROUP_HEADER = "X-Agent-Group"

#: 登录态 Cookie 名（与前端 `frontend/mock/services/cookie.ts` 的 `SESSION_COOKIE` 一致）
SESSION_COOKIE = "wk_session"

#: 允许的角色（小写，内部口径）。`kb_operator` 是知识库运营，只能读写知识库。
ROLES: frozenset[str] = frozenset({"agent", "supervisor", "kb_operator", "admin"})

#: 写配置类接口要求的最低角色（权限裁剪）
CONFIG_WRITE_ROLES: frozenset[str] = frozenset({"supervisor", "admin"})

#: 数据库角色（大写）→ 内部小写口径
_DB_ROLE_MAP: dict[str, str] = {
    "AGENT": "agent",
    "SUPERVISOR": "supervisor",
    "KB_OPERATOR": "kb_operator",
    "ADMIN": "admin",
}


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    """已认证的坐席身份。"""

    agent_id: str
    role: str = "agent"
    group_id: str = ""
    authenticated: bool = False
    username: str = ""
    display_name: str = ""

    @property
    def is_supervisor(self) -> bool:
        """是否有配置写权限。"""
        return self.role in CONFIG_WRITE_ROLES

    def to_dict(self) -> dict[str, str]:
        """导出（审计日志用）。"""
        return {"agent_id": self.agent_id, "role": self.role, "group_id": self.group_id}


def _extract_token(authorization: str | None, cookie: str | None) -> str:
    """从 `Authorization: Bearer` 或登录态 Cookie 里取 token。"""
    if authorization:
        raw = authorization.strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
    return (cookie or "").strip()


def _principal_from_db(token: str) -> AgentPrincipal | None:
    """用登录态 token 查库换身份；查不到/库不可用一律 None。"""
    if not token:
        return None
    try:
        from app.services.auth.service import get_auth_service  # noqa: PLC0415

        view = get_auth_service().resolve_token(token)
    except Exception:  # noqa: BLE001 - 表未建/DB 抖动时退回静态 Token 模式
        return None
    if not view:
        return None
    role = _DB_ROLE_MAP.get(str(view.get("role") or ""), "agent")
    return AgentPrincipal(
        agent_id=str(view.get("user_id") or ""),
        role=role,
        group_id=str(view.get("group_id") or ""),
        authenticated=True,
        username=str(view.get("username") or ""),
        display_name=str(view.get("display_name") or ""),
    )


def verify_agent(
    x_agent_token: Annotated[str | None, Header(alias=AGENT_TOKEN_HEADER)] = None,
    x_agent_id: Annotated[str | None, Header(alias=AGENT_ID_HEADER)] = None,
    x_agent_role: Annotated[str | None, Header(alias=AGENT_ROLE_HEADER)] = None,
    x_agent_group: Annotated[str | None, Header(alias=AGENT_GROUP_HEADER)] = None,
    wk_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AgentPrincipal:
    """校验坐席登录态。

    **两种身份来源，数据库登录态优先**：

    1. 登录态 token（`Authorization: Bearer …` 或 `wk_session` Cookie）——真实
       坐席登录后由 `/bff/v1/auth/login` 签发，查 `auth_sessions` 换出角色与分组；
    2. 静态 Token（`X-Agent-Token` + `X-Agent-*` 请求头）——运维脚本/联调/自检用，
       生产环境靠 `bff_require_auth` 打开后强制校验。

    规则：

    * `bff_require_auth=False`（本地/测试）时**放行**，缺 Token 记为匿名坐席；
    * 开启后两种来源都不成立 → `10002`（401），不区分「没配」与「配错」，避免探测；
    * 角色不在白名单 → `10003`（403）。

    Raises:
        ForbiddenError: 未登录或角色非法。
    """
    token = _extract_token(authorization, wk_session)
    from_db = _principal_from_db(token)
    if from_db is not None:
        return from_db

    settings = get_settings()
    expected = _secret_value(settings.bff_agent_token)

    if settings.bff_require_auth:
        if not x_agent_token or not expected or x_agent_token != expected:
            # 401 / 10002：前端据此跳登录页，不能混成 403（403 是「已登录但没权限」）
            raise AppError(
                BffErrorCode.UNAUTHENTICATED, "坐席未登录或登录态已过期，请重新登录"
            )
        authenticated = True
    else:
        authenticated = bool(x_agent_token and expected and x_agent_token == expected)

    role = (x_agent_role or "agent").strip().lower()
    if role not in ROLES:
        raise ForbiddenError(f"非法角色 {role!r}，可选：{sorted(ROLES)}")

    return AgentPrincipal(
        agent_id=(x_agent_id or "agent_demo").strip() or "agent_demo",
        role=role,
        group_id=(x_agent_group or "").strip(),
        authenticated=authenticated,
    )


AgentPrincipalDep = Annotated[AgentPrincipal, Depends(verify_agent)]

#: 路由级守卫（`APIRouter(dependencies=[BffAuthGuard])`）
BffAuthGuard = Depends(verify_agent)


def require_role(*roles: str):
    """构造「要求指定角色」的依赖。

    Args:
        *roles: 允许的角色集合。

    Returns:
        FastAPI 依赖函数。

    Raises:
        ForbiddenError: 当前坐席角色不在允许集合内。
    """
    allowed = frozenset(roles)

    def _dep(principal: AgentPrincipalDep) -> AgentPrincipal:
        if principal.role not in allowed:
            raise ForbiddenError(
                f"当前角色 {principal.role!r} 无权执行该操作，需要 {sorted(allowed)}"
            )
        return principal

    return _dep
