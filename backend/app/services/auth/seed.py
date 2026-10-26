"""账号种子数据 —— 首次启动自动写入，已有数据则跳过。

**演示口径**：7 个账号统一口令 `Wk@2026demo`（见 `DEMO_PASSWORD`）。
真实上线时这里只应保留超管初始密码 + `must_change=1`，强迫首登改密。

**为什么账号带分组**：权限矩阵里坐席只能看「本组会话」，分组不是装饰字段，
它直接决定 `/bff/v1/sessions` 返回哪些数据（见 `app/api/bff/sessions.py`）。

**幂等**：只在 `users` 表为空时写入。种子不覆盖已有账号——运营改过的密码、
停用的账号，重启后不能被种子悄悄改回去。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.db.engine import session_scope
from app.db.models.auth import (
    DEMO_PASSWORD,
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_KB_OPERATOR,
    ROLE_SUPERVISOR,
    STATUS_ACTIVE,
    Role,
    User,
)
from app.services.auth.password import hash_password

logger = logging.getLogger(__name__)

#: 角色种子（与前端 `SEED_ROLES` 一致）
SEED_ROLES: tuple[dict[str, object], ...] = (
    {"role_code": ROLE_AGENT, "role_name": "坐席", "landing": "/console/sessions", "sort_order": 1},
    {
        "role_code": ROLE_SUPERVISOR,
        "role_name": "客服主管",
        "landing": "/console/sessions",
        "sort_order": 2,
    },
    {
        "role_code": ROLE_KB_OPERATOR,
        "role_name": "知识库运营",
        "landing": "/admin/kb",
        "sort_order": 3,
    },
    {"role_code": ROLE_ADMIN, "role_name": "系统管理员", "landing": "/admin/metrics", "sort_order": 4},
)

#: 账号种子（与前端 `SEED_USERS` 一致）。`U0005`–`U0007` 用于验证
#: 「同角色不同人接管会冲突」的 10005 场景。
SEED_USERS: tuple[dict[str, object], ...] = (
    {"user_id": "U0001", "username": "wangfang", "display_name": "王芳", "role_code": ROLE_AGENT, "group_id": "G3"},
    {"user_id": "U0002", "username": "limin", "display_name": "李敏", "role_code": ROLE_SUPERVISOR, "group_id": "G1"},
    {"user_id": "U0003", "username": "zhangtao", "display_name": "张涛", "role_code": ROLE_KB_OPERATOR, "group_id": None},
    {"user_id": "U0004", "username": "chenjing", "display_name": "陈静", "role_code": ROLE_ADMIN, "group_id": None},
    {"user_id": "U0005", "username": "liuyang", "display_name": "刘洋", "role_code": ROLE_AGENT, "group_id": "G3"},
    {"user_id": "U0006", "username": "zhaolei", "display_name": "赵磊", "role_code": ROLE_AGENT, "group_id": "G1"},
    {"user_id": "U0007", "username": "sunyue", "display_name": "孙悦", "role_code": ROLE_AGENT, "group_id": "G2"},
)


def seed_into(db: OrmSession) -> bool:
    """在给定会话里写入种子（幂等）。

    Args:
        db: 已打开的 ORM 会话。

    Returns:
        是否真的写入了（False 表示已有账号，跳过）。
    """
    existing = db.scalar(select(func.count()).select_from(User)) or 0
    if existing > 0:
        return False

    for role in SEED_ROLES:
        db.merge(Role(**role))  # type: ignore[arg-type]

    # 分组由 BE-14 的 `agent_groups` 维护；这里只在缺失时补，不覆盖运营改过的排班。
    from app.services.handoff.routing import DEFAULT_GROUPS  # noqa: PLC0415

    from app.db.models.ticket import AgentGroup  # noqa: PLC0415

    for group in DEFAULT_GROUPS:
        if db.get(AgentGroup, group.group_id) is None:
            db.add(
                AgentGroup(
                    group_id=group.group_id,
                    group_name=group.group_name,
                    scope=group.scope,
                    agent_count=group.agent_count,
                    supervisor=group.supervisor,
                    online_hours=group.online_hours,
                    channels=list(group.channels),
                    enabled=group.enabled,
                )
            )

    for user in SEED_USERS:
        db.add(
            User(
                user_id=str(user["user_id"]),
                username=str(user["username"]).lower(),
                display_name=str(user["display_name"]),
                role_code=str(user["role_code"]),
                group_id=user["group_id"],  # type: ignore[arg-type]
                password_hash=hash_password(DEMO_PASSWORD),
                status=STATUS_ACTIVE,
                failed_count=0,
                must_change=False,
                last_login_ip="",
                created_by="system",
            )
        )
    return True


def ensure_seeded() -> bool:
    """确保种子已写入（用自己的会话，失败只告警不抛）。

    启动期调用：数据库不可达时不应该让整个应用起不来。

    Returns:
        是否真的写入了。
    """
    try:
        with session_scope() as db:
            written = seed_into(db)
        if written:
            logger.info("账号种子已写入", extra={"extra": {"users": len(SEED_USERS)}})
        return written
    except Exception as exc:  # noqa: BLE001 - 种子失败不阻断启动
        logger.warning("账号种子写入失败：%s (%s)", type(exc).__name__, exc)
        return False


__all__ = ["SEED_ROLES", "SEED_USERS", "ensure_seeded", "seed_into"]
