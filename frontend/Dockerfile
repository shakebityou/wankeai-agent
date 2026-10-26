# 玩可爱智能客服 Agent —— 前端容器镜像（多阶段构建）
#
# 阶段一：Node 里把 dist/ 构建出来
# 阶段二：Nginx 托管静态资源 + 反代 /bff/ 到后端
#
# 为什么在容器里构建而不是直接拷宿主机的 dist/：
#   交付要求「一键部署」可复现，镜像必须能从源码重建；否则换台机器
#   就变成「我本地有个 dist」的口口相传式部署。
#
# 构建参数：
#   NODE_IMAGE  —— Node 基础镜像（走镜像加速前缀时由 compose 传入）
#   NGINX_IMAGE —— Nginx 基础镜像
#   NPM_REGISTRY—— npm 源，默认 npmmirror（国内可用）
#
# Node 必须 ≥22.5：`mock/vite-plugin-bff-mock.ts` 与 `vite.config.ts` 用到了
# `node:sqlite` 内置模块（本机开发环境是 v24.18.0），node:20 会直接报
# `ERR_UNKNOWN_BUILTIN_MODULE: No such built-in module: node:sqlite`。

ARG NODE_IMAGE=node:24-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ─────────────────────────────────────────────────────────────
# 阶段一：构建
# ─────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS build

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

# 只先拷依赖清单，让 npm ci 这层能被缓存住
COPY package.json package-lock.json .npmrc* ./
RUN npm config set registry "${NPM_REGISTRY}" \
    && npm ci --no-audit --no-fund

# 再拷源码与构建脚本
COPY tsconfig*.json vite.config.ts vitest.config.ts index.html env.d.ts ./
COPY src ./src
COPY scripts ./scripts
COPY mock ./mock

# 用 build:only（只跑 vite build，不做 vue-tsc 类型检查）：
#   类型检查是 CI 的独立门禁（`npm run typecheck`），在镜像构建里重复跑一遍
#   只会拖长构建时间，且 vue-tsc 在容器里对内存要求较高。
RUN npm run build:only

# ─────────────────────────────────────────────────────────────
# 阶段二：运行
# ─────────────────────────────────────────────────────────────
FROM ${NGINX_IMAGE} AS runtime

# 容器版配置（上游是 compose 服务名 app:8000，不是 127.0.0.1）
COPY deploy/nginx.docker.conf /etc/nginx/conf.d/default.conf
COPY deploy/50x.html /usr/share/nginx/html/50x.html

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
