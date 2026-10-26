"""账号与鉴权（第 31 条 BFF 扩展）。

| 模块 | 职责 |
| --- | --- |
| `password` | scrypt 哈希、token 哈希、口令强度校验 |
| `seed` | 演示账号种子（4 角色 / 7 账号 / 6 分组） |
| `service` | 登录、登出、会话解析、改密、账号管理、操作日志 |
"""

from __future__ import annotations

__all__: list[str] = []
