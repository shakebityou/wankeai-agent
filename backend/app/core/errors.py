"""错误码体系。

两套错误码严格区隔（任务书 2.5.6 节）：

* **商家错误码**（契约 A）：来自商家《接口对接说明书》，项目组**只做解析与降级**，
  不重新定义。取值范围 `40001`~`50401`，见 `MerchantErrorCode`。
* **BFF 错误码**（契约 C）：项目组自用，取值范围 `10001`~`30001`，见 `BffErrorCode`。

设计要点：商家错误码附带**处理策略**（是否重试、重试几次、用户侧话术、降级动作），
这些策略是 BE-07/BE-18 的实现依据，集中在此处避免散落各调用点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class MerchantErrorCode(IntEnum):
    """商家接口错误码（契约 A，2.3.3 节）。项目组不重定义，只解析。"""

    SUCCESS = 0
    PARAM_MISSING = 40001  # 参数缺失/格式错误
    PARAM_INVALID = 40002  # 参数值非法（如 order_no 非 12 位）
    SIGN_FAILED = 40101  # 签名失败 / Nonce 重放
    TOKEN_INVALID = 40102  # Token 无效或吊销
    TIMESTAMP_SKEW = 40103  # 时间戳偏差 > 300s
    NO_PERMISSION = 40301  # 无数据权限（跨店铺/越权）
    NOT_FOUND = 40401  # 资源不存在
    IDEMPOTENT_CONFLICT = 40901  # 幂等冲突
    RATE_LIMITED = 42901  # 触发限流
    INTERNAL_ERROR = 50001  # 服务内部错误
    SERVICE_UNAVAILABLE = 50301  # 服务暂不可用
    UPSTREAM_TIMEOUT = 50401  # 上游超时（ERP/快递商）


@dataclass(frozen=True)
class ErrorPolicy:
    """商家错误码的处理策略（BE-07 逐条实现依据）。

    Attributes:
        retryable: 是否重试。
        max_retries: 最大重试次数（不含首次请求）。
        retry_mode: 重试方式，`none` / `resign`（重算签名换 Nonce）/
            `retry_after`（按 Retry-After 退避）/ `exponential`（指数退避）。
        user_message: 用户侧话术，None 表示无感（不改变用户可见回复）。
        degrade: 降级动作标识，供编排层与降级表（2.3.5）映射。
        alert: 是否触发告警，及告警级别（P0/P1/安全）。
    """

    retryable: bool
    max_retries: int
    retry_mode: str
    user_message: str | None
    degrade: str | None = None
    alert: str | None = None


#: 商家错误码 → 处理策略。严格对齐任务书 2.3.3 表格。
MERCHANT_ERROR_POLICIES: dict[MerchantErrorCode, ErrorPolicy] = {
    MerchantErrorCode.SUCCESS: ErrorPolicy(False, 0, "none", None),
    MerchantErrorCode.PARAM_MISSING: ErrorPolicy(
        False,
        0,
        "none",
        "我这边没查到这个订单，您方便核对一下订单号吗？",
        alert="P1",
    ),
    MerchantErrorCode.PARAM_INVALID: ErrorPolicy(
        False, 0, "none", "引导用户确认订单号"
    ),
    MerchantErrorCode.SIGN_FAILED: ErrorPolicy(
        True, 1, "resign", "若仍失败 → 转人工"
    ),
    MerchantErrorCode.TOKEN_INVALID: ErrorPolicy(
        False,
        0,
        "none",
        "该渠道暂不支持订单查询",
        degrade="FAQ_ONLY",
        alert="P0",  # 立即告警商家 IT
    ),
    MerchantErrorCode.TIMESTAMP_SKEW: ErrorPolicy(
        True, 1, "resign", "转人工", degrade="NTP_RESYNC"
    ),
    MerchantErrorCode.NO_PERMISSION: ErrorPolicy(
        False,
        0,
        "none",
        "这个订单不在当前账号下，请用下单账号咨询哦～",
        alert="安全",
    ),
    MerchantErrorCode.NOT_FOUND: ErrorPolicy(
        False, 0, "none", "没查到这笔订单，您核对下订单号？"
    ),
    MerchantErrorCode.IDEMPOTENT_CONFLICT: ErrorPolicy(
        False, 0, "none", None, degrade="REUSE_REQUEST_ID"
    ),
    MerchantErrorCode.RATE_LIMITED: ErrorPolicy(
        True, 3, "retry_after", "正在为您查询，请稍等一下～"
    ),
    MerchantErrorCode.INTERNAL_ERROR: ErrorPolicy(
        True, 1, "exponential", "转人工", alert="P0"
    ),
    MerchantErrorCode.SERVICE_UNAVAILABLE: ErrorPolicy(
        True, 3, "exponential", None, degrade="PER_INTERFACE"  # 按各接口降级策略
    ),
    MerchantErrorCode.UPSTREAM_TIMEOUT: ErrorPolicy(
        True, 3, "exponential", "系统查询中，请稍候", degrade="PER_INTERFACE"
    ),
}


class BffErrorCode(IntEnum):
    """BFF 错误码（契约 C，项目组自用，2.5.6 节）。与商家错误码区隔。"""

    SUCCESS = 0
    PARAM_INVALID = 10001  # 参数校验失败 → 表单标红提示
    UNAUTHENTICATED = 10002  # 坐席未登录/登录态过期 → 跳转登录页
    FORBIDDEN = 10003  # 无权限（如非本组工单）
    NOT_FOUND = 10004  # 资源不存在
    CONFLICT = 10005  # 并发冲突（如两人同时接管）
    TOO_MANY_REQUESTS = 20001  # 前端请求过于频繁
    DOWNSTREAM_UNAVAILABLE = 30001  # 下游（商家接口/LLM）不可用


#: BFF 错误码 → HTTP 状态码
BFF_HTTP_STATUS: dict[BffErrorCode, int] = {
    BffErrorCode.SUCCESS: 200,
    BffErrorCode.PARAM_INVALID: 400,
    BffErrorCode.UNAUTHENTICATED: 401,
    BffErrorCode.FORBIDDEN: 403,
    BffErrorCode.NOT_FOUND: 404,
    BffErrorCode.CONFLICT: 409,
    BffErrorCode.TOO_MANY_REQUESTS: 429,
    BffErrorCode.DOWNSTREAM_UNAVAILABLE: 503,
}


class AppError(Exception):
    """项目组内部统一异常基类。

    Attributes:
        code: BFF 错误码（对外）。
        message: 面向调用方的错误描述（**不得包含敏感信息**）。
        http_status: HTTP 状态码，默认由 `code` 推导。
        detail: 内部诊断信息，仅落日志，不下发给前端。
    """

    def __init__(
        self,
        code: BffErrorCode,
        message: str,
        *,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status or BFF_HTTP_STATUS.get(code, 400)
        self.detail = detail


class ValidationError(AppError):
    """参数校验失败。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(BffErrorCode.PARAM_INVALID, message, detail=detail)


