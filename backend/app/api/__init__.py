"""API 层包（L1 接入层 / 契约 B 内部服务 / 契约 C BFF）。

路由分三组，边界严格：

| 前缀 | 面向 | 契约 | 鉴权 |
| --- | --- | --- | --- |
| `/health`、`/metrics` | 运维探针 | — | 无 |
| `/internal/v1/*` | 项目组内部服务 | 契约 B（任务书 2.4） | `X-Internal-Token` |
| `/bff/v1/*` | 前端坐席台 / 运营后台 | 契约 C（任务书 2.5） | 登录态（后续接） |
| `/api/v1/*` | 商家回调入口 | 契约 A（任务书 2.3） | 回调签名 |

**前端只允许访问 `/bff/v1/*`**（任务书 4.5.2），`/internal/v1/*` 不对前端暴露。
"""

from __future__ import annotations

__all__: list[str] = []
