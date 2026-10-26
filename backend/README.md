# backend

玩可爱智能客服 Agent · 后端（模块化单体）。

任务书口径：Python 3.11 + FastAPI ≥0.110 + PostgreSQL 14 + Redis 7 +
Milvus 2.4 + Elasticsearch 8 + 阿里云 OSS + Celery；不引入重量级 Agent 框架
（自研状态机 + LangGraph 风格 DAG，保证确定性可控可审计）。

## 三条硬约束（代码层面的落点）

| 约束 | 内容 | 落点 |
| --- | --- | --- |
| HC-1 | 不生成、不发送任何外链或跳转卡片 | `app/compliance/outbound_gate.py` 出站扫描 |
| HC-2 | 数据不出境、不用于训练、个人信息不落库、日志零敏感字段 | `app/core/logging.py` 脱敏管道 + `app/middleware/trace.py` 不记请求体 |
| HC-3 | 会变的数字永远走接口，库存/价格不进向量库 | `app/adapters/vector_store.py` 的 `FORBIDDEN_FIELDS` |

## 目录结构

```
app/
├── main.py               # FastAPI 应用工厂（uvicorn --factory / gunicorn app.main:app）
├── api/                  # 接入层：deps / errors / v1 路由
│   └── v1/health.py      # /health, /health/live, /health/ready, /health/deps, /metrics
├── core/                 # 横切基础：config / enums / errors / logging / metrics /
│                         #           ratelimit / response / retry / trace / idempotency
├── adapters/             # 端口与实现：cache / embedding / llm_provider / media_store /
│                         #               reranker / sparse_index / vector_store
├── compliance/           # HC-1 出站合规闸（outbound_gate）
├── db/                   # SQLAlchemy：engine / types / models
├── middleware/           # trace / access-log / metrics（纯 ASGI，不缓冲 SSE）
└── schemas/              # 契约模型：umo / chat / rag / knowledge / tools /
                          #            handoff / bff / common
```

## 本地运行（无需 PG/Redis/Milvus/ES）

```powershell
# 依赖
pip install -r requirements.txt

# 起服务（WK_ENV=local 时全部用本地等价适配器）
$env:PYTHONPATH = "$PWD"; $env:WK_ENV = "local"
uvicorn app.main:app --reload --port 8000
```

- 文档：<http://127.0.0.1:8000/docs>
- 探活：<http://127.0.0.1:8000/health>（恒 200，compose healthcheck 依赖它）
- 指标：<http://127.0.0.1:8000/metrics>
- `WK_ENV=local` 下的实现：`InMemoryCache` / `NumpyVectorStore` /
  `Bm25SparseIndex` / `LocalHashEmbedder` / `LexicalReranker` / `LocalMediaStore`
  （实现类名可从 `/health/deps` 的 `impl` 字段核对）。

## 测试

```powershell
$env:PYTHONPATH = "$PWD"; $env:WK_ENV = "local"
python -m pytest                    # 全量（含覆盖率门槛 70%）
python -m pytest tests/test_contracts.py -v   # 只跑契约测试
```

覆盖率门槛见 `pyproject.toml` 的 `--cov-fail-under=70`；
关键路径（BE-04/05/06 等服务层）在 CI 单独用 `--cov-fail-under=85` 跑。

## docker-compose 全套编排

```powershell
docker compose --profile core up -d                       # app + PG + Redis + Milvus + ES
docker compose --profile core --profile obs up -d         # 再加 Prometheus + Grafana + Loki
```

宿主机端口刻意错开本机已有服务：Redis `6380`、PG `5433`、Milvus `19531`、
ES `9201`、App `8000`、Prometheus `9090`、Grafana `3000`、Loki `3100`。

**镜像源**：`.env` 里的 `REGISTRY_MIRROR`。国内直连 `docker.io` 常受限；
注意宿主机开系统代理时部分镜像站会因「非大陆 IP」返回 403。

## 生产启动

Dockerfile 的 CMD（Gunicorn + UvicornWorker，worker 数 = 2×CPU+1）：

```
gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker \
    --workers $WEB_CONCURRENCY --bind 0.0.0.0:8000
```

## 配置

`.env.example` 是唯一口径（247 行，全部 `WK_` 前缀）。**禁止把 `.env` 提交仓库**
（已在 `.gitignore` 排除）；`WK_APP_SECRET` / `WK_INTERNAL_TOKEN` 由 KMS 或
环境变量注入，禁止硬编码、禁止入库。

## 数据库迁移

```powershell
alembic upgrade head      # 应用迁移
alembic revision -m "xxx" # 生成新迁移
```

`create_all()` 只用于 SQLite 本地开发与单测；生产一律走 Alembic。
