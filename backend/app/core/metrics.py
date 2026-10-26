"""Prometheus 指标（任务书 3.1「OpenTelemetry + Prometheus + Grafana + Loki」）。

指标命名统一前缀 `wk_`，标签基数受控——**HTTP 路径标签一律使用路由模板**
（如 `/bff/v1/sessions/{session_id}`）而非真实路径，否则 `session_id` 会把
时间序列基数打爆。

`/metrics` 由 `app.main` 暴露，供 `docker-compose` 的 Prometheus 抓取。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: 独立 registry，避免与 prometheus_client 默认进程指标混在一起（测试可重置）
REGISTRY = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "wk_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "wk_http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# 对话 / RAG / 工具 / 转人工
# ---------------------------------------------------------------------------

CHAT_TURNS_TOTAL = Counter(
    "wk_chat_turns_total",
    "对话轮次总数",
    ["channel", "decision"],
    registry=REGISTRY,
)

RAG_DECISION_TOTAL = Counter(
    "wk_rag_decision_total",
    "RAG 阈值分流次数（M-03 无依据拒答率依赖本指标）",
    ["decision"],
    registry=REGISTRY,
)

RAG_RETRIEVAL_DURATION = Histogram(
    "wk_rag_retrieval_duration_seconds",
    "RAG 检索耗时（BE-05 DoD：P95 ≤ 200ms）",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0),
    registry=REGISTRY,
)

TOOL_CALLS_TOTAL = Counter(
    "wk_tool_calls_total",
    "业务工具调用次数",
    ["tool", "ok", "error_code"],
    registry=REGISTRY,
)

TOOL_CALL_DURATION = Histogram(
    "wk_tool_call_duration_seconds",
    "业务工具调用耗时",
    ["tool"],
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 5.0),
    registry=REGISTRY,
)

HANDOFF_TOTAL = Counter(
    "wk_handoff_total",
    "转人工触发次数（按 5 类 reason）",
    ["reason", "priority"],
    registry=REGISTRY,
)

TICKET_CREATE_TOTAL = Counter(
    "wk_ticket_create_total",
    "工单创建次数",
    ["ok", "idempotent_hit"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# 模型 / 合规 / 限流
# ---------------------------------------------------------------------------

LLM_REQUESTS_TOTAL = Counter(
    "wk_llm_requests_total",
    "LLM 调用次数",
    ["provider", "model", "ok", "cached"],
    registry=REGISTRY,
)

LLM_LATENCY = Histogram(
    "wk_llm_latency_seconds",
    "LLM 调用耗时",
    ["provider"],
    buckets=(0.2, 0.5, 0.8, 1.2, 2.0, 3.0, 5.0, 10.0, 20.0),
    registry=REGISTRY,
)

LLM_TOKENS_TOTAL = Counter(
    "wk_llm_tokens_total",
    "LLM token 消耗（月度预算 ≤ 10000 元的核算依据）",
    ["provider", "kind"],
    registry=REGISTRY,
)

COMPLIANCE_BLOCKED_TOTAL = Counter(
    "wk_compliance_blocked_total",
    "出站内容被合规闸拦截次数（HC-1 外链零输出）",
    ["category"],
    registry=REGISTRY,
)

COMPLIANCE_MASKED_TOTAL = Counter(
    "wk_compliance_masked_total",
    "接口返回值 / 日志被脱敏的次数（BE-17：零完整敏感字段）",
    ["category"],
    registry=REGISTRY,
)

OUTBOUND_SENDS_TOTAL = Counter(
    "wk_outbound_sends_total",
    "消息发送次数（BE-02 DoD：成功率 ≥ 99.9%）",
    ["channel", "msg_type", "ok"],
    registry=REGISTRY,
)

RATE_LIMIT_REJECTED_TOTAL = Counter(
    "wk_rate_limit_rejected_total",
    "被限流拒绝的调用次数",
    ["endpoint"],
    registry=REGISTRY,
)

CALLBACK_SIGNATURE_REJECTED_TOTAL = Counter(
    "wk_callback_signature_rejected_total",
    "回调验签失败被丢弃次数（BE-01 DoD：伪造签名 100% 丢弃）",
    ["channel", "reason"],
    registry=REGISTRY,
)

CALLBACK_RECEIVED_TOTAL = Counter(
    "wk_callback_received_total",
    "渠道回调受理次数（收到的消息条数 / 入队条数 / 重复条数）",
    ["channel", "result"],
    registry=REGISTRY,
)

CIRCUIT_BREAKER_OPEN = Gauge(
    "wk_circuit_breaker_open",
    "熔断器是否打开（1 打开 / 0 关闭）",
    ["name"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# 业务量（Gauge）
# ---------------------------------------------------------------------------

SESSIONS_ACTIVE = Gauge(
    "wk_sessions_active",
    "当前活跃会话数",
    registry=REGISTRY,
)

KB_CHUNKS_TOTAL = Gauge(
    "wk_kb_chunks_total",
    "知识库 chunk 总数",
    ["knowledge_type"],
    registry=REGISTRY,
)

APP_INFO = Gauge(
    "wk_app_info",
    "应用信息（恒为 1，标签携带版本与环境）",
    ["version", "env"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# 便捷记录函数（业务层只调这些，不直接碰指标对象）
# ---------------------------------------------------------------------------


def record_http(method: str, path: str, status: int, duration_seconds: float) -> None:
    """记录一次 HTTP 请求。`path` 必须是路由模板，不是真实路径。"""
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(duration_seconds)


def record_rag_decision(decision: str, duration_seconds: float | None = None) -> None:
    """记录一次 RAG 阈值分流结果。"""
    RAG_DECISION_TOTAL.labels(decision=decision).inc()
    if duration_seconds is not None:
        RAG_RETRIEVAL_DURATION.observe(duration_seconds)


def record_tool_call(
    tool: str,
    ok: bool,
    *,
    error_code: int | None = None,
    duration_seconds: float | None = None,
) -> None:
    """记录一次业务工具调用。"""
    TOOL_CALLS_TOTAL.labels(
        tool=tool, ok=str(ok).lower(), error_code=str(error_code or 0)
    ).inc()
    if duration_seconds is not None:
        TOOL_CALL_DURATION.labels(tool=tool).observe(duration_seconds)


def record_llm(
    provider: str,
    model: str,
    *,
    ok: bool,
    cached: bool = False,
    duration_seconds: float | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """记录一次 LLM 调用（含 token 核算）。"""
    LLM_REQUESTS_TOTAL.labels(
        provider=provider, model=model, ok=str(ok).lower(), cached=str(cached).lower()
    ).inc()
    if duration_seconds is not None:
        LLM_LATENCY.labels(provider=provider).observe(duration_seconds)
    if prompt_tokens:
        LLM_TOKENS_TOTAL.labels(provider=provider, kind="prompt").inc(prompt_tokens)
    if completion_tokens:
        LLM_TOKENS_TOTAL.labels(provider=provider, kind="completion").inc(
            completion_tokens
        )


def record_compliance_block(category: str) -> None:
    """记录一次出站拦截。"""
    COMPLIANCE_BLOCKED_TOTAL.labels(category=category).inc()


def record_compliance_mask(category: str) -> None:
    """记录一次脱敏（响应出口 / 日志）。"""
    COMPLIANCE_MASKED_TOTAL.labels(category=category).inc()


def record_callback(channel: str, result: str, count: int = 1) -> None:
    """记录一次回调受理。

    Args:
        channel: 渠道值。
        result: `enqueued` / `duplicated` / `rejected` / `parse_error`。
        count: 条数。
    """
    CALLBACK_RECEIVED_TOTAL.labels(channel=channel, result=result).inc(count)


def record_callback_signature_rejected(channel: str, reason: str) -> None:
    """记录一次验签失败（安全事件，需告警）。

    Args:
        channel: 渠道值。
        reason: `SignatureError` 的原因码。
    """
    CALLBACK_SIGNATURE_REJECTED_TOTAL.labels(channel=channel, reason=reason).inc()


def sync_circuit_breakers(snapshot: list[dict]) -> None:
    """把熔断器快照同步到 Gauge（由 `/health/deps` 或后台任务定期调用）。"""
    for item in snapshot:
        name = str(item.get("name", "unknown"))
        CIRCUIT_BREAKER_OPEN.labels(name=name).set(1 if item.get("open") else 0)


def set_app_info(version: str, env: str) -> None:
    """设置应用信息指标（启动时调用一次）。"""
    APP_INFO.labels(version=version, env=env).set(1)


def render_metrics() -> tuple[bytes, str]:
    """渲染 Prometheus 文本格式。

    Returns:
        `(body, content_type)` 二元组。
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = [
    "APP_INFO",
    "CHAT_TURNS_TOTAL",
    "CIRCUIT_BREAKER_OPEN",
    "COMPLIANCE_BLOCKED_TOTAL",
    "COMPLIANCE_MASKED_TOTAL",
    "HANDOFF_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "KB_CHUNKS_TOTAL",
    "LLM_LATENCY",
    "LLM_REQUESTS_TOTAL",
    "LLM_TOKENS_TOTAL",
    "OUTBOUND_SENDS_TOTAL",
    "RAG_DECISION_TOTAL",
    "RAG_RETRIEVAL_DURATION",
    "RATE_LIMIT_REJECTED_TOTAL",
    "REGISTRY",
    "SESSIONS_ACTIVE",
    "TICKET_CREATE_TOTAL",
    "TOOL_CALLS_TOTAL",
    "TOOL_CALL_DURATION",
    "record_compliance_block",
    "record_compliance_mask",
    "record_http",
    "record_llm",
    "record_rag_decision",
    "record_tool_call",
    "render_metrics",
    "set_app_info",
    "sync_circuit_breakers",
]
