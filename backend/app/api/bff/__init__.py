"""契约 C · BFF 路由聚合（前端唯一入口 `/bff/v1/*`）。

**为什么 BFF 单独成一个包**：BFF 的鉴权（坐席登录态）、字段裁剪、权限裁剪
与内部服务（`/internal/v1/*`，Token 鉴权）完全不同，混在一起会让
「谁能调哪个接口」变得难以审计。

| 子模块 | 覆盖契约 |
| --- | --- |
| `auth` | 登录 / 登出 / 当前用户 / 改密 + 分组字典 |
| `sessions` | 会话列表/详情/上下文卡片/接管/释放/回放 + 对话 + SSE |
| `knowledge` | 知识库 CRUD / 审核 / 导入 / 检索 / 纠错 / 统计 |
| `console` | 12 项指标 / 转人工规则 / 坐席分组 / 渠道 / 工单 |

**本文件是 BFF 的唯一挂载点**：新增子路由在 `_ROUTERS` 里加一行即可，
`app.main` 不需要改动。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.bff import auth as _auth
from app.api.bff import console as _console
from app.api.bff import kb_ops as _kb_ops
from app.api.bff import knowledge as _knowledge
from app.api.bff import sessions as _sessions
from app.api.bff import users as _users

#: BFF 子路由（顺序即 OpenAPI 分组顺序）
_ROUTERS: tuple[APIRouter, ...] = (
    _auth.router,
    _users.router,
    _sessions.router,
    _knowledge.router,
    _kb_ops.router,
    _console.router,
)


def build_bff_router() -> APIRouter:
    """构造 `/bff/v1` 路由。

    Returns:
        已挂载全部 BFF 子路由的 `APIRouter`。
    """
    router = APIRouter(prefix="/bff/v1", tags=["bff"])
    for sub in _ROUTERS:
        router.include_router(sub)
    return router


__all__ = ["build_bff_router"]
