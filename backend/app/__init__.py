"""玩可爱智能客服 Agent · 后端应用包。

模块化单体（Modular Monolith），7 层架构对应关系：

    L1 接入层        app/api/v1/callback.py, app/services/message_send.py
    L2 多模态预处理   app/services/multimodal/
    L3 对话编排层     app/services/orchestrator/, app/services/session/, app/services/llm_gateway.py
    L4 RAG 检索层     app/services/rag/, app/services/knowledge/
    L5 业务工具层     app/services/tools/
    L6 人工坐席层     app/services/handoff/
    L7 基础层        app/compliance/, app/core/idempotency.py, app/core/ratelimit.py

层间通过 app/schemas 中的契约模型通信，为二期微服务拆分预留边界。
"""

__version__ = "1.0.0"
