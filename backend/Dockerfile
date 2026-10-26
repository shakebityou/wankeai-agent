# ==============================================================================
# 玩可爱智能客服 Agent · 后端镜像（模块化单体）
# ------------------------------------------------------------------------------
# 运行基线 Python 3.11（任务书 3.1 锁定）。
# 进程模型：Gunicorn + UvicornWorker，worker = 2 × CPU + 1（任务书 3.1）。
# ==============================================================================
# 基础镜像可覆盖：国内构建传 --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

WORKDIR /app

# ffmpeg：语音转码（BE-10，amr/silk/mp3 → 16k 单声道 wav）
# curl：健康检查；libpq5：psycopg2 运行时依赖
#
# APT_MIRROR：Debian 源镜像。官方 deb.debian.org 在部分网络环境不可达，
# 传 --build-arg APT_MIRROR=mirrors.aliyun.com 即可切换（bookworm 用 deb822
# 的 debian.sources，buster 及更早用 sources.list，两种都覆盖）。
ARG APT_MIRROR=""
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$f" ] && sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" "$f" || true; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg curl libpq5 ca-certificates tzdata; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------------
# 依赖层（单独一层，改代码不重装依赖）
# ------------------------------------------------------------------------------
# PIP_INDEX_URL：PyPI 源。默认官方源；国内构建可传清华/阿里镜像加速。
ARG PIP_INDEX_URL="https://pypi.org/simple"
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# ------------------------------------------------------------------------------
# 应用代码
# ------------------------------------------------------------------------------
COPY . .

# 非 root 运行（最小权限原则，商家《服务器与部署环境说明》4.1）
RUN useradd --create-home --shell /bin/bash wkagent \
    && mkdir -p /app/.local \
    && chown -R wkagent:wkagent /app
USER wkagent

EXPOSE 8000

# 健康检查（compose 里另有 healthcheck，此处兜底）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# worker 数：2 × CPU + 1，由 docker-compose 的 deploy.resources 或环境变量给出
# 默认 4（2 vCPU 场景），生产按实际 CPU 调整。
ENV WEB_CONCURRENCY=4 \
    GUNICORN_TIMEOUT=60 \
    GUNICORN_GRACEFUL_TIMEOUT=30 \
    GUNICORN_KEEPALIVE=5

CMD ["sh", "-c", "gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers ${WEB_CONCURRENCY} \
    --bind 0.0.0.0:8000 \
    --timeout ${GUNICORN_TIMEOUT} \
    --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT} \
    --keep-alive ${GUNICORN_KEEPALIVE} \
    --access-logfile - \
    --error-logfile -"]
