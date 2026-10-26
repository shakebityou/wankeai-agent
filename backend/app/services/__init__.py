"""服务层目录（L3–L6 业务实现）。

按任务书 `3.2` 的七层拆分：

* `knowledge/` —— L4 知识库管理服务（`BE-06`）
* `rag/` —— L4 检索服务（`BE-05`）
* `orchestrator/` —— L3 对话编排（`BE-04`）+ 会话状态（`BE-03`）
* `session/` —— L3 会话与多轮状态（`BE-03`）
* `tools/` —— L5 业务工具（`BE-07`/`BE-08`/`BE-09`）
* `handoff/` —— L6 人工坐席（`BE-13`/`BE-14`/`BE-15`）
* `multimodal/` —— L2 多模态预处理（`BE-10`/`BE-11`/`BE-12`）
* `llm_gateway.py` —— L3 LLM 网关（`BE-16`）
* `message_send.py` —— L1 消息发送服务（`BE-02`）

每个子模块只依赖 `app/schemas`（契约）与 `app/adapters`（端口），
不直接 import 其他服务——跨服务调用一律走构造函数注入，
这样单测才能把协作者换成假实现。
"""
