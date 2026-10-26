"""契约 B 内部服务路由聚合（任务书 2.4）。

`/internal/v1/*` **只给项目组内部服务调用**，不对前端与商家暴露，
全部依赖 `app.api.deps.verify_internal_token`。

| 子模块 | 契约 | 对应任务 |
| --- | --- | --- |
| `chat` | `POST /internal/v1/chat/turn` | BE-04 |
| `message` | `POST /internal/v1/message/send` 等 | BE-02 |
| `rag` | `POST /internal/v1/rag/retrieve` | BE-05 |
| `kb` | `/internal/v1/kb/*` | BE-06 |
| `tools` | `/internal/v1/tools/*` | BE-07 / BE-08 / BE-09 |
| `handoff` | `POST /internal/v1/handoff` | BE-13 / BE-14 |

**本文件是唯一的挂载点**：新增子路由只需在 `_ROUTERS` 里加一行，
`app.main` 不需要改动。运维探针（`/health`、`/metrics`）在
`app.api.v1.health`，由 `app.main` 直接挂在根路径，**不在本聚合内**——
它们不能被 `/internal/v1` 前缀污染，否则 compose 的 healthcheck 会失效。

**例外**：`app.api.v1.callback` 是**渠道回调**（契约 2.3.4），路径为
`/api/v1/callback/*`，由商家侧调用，既不属于内部服务也不属于探针，
因此**不并入本聚合**，由 `app.main` 单独挂载。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import chat as _chat
from app.api.v1 import handoff as _handoff
from app.api.v1 import kb as _kb
from app.api.v1 import message as _message
from app.api.v1 import multimodal as _multimodal
from app.api.v1 import rag as _rag
from app.api.v1 import tools as _tools

#: 已落地的内部服务子路由（后续任务在此追加；顺序即 OpenAPI 分组顺序）
_ROUTERS: tuple[APIRouter, ...] = (
    _chat.router,
    _message.router,
    _rag.router,
    _kb.router,
    _tools.router,
    _handoff.router,
    _multimodal.router,
)


def build_internal_router() -> APIRouter:
    """构造 `/internal/v1` 路由。

    Returns:
        已挂载全部子路由的 `APIRouter`。当前为空壳（各任务逐步填充），
        空 `APIRouter` 被挂载不会产生任何路由，也不影响 OpenAPI。
    """
    router = APIRouter(prefix="/internal/v1", tags=["internal"])
    for sub in _ROUTERS:
        router.include_router(sub)
    return router


__all__ = ["build_internal_router"]
