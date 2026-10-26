"""配置管理。

所有可配置项集中于此，通过环境变量注入（12-factor）。**严禁硬编码密钥**：
`app_secret` / `internal_token` / LLM API Key 一律从环境变量或 KMS 读取，
配置文件仅保留占位符（对应任务书 2.3.2 节「app_secret 走 KMS/环境变量，
禁止硬编码、禁止入库」与 S-20 强制要求）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: 仓库根目录（含 `backend/` 与 `商家材料包/` 的那一层），用于定位商家材料包
#:
#: 宿主机布局：``<repo>/backend/app/core/config.py`` → ``parents[3]`` 即仓库根。
#: 容器布局：  ``/app/app/core/config.py`` → ``parents[3]`` 会算成 ``/``，
#: 此时退化为应用根 ``/app``。若不处理，`ensure_local_dirs()` 会去建
#: ``/backend/.local`` 这类无权限目录，容器内 worker 直接起不来。
def _detect_repo_root() -> Path:
    """按实际目录布局识别仓库根（宿主机）或应用根（容器内）。"""
    here = Path(__file__).resolve()
    candidate = here.parents[3]
    if (candidate / "backend").is_dir() or (candidate / "商家材料包").is_dir():
        return candidate
    return here.parents[2]


REPO_ROOT = _detect_repo_root()


class Settings(BaseSettings):
    """全局配置。环境变量前缀 `WK_`，例如 `WK_APP_ID=dsh_agent_001`。"""

    model_config = SettingsConfigDict(
        env_prefix="WK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    app_name: str = "玩可爱智能客服 Agent 后端"
    app_version: str = "1.0.0"
    env: Literal["local", "dev", "test", "prod"] = "local"
    debug: bool = False
    #: 对外固定域名（回调 URL 必须 HTTPS + 已备案，见 2.3.4 节）
    public_base_url: str = "https://agent.wankeai.example.com"
    #: 日志级别（DEBUG/INFO/WARNING/ERROR）
    log_level: str = "INFO"
    #: 是否暴露 OpenAPI 文档（`/docs`、`/openapi.json`）。生产建议关闭
    docs_enabled: bool = True
    #: 是否暴露 Prometheus `/metrics`
    metrics_enabled: bool = True
    #: CORS 允许来源（逗号分隔）。**默认空 = 不开启 CORS**：
    #: 坐席台与运营后台由 Nginx 同源反代 `/bff`，不需要跨域；
    #: 只有本地 `vite dev`（5273 端口）联调才需要显式配置。
    #: `NoDecode`：关闭 pydantic-settings 对复杂类型的强制 JSON 解码，
    #: 交给下方 `_split_csv` 校验器处理，这样 `.env` 里既支持
    #: `a,b,c` 逗号分隔（`.env.example` 的写法），也支持 `["a","b"]` JSON。
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # 商家渠道契约（契约 A）
    # ------------------------------------------------------------------
    merchant_base_url: str = "https://open-test.wankeai.example.com"
    #: 应用标识，由商家分配
    app_id: str = "dsh_agent_001"
    #: 应用级 Token（长期）。**必须由环境变量注入，禁止硬编码**
    internal_token: SecretStr = SecretStr("")
    #: HMAC-SHA256 签名密钥。**必须由 KMS/环境变量注入，禁止入库、禁止提交仓库**
    app_secret: SecretStr = SecretStr("")
    #: BFF 坐席登录态 Token（契约 C）。**生产由统一登录（SSO）签发，本机用固定值联调**
    bff_agent_token: SecretStr = SecretStr("")
    #: BFF 是否强制校验坐席 Token。本地/测试默认关闭（无 Token 也能跑通 mock），生产必须 True
    bff_require_auth: bool = False
    #: X-Timestamp 允许偏差（秒），商家校验窗口
    timestamp_skew_seconds: int = 300
    #: X-Nonce 本地去重缓存 TTL（秒），与商家校验窗口一致
    nonce_ttl_seconds: int = 300
    #: 回调签名校验开关。本地/测试可关闭，生产必须为 True
    verify_callback_signature: bool = True
    #: 商家接口 HTTP 超时（秒）
    merchant_timeout_seconds: float = 5.0
    #: 商家接口连接超时（秒）
    merchant_connect_timeout_seconds: float = 2.0
    #: 测试环境 QPS 上限为生产 1/5（联调压测注意）
    is_merchant_test_env: bool = True

    # ------------------------------------------------------------------
    # 数据源：PostgreSQL 14
    # ------------------------------------------------------------------
    postgres_dsn: str = (
        "postgresql+psycopg2://wkagent:wkagent@localhost:5432/wkagent"
    )
    postgres_pool_size: int = 10
    postgres_max_overflow: int = 20
    postgres_echo: bool = False

    # ------------------------------------------------------------------
    # 关系库后端切换
    # ------------------------------------------------------------------
    #: 关系库后端。`auto` 的解析规则：prod → postgres，其余 → sqlite。
    #:
    #: **为什么需要这个开关**：任务书 3.1 锁定 PostgreSQL 14（选它的理由是
    #: JSONB + 全文检索），但交付机没有 Docker/PG，只有 SQL Server 2022。
    #: 为了「先用本机组件跑通全链路」，允许把关系库换成行为等价的 SQL Server
    #: （`WK_DB_BACKEND=mssql`）。ORM 模型与迁移脚本保持方言无关，切换只改环境
    #: 变量，不改业务代码。偏差需登记在 `docs/偏差记录.md`。
    #:
    #: 取值：`auto` / `postgres` / `mssql` / `sqlite`
    db_backend: Literal["auto", "postgres", "mssql", "sqlite"] = "auto"
    #: 关系库连接串显式覆盖（优先级最高，设置后忽略 `db_backend` 推导）
    db_url: str = ""
    #: SQL Server 连接串（Windows 集成认证时用户名密码留空）
    mssql_dsn: str = (
        "mssql+pyodbc://@localhost/wkagent"
        "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
    )
    #: 连接池回收时间（秒）。云 RDS / VPN 链路会静默掐掉空闲连接，
    #: 不设 `pool_recycle` 时首个请求必然报「连接已关闭」。
    db_pool_recycle_seconds: int = 1800
    #: 连接建立前的探活，防止拿到已被服务端断开的死连接
    db_pool_pre_ping: bool = True

    # ------------------------------------------------------------------
    # 数据源：Redis 7（会话状态 / Nonce / 幂等键 / 限流计数）
    # ------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout: float = 2.0
    #: 会话状态 TTL（秒）。30 分钟，与商家「会话原文 30 天」区分：
    #: **Redis 只存脱敏状态，TTL 30 分钟**
    session_ttl_seconds: int = 1800
    #: 幂等键 TTL（秒）。msg_id 去重 TTL 24h
    idempotency_ttl_seconds: int = 86400
    #: 相同问题缓存 TTL（秒）。BE-16 成本控制，TTL 1h
    answer_cache_ttl_seconds: int = 3600

    # ------------------------------------------------------------------
    # 数据源：Milvus 2.4（向量库，1024 维）
    # ------------------------------------------------------------------
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "wk_knowledge_chunks"
    milvus_user: str = ""
    milvus_password: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # 数据源：Elasticsearch 8（BM25 稀疏召回）
    # ------------------------------------------------------------------
    es_url: str = "http://localhost:9200"
    es_index: str = "wk_knowledge_chunks"
    es_user: str = ""
    es_password: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # 对象存储 OSS（语音原始文件 / 图片 / 导出文件）
    # ------------------------------------------------------------------
    oss_endpoint: str = "https://oss-cn-hangzhou.aliyuncs.com"
    oss_bucket: str = "wankeai-agent"
    oss_access_key_id: SecretStr = SecretStr("")
    oss_access_key_secret: SecretStr = SecretStr("")
    #: 语音原始文件生命周期（天）。合规要求 7 天自动删除
    voice_object_ttl_days: int = 7

    # ------------------------------------------------------------------
    # RAG 参数（**硬性，不可改**，任务书 2.4.2 / BE-05）
    # ------------------------------------------------------------------
    embedding_dim: int = 1024
    rag_top_k_vector: int = 20
    rag_top_k_bm25: int = 20
    rag_top_n_rerank: int = 5
    #: 直接答阈值：top_score >= 0.72 → DIRECT_ANSWER
    rag_threshold_direct: float = 0.72
    #: 兜底阈值：top_score < 0.55 → NO_EVIDENCE，**必须**走兜底或转人工
    rag_threshold_fallback: float = 0.55
    #: RRF 融合常数 k=60
    rag_rrf_k: int = 60
    #: chunk 重叠比例 15%
    chunk_overlap_ratio: float = 0.15
    #: 知识更新后生效延迟上限（分钟），NFR-05
    knowledge_effective_delay_minutes: int = 5
    #: LLM 上下文只送 Top-N（成本控制，只送 Top-3）
    llm_context_top_n: int = 3

    # ------------------------------------------------------------------
    # Embedding（1024 维，国产境内服务）
    # ------------------------------------------------------------------
    embedding_provider: Literal["local", "dashscope", "custom"] = "local"
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "text-embedding-v3"

    # ------------------------------------------------------------------
    # Cross-Encoder Rerank（RAG 六步第 5 步）
    # ------------------------------------------------------------------
    rerank_base_url: str = ""
    rerank_api_key: SecretStr = SecretStr("")
    rerank_model: str = "gte-rerank"

    # ------------------------------------------------------------------
    # LLM 网关（BE-16）
    # ------------------------------------------------------------------
    llm_provider: Literal["local", "dashscope", "zhipu", "custom"] = "local"
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "qwen-plus"
    llm_fallback_provider: Literal["local", "dashscope", "zhipu", "custom"] = "local"
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_model: str = "glm-4"
    llm_timeout_seconds: float = 20.0
    llm_max_output_chars: int = 120  # 单条回复 ≤ 120 字（提示词约束第 5 条）
    #: 允许的 LLM 出口域名白名单。**禁止调用境外模型 API**（网络层强制）
    llm_domain_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "dashscope.aliyuncs.com",
            "open.bigmodel.cn",
            "localhost",
            "127.0.0.1",
        ]
    )
    #: 日用量告警阈值比例（达到 80% 告警）
    llm_daily_budget_alert_ratio: float = 0.8
    #: 日 token 预算（主+备合计）。超预算后网关拒绝真实调用并降级到模板，
    #: 避免一次配置错误（如缓存全失效）把当日额度烧穿。
    llm_daily_token_budget: int = 2_000_000
    #: 单会话累计 token 上限。DoD「单会话轮次上限」的成本维度实现：
    #: 轮次上限（10 轮）管不住「每轮都很长」的情况，两个闸都要有。
    llm_session_token_limit: int = 40_000
    #: 主 Provider 连续失败几次后切备（并在冷却期内直接用备）
    llm_failover_threshold: int = 2
    #: 切备后的冷却秒数（冷却期内主动走备，避免每次先试主再超时）
    llm_failover_cooldown_seconds: int = 60

    # ------------------------------------------------------------------
    # ASR / VLM / OCR（国产境内服务）
    # ------------------------------------------------------------------
    asr_provider: Literal["local", "aliyun", "xfyun"] = "local"
    asr_base_url: str = ""
    asr_api_key: SecretStr = SecretStr("")
    #: ASR 低置信度阈值，< 0.6 → need_clarify
    asr_low_confidence: float = 0.6
    #: 本地 ASR 的「音频指纹 → 文本」固定表（JSON）。仅演示/回归用；
    #: 不配则本地实现如实报告无识别引擎（见 `app/adapters/asr.py` 的说明）。
    asr_fixture_file: str = ""
    #: 单条语音时长上限（秒），超限截断并提示分段
    voice_max_duration_seconds: int = 60

    vlm_provider: Literal["local", "dashscope"] = "local"
    vlm_base_url: str = ""
    vlm_api_key: SecretStr = SecretStr("")
    #: 商品图 Top-3 匹配阈值
    vlm_match_threshold: float = 0.6

    ocr_provider: Literal["local", "aliyun", "paddleocr"] = "local"
    ocr_base_url: str = ""
    ocr_api_key: SecretStr = SecretStr("")
    #: OCR 置信度阈值，< 0.95 → 引导手动输入订单号
    ocr_confidence_threshold: float = 0.95

    # ------------------------------------------------------------------
    # 多模态预处理
    # ------------------------------------------------------------------
    image_max_bytes: int = 10 * 1024 * 1024  # 限 ≤ 10MB
    image_allowed_formats: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["jpg", "jpeg", "png", "webp"]
    )
    ffmpeg_path: str = "ffmpeg"
    #: silk（微信语音）解码器命令模板，`{in}` / `{out}` 为占位符。
    #: ffmpeg 不含 silk demuxer，需外部解码器（如 silk_v3_decoder）；留空则
    #: silk 消息走「格式不支持」兜底话术（见 `app/services/multimodal/audio.py`）。
    silk_decoder_cmd: str = ""
    #: 转码目标：16kHz / 16bit / 单声道 wav
    audio_target_sample_rate: int = 16000
    audio_target_channels: int = 1

    # ------------------------------------------------------------------
    # 转人工（BE-13 / BE-14 / BE-15）
    # ------------------------------------------------------------------
    #: 单会话轮次上限，turn_count > 10 强制引导转人工（成本控制）
    max_turns_per_session: int = 10
    #: 同一问题连续 N 轮兜底 → 未解决转人工（规则③）
    unresolved_threshold: int = 2
    #: 连续 N 轮负面情绪 → 转人工（规则②）
    negative_emotion_streak: int = 3
    #: 单会话订单查询频次上限（次/小时），BE-08
    order_query_hourly_limit: int = 20
    #: 工单接口限流：5 QPS（**全局瓶颈**）
    ticket_qps: int = 5
    #: 工单本地队列退避序列（秒）
    ticket_retry_backoff: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [1, 2, 4, 8, 16]
    )
    #: 在线状态刷新间隔（秒），定时任务每 5 分钟刷新
    agent_online_refresh_seconds: int = 300

    # ------------------------------------------------------------------
    # 限流配置（BE-18，按商家各接口 QPS）
    # ------------------------------------------------------------------
    qps_order_query: int = 20
    qps_logistics_track: int = 20
    qps_inventory_query: int = 50
    qps_product_detail: int = 30
    qps_ticket_create: int = 5
    qps_message_send: int = 30

    # ------------------------------------------------------------------
    # 幂等 / 重试 / 熔断（BE-18）
    # ------------------------------------------------------------------
    #: 熔断器连续失败阈值
    circuit_breaker_failure_threshold: int = 5
    #: 熔断开启时长（秒），之后进入半开探测
    circuit_breaker_open_seconds: int = 30
    #: 通用指数退避基数（秒）
    retry_backoff_base: float = 0.5
    #: 通用最大重试次数
    retry_max_attempts: int = 3

    # ------------------------------------------------------------------
    # 消息发送（BE-02）
    # ------------------------------------------------------------------
    #: 出站通道实现。`mock` = 本机确定性 Mock（商家测试环境不可达，见交接 9.2）；
    #: `http` = 真实 https 调用商家 `/api/v1/message/send`
    message_channel: Literal["mock", "http"] = "mock"
    #: 单条消息字数上限（任务书 BE-02 DoD④：单条 ≤ 120 字）
    outbound_max_chars_per_segment: int = 120
    #: 发送失败重试次数（**不含首次**）。商家说明书附录 C：消息发送失败退避重试 2 次
    outbound_max_retries: int = 2
    #: 首次重试前的退避基数（秒），第 n 次重试为 base * 2^(n-1)；
    #: 429 时优先服从商家 `Retry-After`（且不超过本上限）
    outbound_retry_base_seconds: float = 0.2
    #: 单次退避上限（秒），防止商家给出离谱的 `Retry-After` 把请求线程挂死
    outbound_retry_max_delay_seconds: float = 5.0
    #: 失败重试过程中的总耗时上限（秒）。超时即放弃并落失败表
    outbound_retry_budget_seconds: float = 10.0
    #: 消息发送幂等 TTL（秒）。24h，与回调去重同一口径
    outbound_idempotency_ttl_seconds: int = 86400

    # ------------------------------------------------------------------
    # 业务工具读接口（BE-07 / BE-08 / BE-09）
    # ------------------------------------------------------------------
    #: 读通道实现。`mock` = 本机确定性 Mock（商家测试环境不可达，见交接 9.2）；
    #: `http` = 真实 https 调用商家 1~4 号读接口
    tool_channel: Literal["mock", "http"] = "mock"

    # ------------------------------------------------------------------
    # 合规（BE-17）
    # ------------------------------------------------------------------
    #: 送模型前做脱敏的硬开关。**生产必须为 True**（BE-17 执行位置⑤）
    mask_before_llm: bool = True
    #: 接口返回值脱敏中间件总开关（BE-17 执行位置②）
    compliance_mask_responses: bool = True
    #: 脱敏中间件生效的路径前缀（逗号分隔）。**空 = 全站生效**
    #:
    #: **刻意不含 `/bff/v1`**：BFF 是内部坐席台，工单号（TS+12 位）与买家订单号
    #: 正是坐席检索、回填、对账要用的主键——按 `mask_value` 的口径它们都是 12 位
    #: 数字串，会被打码成 `TS25****9360`，坐席拿着打码号既查不到工单也回填不了
    #: 商家系统（实测就是这样 404 的）。对坐席侧的合规靠三件事保证：角色裁剪
    #: （`require_role`）、字段裁剪（`app.schemas.bff` 里没有的字段不下发）、
    #: 写操作全量审计（`AuditLog`）。**买家侧出站文本**仍由合规闸（BE-17 位置①）
    #: 与 `/internal/v1/message` 的脱敏兜住。
    compliance_mask_prefixes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "/api/v1/callback",
            "/internal/v1/message",
        ]
    )
    #: 接口日志留存（月），合规要求 ≥ 6 个月
    api_log_retention_months: int = 6
    #: 写操作日志留存（月），合规要求 ≥ 1 年
    write_log_retention_months: int = 12
    #: 出站内容是否强制过合规闸
    compliance_gate_enabled: bool = True

    # ------------------------------------------------------------------
    # 数据目录（商家材料包）
    # ------------------------------------------------------------------
    #: 商家材料包根目录
    merchant_package_dir: Path = REPO_ROOT / "商家材料包"
    #: 本地 SQLite 落库路径（无 PostgreSQL 时的等价适配器使用）
    local_db_path: Path = REPO_ROOT / "backend" / ".local" / "wkagent.db"
    #: 本地向量/稀疏索引持久化目录（无 Milvus/ES 时的等价适配器使用）
    local_index_dir: Path = REPO_ROOT / "backend" / ".local" / "index"
    #: 媒体落地目录（无 OSS 时的本地等价实现）
    local_media_dir: Path = REPO_ROOT / "backend" / ".local" / "media"

    @field_validator(
        "llm_domain_allowlist",
        "image_allowed_formats",
        "cors_origins",
        "compliance_mask_prefixes",
        "ticket_retry_backoff",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """解析列表型环境变量：兼容 `a,b,c` 与 `["a","b"]` 两种写法。

        为什么需要：`.env.example` 用逗号分隔（运维习惯），而 pydantic-settings
        对 `list[...]` 字段默认强制 `json.loads`，逗号串会直接抛
        `SettingsError: error parsing value for field ...`。字段上加了
        `NoDecode` 关闭默认解码后，统一由这里接管：
        空串 → 空列表；`[...]` 开头 → 按 JSON 解析；其余 → 按逗号切分。
        """
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    return json.loads(text)
                except ValueError:
                    pass  # 不是合法 JSON 就退回逗号切分
            return [item.strip() for item in text.split(",") if item.strip()]
        return v

    @field_validator(
        "merchant_package_dir",
        "local_db_path",
        "local_index_dir",
        "local_media_dir",
        mode="after",
    )
    @classmethod
    def _resolve_path(cls, v: Path) -> Path:
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()

    @property
    def is_prod(self) -> bool:
        """是否生产环境。"""
        return self.env == "prod"

    @property
    def resolved_db_url(self) -> str:
        """解析后的关系库连接串。

        优先级：`db_url` 显式覆盖 > `db_backend` 推导。

        Returns:
            SQLAlchemy 连接串。
        """
        if self.db_url:
            return self.db_url
        backend = self.db_backend
        if backend == "auto":
            backend = "postgres" if self.is_prod else "sqlite"
        if backend == "postgres":
            return self.postgres_dsn
        if backend == "mssql":
            return self.mssql_dsn
        return f"sqlite+pysqlite:///{self.local_db_path.as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        """当前是否 SQLite（影响连接参数与迁移策略）。"""
        return self.resolved_db_url.startswith("sqlite")

    @property
    def effective_qps(self) -> dict[str, int]:
        """按环境折算后的有效 QPS。测试环境为生产 1/5。"""
        divisor = 5 if (self.is_merchant_test_env and not self.is_prod) else 1
        return {
            "order_query": max(1, self.qps_order_query // divisor),
            "logistics_track": max(1, self.qps_logistics_track // divisor),
            "inventory_query": max(1, self.qps_inventory_query // divisor),
            "product_detail": max(1, self.qps_product_detail // divisor),
            "ticket_create": max(1, self.qps_ticket_create // divisor),
            "message_send": max(1, self.qps_message_send // divisor),
        }

    def ensure_local_dirs(self) -> None:
        """确保本地落盘目录存在（本地适配器使用）。"""
        for path in (
            self.local_db_path.parent,
            self.local_index_dir,
            self.local_media_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。测试中可用 `get_settings.cache_clear()` 重置。"""
    settings = Settings()
    settings.ensure_local_dirs()
    return settings


__all__ = ["REPO_ROOT", "Settings", "get_settings"]
