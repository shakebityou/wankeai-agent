"""转人工子包（BE-13 判定 / BE-14 建单路由 / BE-15 离线兜底）。

| 模块 | 职责 |
| --- | --- |
| `rules` | 六条触发规则（可配置，DB 为源，内置默认兜底） |
| `routing` | 坐席分组路由（reason + 渠道 + 意图 → G1~G6） |
| `online` | 服务时段判定与在线/离线话术 |
| `service` | 建单主流程（幂等 / 限流 / 本地队列 / 审计） |

**入口**：`get_handoff_service()`。编排层（`orchestrator._do_handoff`）只认
`service.handoff(...)` 一个方法，内部细节变更不影响它。
"""

from __future__ import annotations

from app.services.handoff.online import (
    FORBIDDEN_WAIT_WORDS,
    is_online,
    now_cst,
    offline_user_message,
    online_user_message,
    parse_online_hours,
)
from app.services.handoff.routing import (
    AgentRouter,
    GroupInfo,
    get_agent_router,
    set_agent_router,
)
from app.services.handoff.rules import (
    DEFAULT_RULES,
    HandoffDecision,
    HandoffRuleEngine,
    RuleSpec,
    get_rule_engine,
    load_rules,
    set_rule_engine,
)
from app.services.handoff.service import (
    HandoffOutcome,
    HandoffService,
    get_handoff_service,
    set_handoff_service,
)

__all__ = [
    "AgentRouter",
    "DEFAULT_RULES",
    "FORBIDDEN_WAIT_WORDS",
    "GroupInfo",
    "HandoffDecision",
    "HandoffOutcome",
    "HandoffRuleEngine",
    "HandoffService",
    "RuleSpec",
    "get_agent_router",
    "get_handoff_service",
    "get_rule_engine",
    "is_online",
    "load_rules",
    "now_cst",
    "offline_user_message",
    "online_user_message",
    "parse_online_hours",
    "set_agent_router",
    "set_handoff_service",
    "set_rule_engine",
]