class NotFoundError(AppError):
    """资源不存在。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(BffErrorCode.NOT_FOUND, message, detail=detail)


class ConflictError(AppError):
    """并发冲突（如会话已被他人接管）。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(BffErrorCode.CONFLICT, message, detail=detail)


class ForbiddenError(AppError):
    """无权限（如越权查询订单、非本组工单）。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(BffErrorCode.FORBIDDEN, message, detail=detail)


class DownstreamUnavailableError(AppError):
    """下游（商家接口 / LLM / 向量库）不可用，已按降级策略处理。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(
            BffErrorCode.DOWNSTREAM_UNAVAILABLE, message, detail=detail
        )


class TooManyRequestsError(AppError):
    """请求过于频繁（限流）。带 `retry_after` 秒数，响应会加 `Retry-After` 头。"""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(BffErrorCode.TOO_MANY_REQUESTS, message, detail=detail)
        self.retry_after = retry_after


@dataclass
class MerchantApiError(Exception):
    """商家接口返回的非 0 错误码，包装为异常供工具层统一处理。

    Attributes:
        code: 商家错误码。
        message: 商家返回的 message 原文（**落日志前必须脱敏**）。
        request_id: 商家返回的 request_id，排障用。
        http_status: 商家返回的 HTTP 状态码。
        retry_after: 429 场景下的 Retry-After 秒数。
        policy: 由 `MERCHANT_ERROR_POLICIES` 查得的处理策略。
    """

    code: MerchantErrorCode
    message: str = ""
    request_id: str | None = None
    http_status: int | None = None
    retry_after: int | None = None
    policy: ErrorPolicy = field(init=False)

    def __post_init__(self) -> None:
        self.policy = MERCHANT_ERROR_POLICIES.get(
            self.code,
            ErrorPolicy(False, 0, "none", "系统查询中，请稍候", degrade="PER_INTERFACE"),
        )
        Exception.__init__(self, f"merchant error {int(self.code)}: {self.message}")

    @property
    def is_retryable(self) -> bool:
        """是否应重试。"""
        return self.policy.retryable


__all__ = [
    "AppError",
    "BFF_HTTP_STATUS",
    "BffErrorCode",
    "ConflictError",
    "DownstreamUnavailableError",
    "ErrorPolicy",
    "ForbiddenError",
    "MERCHANT_ERROR_POLICIES",
    "MerchantApiError",
    "MerchantErrorCode",
    "NotFoundError",
    "TooManyRequestsError",
    "ValidationError",
]
