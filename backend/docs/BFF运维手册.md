# BFF 运维手册（第 31 条交付物）

> 适用版本：玩可爱智能客服 Agent · 后端 `v1.0.0`
> 覆盖范围：`/bff/v1/*`（契约 C，坐席工作台唯一入口）
> 读者：运维、值班工程师、联调负责人
> 配套文件：[`bff-openapi.json`](./bff-openapi.json)（由 `scripts/check_bff.py` 从运行中的应用导出，与实现同源）

---

## 1. 它是什么、边界在哪

BFF（Backend For Frontend）是**前端唯一入口**，不是又一个业务服务：

| 职责 | 说明 |
| --- | --- |
| 协议翻译 | 把内部接口（`/internal/v1/*`，`X-Internal-Token` 鉴权）翻译成坐席工作台要的形状（`/bff/v1/*`，坐席 Token 鉴权） |
| 字段裁剪 | 只下发 `app/schemas/bff.py` 里声明过的字段；DB 里多出来的列（`negative_streak`、`intent_stack` 等）一律不出网 |
| 权限裁剪 | 写配置类接口要求 `supervisor`/`admin`，普通 `agent` 只能读与接管 |
| 聚合 | 会话列表 + 上下文卡片 + 工单 + 指标 + 知识库，一次请求给前端够用的数据 |
| 降级演练 | 通过 `X-Mock-Scenario` 请求头把 8 个降级场景演出来（**仅 local/dev/test 生效**） |

**它不做什么**：不写业务规则、不直连商家接口、不做检索——这些都还在后端服务里，BFF 只是"换个形状再转一手"。所以 BFF 出问题**不影响买家侧对话**，只影响坐席台。

---

## 2. 部署与配置

### 2.1 进程

BFF 与后端是**同一个进程**（模块化单体，`app/main.py` 里 `include_router(build_bff_router())`），没有独立端口、独立容器。部署后端即部署 BFF。

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

验证挂载成功：

```bash
curl -s http://127.0.0.1:8000/openapi.json | python -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for p in d['paths'] if p.startswith('/bff/v1')))"
# 期望输出：41
```

### 2.2 环境变量（`WK_` 前缀）

| 变量 | 默认 | 生产取值 | 说明 |
| --- | --- | --- | --- |
| `WK_ENV` | `local` | `prod` | **`prod` 下 `X-Mock-Scenario` 一律 400**，防止外部用请求头操纵降级 |
| `WK_BFF_REQUIRE_AUTH` | `false` | **`true`** | 关闭时任何人不带 Token 都能进坐席台——**生产必须打开** |
| `WK_BFF_AGENT_TOKEN` | 空 | 由 SSO/网关注入 | 坐席登录态 Token；空值 + `REQUIRE_AUTH=true` 时**所有请求 401** |
| `WK_COMPLIANCE_MASK_RESPONSES` | `true` | `true` | 响应出口脱敏总开关 |
| `WK_COMPLIANCE_MASK_PREFIXES` | `/api/v1/callback,/internal/v1/message` | 同默认 | **刻意不含 `/bff/v1`**，原因见 §6.1 |
| `WK_CORS_ORIGINS` | 本地地址 | 坐席台域名 | 前端跨域白名单 |

> `WK_BFF_AGENT_TOKEN` 是 `SecretStr`，日志与 `/health` 里不会出现明文；轮换时改环境变量重启即可（无状态）。

### 2.3 坐席身份请求头

| 头 | 示例 | 说明 |
| --- | --- | --- |
| `X-Agent-Token` | `eyJhbGciOi...` | 登录态；`REQUIRE_AUTH=true` 时必需 |
| `X-Agent-Id` | `A002` | 坐席工号；缺省 `agent_demo` |
| `X-Agent-Role` | `agent` / `supervisor` / `admin` | 缺省 `agent`（最小权限） |
| `X-Agent-Group` | `G2` | 所属分组，用于「只看本组工单」 |

**换真 SSO 时只改一个函数**：`app/api/bff/deps.py::verify_agent`。接口形状、错误码、前端都不用动。

---

## 3. 健康检查与监控

### 3.1 探针

```bash
curl -s http://127.0.0.1:8000/health          # 进程级
curl -s http://127.0.0.1:8000/bff/v1/metrics/overview -H 'X-Agent-Role: supervisor'
```

`/metrics/overview` 返回 13 项指标：9 项是**已验收的离线实测基线**（意图 85.8%、RAG Recall@5 100%、转人工召回 64.3% 等，`name` 里标了「实测基线」），4 项是**实时算的**（会话量、转人工率、编排 P95、检索 P95）。**不要把基线当实时看**——它不会随流量变化。

### 3.2 关键告警

| 现象 | 阈值建议 | 处置 |
| --- | --- | --- |
| `bff_requests_total{status=~"5.."}` 占比 | > 1% 持续 5 min | 看下游 30001；查商家接口/LLM |
| 编排 P95 | > 2000 ms 持续 10 min | NFR-01 上限；查检索与 LLM 网关 |
| 401 突增 | 5 min 内 > 100 | SSO Token 过期或网关未注入头 |
| 409 突增 | 5 min 内 > 20 | 多坐席抢同一会话；检查前端刷新策略 |
| 429 突增 | 5 min 内 > 50 | 前端轮询过密；查 `Retry-After` 是否被尊重 |

Prometheus 规则文件：`deploy/prometheus/rules/wkagent.yml`。

---

## 4. 8 个降级场景演练（联调必做）

**触发方式统一是请求头 `X-Mock-Scenario: <场景名>`**，只在 `WK_ENV ∈ {local, dev, test}` 生效；未知场景名直接 400（防止拼错后静默走正常路径）。

| # | 场景 | 触发 | 期望响应 | 前端表现 |
| --- | --- | --- | --- | --- |
| ① | 订单查询超时 | `error_50401` | 200，`degraded` 含 `MOCK_ORDER_TIMEOUT`，回复「系统查询中，请稍候～您也可以点右侧「转人工」…」 | 状态条「正在查询订单…」→ 超时提示 + 转人工按钮 |
| ② | 库存接口不可用 | `error_50301` | **503 / 30001** | 展示「库存实时同步中」，**不显示具体数字** |
| ③ | 工单接口不可用 | `error_50301`（同码，打 `/chat/turn` 后转人工链路） | **503 / 30001** | 展示人工客服联系方式，**不让用户空等** |
| ④ | 触发限流 | `error_42901` | **429 / 20001**，响应头 `Retry-After: 5` | 按钮禁用 + 倒计时 5 秒 |
| ⑤ | 签名失败 | `error_40101` | 200，`degraded` 含 `MOCK_SIGNATURE_RETRY` | 前端无感（后端重试）；持续失败转人工 |
| ⑥ | Token 失效 | `error_40102` | 200，正常应答（渠道降级为纯 FAQ） | 提示「渠道受限」 |
| ⑦ | 并发接管冲突 | `error_10005` | **409 / 10005**「该会话已被坐席 A002 接管，请刷新后重试」 | 提示 + 刷新按钮 |
| ⑧ | 无依据拒答 | `no_evidence` | 200，`decision.forced_no_evidence=true`，回复「马上为您转接人工客服～」 | 机器人转人工气泡 |

一键回归全部 8 个场景：

```bash
cd backend
python scripts/check_bff.py        # 期望：ALL PASSED (77 checks)
```

单场景手工验证（以①为例）：

```bash
curl -s -X POST http://127.0.0.1:8000/bff/v1/chat/turn \
  -H 'Content-Type: application/json' \
  -H 'X-Mock-Scenario: error_50401' \
  -d '{"session_id":"drill_1","text":"帮我查订单"}' | python -m json.tool
```

> **演练铁律**：`WK_ENV=prod` 下这个头会被拒（400），所以演练只能在测试环境做；生产要演降级，用下游服务的故障注入，不要指望这个开关。

---

## 5. 日常操作

### 5.1 坐席台主链路自检

```bash
# 1) 跑一轮对话
curl -s -X POST http://127.0.0.1:8000/bff/v1/chat/turn \
  -H 'Content-Type: application/json' -H 'X-Agent-Id: A002' \
  -d '{"session_id":"ops_1","text":"我要转人工"}' | python -m json.tool

# 2) 看会话
curl -s 'http://127.0.0.1:8000/bff/v1/sessions?page=1&page_size=10' | python -m json.tool

# 3) 接管
curl -s -X POST http://127.0.0.1:8000/bff/v1/sessions/ops_1/takeover \
  -H 'Content-Type: application/json' -d '{"agent_id":"A002"}' | python -m json.tool

# 4) 释放
curl -s -X POST http://127.0.0.1:8000/bff/v1/sessions/ops_1/release | python -m json.tool
```

### 5.2 规则配置（需 supervisor/admin）

```bash
# 读
curl -s http://127.0.0.1:8000/bff/v1/config/handoff-rules -H 'X-Agent-Role: supervisor'

# 改（①④ 恒启用，改 enabled=false 会 400）
curl -s -X PUT http://127.0.0.1:8000/bff/v1/config/handoff-rules \
  -H 'Content-Type: application/json' -H 'X-Agent-Role: supervisor' -H 'X-Agent-Id: sup_1' \
  -d '{"rule_id":"②","threshold":2}'
```

每次改规则都会写一条 `audit_logs`（`action=RULE_UPDATE`，含 before/after），可在库里审计。

### 5.3 知识库

```bash
curl -s 'http://127.0.0.1:8000/bff/v1/kb/stats' | python -m json.tool
curl -s -X POST http://127.0.0.1:8000/bff/v1/kb/search \
  -H 'Content-Type: application/json' -d '{"query":"玩偶多高","top_k":5}' | python -m json.tool
```

审核（`POST /kb/chunks/{id}/review`）同样需要 supervisor/admin；状态只接受 `PENDING/APPROVED/REJECTED/DISABLED`。

### 5.4 账号体系（登录 / 改密 / 账号管理 / 操作日志）

坐席台自带一套本地账号体系，**不依赖外部 SSO 也能跑**；将来接 SSO 只需让网关注入
`X-Agent-Token`/`X-Agent-Id`/`X-Agent-Role`，账号接口可以整体下线。

**数据表 3 张**（`roles` / `users` / `auth_sessions`，迁移 `b1a7c4e29f30`）：
口令用 `scrypt`（N=16384/r=8/p=1，每账号 16 字节随机盐）存 `password_hash`，
登录态存 `auth_sessions.token_hash`（**只存 sha256，不存明文 Token**），12 小时过期。
分组不另建表，直接复用 `agent_groups.group_id`。

**首次启动自动播种**：`ensure_seeded()` 在 `users` 表为空时写入 4 个角色 + 7 个演示账号，
统一演示口令 `Wk@2026demo`（**上线前必须逐个重置**，见下方 5.4.3）。

#### 5.4.1 登录与登录态

```bash
# 登录：成功同时下发 HttpOnly Cookie `wk_session` 与 body 里的 token
curl -s -c cookie.txt -X POST http://127.0.0.1:8000/bff/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"wangfang","password":"Wk@2026demo"}' | python -m json.tool

# 我是谁（前端据此决定跳不跳登录页；401/10002 表示未登录）
curl -s -b cookie.txt http://127.0.0.1:8000/bff/v1/auth/me | python -m json.tool

# 登出（未登录也返 200，幂等）
curl -s -b cookie.txt -X POST http://127.0.0.1:8000/bff/v1/auth/logout

# 改自己的密码（改完吊销该账号全部登录态，必须重新登录）
curl -s -b cookie.txt -X POST http://127.0.0.1:8000/bff/v1/auth/password \
  -H 'Content-Type: application/json' \
  -d '{"old_password":"Wk@2026demo","new_password":"N3wPassw0rd"}'
```

> **登录态放 HttpOnly Cookie 而不是 localStorage**：脚本读不到 token，一次 XSS 不至于直接丢会话。
> 代价是前端判断不了"我登录了没"，所以另配 `/auth/me`（与前端 `frontend/mock/services/cookie.ts` 取舍一致）。

**锁定策略**：连续 5 次密码错 → 账号锁 10 分钟，返回 `429` + `20001` + `Retry-After`
（前端据此倒计时禁用按钮）。锁定到期自动解锁，无需人工干预。
错误码口径：缺字段 `10001`；账号不存在/密码错/已停用 `10002`；锁定 `20001`。

#### 5.4.2 账号管理（**仅 `admin`**）

```bash
# 列表（含全量统计 summary，统计不受筛选影响）
curl -s -H 'X-Agent-Role: admin' -H 'X-Agent-Id: U0004' \
  'http://127.0.0.1:8000/bff/v1/users?page=1&page_size=20' | python -m json.tool

# 新建（不传 password 则用默认演示口令，且 must_change=true 强制首登改密）
curl -s -X POST http://127.0.0.1:8000/bff/v1/users \
  -H 'Content-Type: application/json' -H 'X-Agent-Role: admin' -H 'X-Agent-Id: U0004' \
  -d '{"username":"newagent","display_name":"新坐席","role":"AGENT","group_id":"G2"}'

# 编辑（只改传了的字段）
curl -s -X PUT http://127.0.0.1:8000/bff/v1/users/U0005 \
  -H 'Content-Type: application/json' -H 'X-Agent-Role: admin' -H 'X-Agent-Id: U0004' \
  -d '{"status":"DISABLED"}'

# 重置密码（改完 must_change=true + 状态回 ACTIVE + 吊销全部会话）
curl -s -X POST http://127.0.0.1:8000/bff/v1/users/U0005/password \
  -H 'Content-Type: application/json' -H 'X-Agent-Role: admin' -H 'X-Agent-Id: U0004' \
  -d '{"new_password":"Reset@2026"}'
```

**为什么账号接口只给 admin**：账号是权限的源头。让"能改权限的人"比"拥有权限的人"多一层，
否则自我提权只需一步。service 层还有两条硬规则兜底：
**不能改自己的角色**、**不能禁用自己**（都返回 400，提示让另一位管理员操作）。

#### 5.4.3 上线前必须做的三件事

1. **重置全部 7 个演示账号的口令**（`Wk@2026demo` 是公开的）；
2. 关掉不需要的账号（`status=DISABLED` 会同时吊销其全部登录态）；
3. 确认 `WK_BFF_REQUIRE_AUTH=true`，否则登录态形同虚设。

#### 5.4.4 操作日志

```bash
curl -s -H 'X-Agent-Role: admin' 'http://127.0.0.1:8000/bff/v1/audit?limit=100' | python -m json.tool
```

返回 `{id, at, actor_name, action, target, detail}`，`limit` 上限 500。
覆盖 `LOGIN` / `LOGIN_LOCKED` / `LOGOUT` / `CHANGE_PASSWORD` / `USER_CREATE` /
`USER_UPDATE` / `USER_RESET_PASSWORD` / `TAKEOVER` / `RELEASE` / `RULE_UPDATE` / `KB_UPDATE`。
**审计是合规要求，不允许提供删除接口**；要清理只能走 DBA 按保留期归档。

#### 5.4.5 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 登录一直提示"账号或密码不正确，还可尝试 N 次" | 口令错；或 `users` 表为空（种子没跑） | 确认 `ensure_seeded()` 已执行；用 `admin` 账号重置 |
| 登录返回 `429/20001` | 连续 5 次错被锁 | 等 10 分钟自动解锁，或让 admin 重置密码（重置会清锁） |
| 所有 `/bff/v1/*` 返回 `401/10002` | `WK_BFF_REQUIRE_AUTH=true` 但没带 Cookie/Token | 先调 `/auth/login`，或检查反代是否透传 `Cookie` |
| 改密码后前端一直 401 | 改密会吊销**全部**会话 | 属预期，重新登录即可 |
| `/users` 返回 `403/10003` | 当前角色不是 `admin` | 用 admin 账号操作；角色来自 `X-Agent-Role` 或登录态 |

---

## 6. 故障排查

### 6.1 工单号在列表里变成 `TS25****9360`，点详情 404

**症状**：`GET /bff/v1/tickets` 返回的 `ticket_no` 带 `****`，拿它去 `GET /bff/v1/tickets/{no}` 必然 404。

**根因**：`ComplianceMaskMiddleware` 按「12 位数字串」脱敏，工单号 `TS`+12 位正好命中。BFF 是内部坐席台，工单号是坐席检索与回填的主键，**被打码就没法工作**。

**处置**：确认 `WK_COMPLIANCE_MASK_PREFIXES` **不含** `/bff/v1`。对坐席侧的合规靠角色裁剪 + 字段裁剪 + 全量审计保证，不靠响应打码（买家侧出站文本仍由合规闸与 `/internal/v1/message` 脱敏兜住）。

### 6.2 所有 `/bff/v1/*` 返回 401/10002

按顺序查：`WK_BFF_REQUIRE_AUTH` 是否被打开而前端没带 `X-Agent-Token`；网关是否把 Token 头剥掉了；`WK_BFF_AGENT_TOKEN` 是否为空（空值 + 强鉴权 = 全 401）。

### 6.3 配置接口返回 403/10003

`X-Agent-Role` 缺省是 `agent`，改配置需要 `supervisor` 或 `admin`。**这是设计行为**，不是 bug；前端要把无权限的按钮置灰。

### 6.4 两个坐席同时接管，一个 409

正常行为（10005）。前端收到后应提示「已被 XXX 接管」并刷新列表，不要自动重试——重试会继续撞。

### 6.5 SSE 连上但没内容

`/bff/v1/chat/stream` 是 `text/event-stream`，注意三件事：反向代理要关缓冲（`X-Accel-Buffering: no` 已由服务端下发）；`nginx` 需 `proxy_buffering off`；浏览器 `EventSource` 不能自定义请求头，坐席 Token 要走 Cookie 或查询参数（网关侧处理）。

### 6.6 指标全是「实测基线」不动

设计如此。9 项离线指标没有逐点数据源，返回的是验收时的实测值；只有会话量/转人工率/两个 P95 是实时算的。趋势接口只对 `orchestrator_p95`、`retrieval_p95` 返回真实点，其余返回空点集（前端画「暂无趋势」而不是假曲线）。

### 6.7 晚上跑自检，工单相关的检查全红

**不是故障，是时间**。坐席分组在线时段是 `09:00–22:00`，`is_online` 读真实时钟；22:00 之后
`HandoffService.handoff()` 一律判离线 → 跳过商家同步，于是 `check_handoff.py` 的
「建单/降级」两节会红，单测里 `SYNCED` 事件、商家工单号、`degraded`、`queue_position` 也会假失败。

判断方法：看失败用例的 `text` 是不是**离线话术**（「已记录您的需求，工单号 …… 人工客服上线后会按工单顺序联系您。」）——是就说明是时段问题，不是代码坏了。

处置：自检脚本已在 `check_ticket_creation`/`check_degrade` 开头调 `_pin_online()` 把服务层
`is_online` 钉死为在线；单测有两个 autouse 夹具做同样的事。**若你自己新写的脚本或用例在夜间红了，
照这个模式钉住在线判定，不要去改 `app.services.handoff.online.is_online` 本身**——真实时段逻辑靠它测。

---

## 7. 上线与回滚

**上线**：BFF 与后端同进程，按后端发布流程走。**账号体系补齐后新增 3 个迁移**
（`b1a7c4e29f30` 建 `roles`/`users`/`auth_sessions`；`c7d2e5a91b04` 给 `channels` 加 5 列运营字段；
`d4f8b2c6e103` 给 `kb_feedback` 加 7 列并建 `ix_kb_feedback_message`），上线前必须先
`alembic upgrade head`。回滚顺序：先回镜像，再 `alembic downgrade` 到对应版本（账号表可整体丢弃，
不影响业务数据；`channels`/`kb_feedback` 新增列都有默认值，回退不丢老数据）。

**灰度**：`WK_BFF_REQUIRE_AUTH=false` → 观察 401/5xx → 打开强鉴权 → 全量。**不要在打开强鉴权前让真实坐席接入**。

**回滚检查单**：
1. 镜像回退到上一版；
2. 确认 `audit_logs` 里没有半截的 `RULE_UPDATE`（规则写入是单事务）；
3. 确认 `tickets` 表无重复 `session_id`（唯一约束兜底，回滚不会产生脏单）。

---

## 8. 安全清单（上线前逐项打勾）

- [ ] `WK_ENV=prod`
- [ ] `WK_BFF_REQUIRE_AUTH=true`，且 `WK_BFF_AGENT_TOKEN` 由网关注入（不落代码库）
- [ ] `WK_CORS_ORIGINS` 只含坐席台域名
- [ ] `verify_agent` 已换成真 SSO 校验（当前是固定 Token 的占位实现）
- [ ] 写配置接口的角色要求未被放宽
- [ ] `WK_COMPLIANCE_MASK_PREFIXES` 不含 `/bff/v1`，且含 `/api/v1/callback`、`/internal/v1/message`
- [ ] 审计日志留存 ≥ 12 个月（`WK_WRITE_LOG_RETENTION_MONTHS`）
- [ ] **7 个演示账号（`Wk@2026demo`）已全部重置或禁用**，见 §5.4.3
- [ ] `alembic upgrade head` 已执行（账号表 + 渠道运营列 + 纠错列，共 3 个迁移）
- [ ] `/openapi.json` 在生产是否暴露已确认（如不需要，网关层屏蔽）

---

## 9. 附：BFF 端点一览（41 个）

| 分组 | 端点 |
| --- | --- |
| 账号 | `POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、`POST /auth/password` |
| 账号 | `GET /users`、`POST /users`、`PUT /users/{user_id}`、`POST /users/{user_id}/password`、`GET /audit` |
| 字典 | `GET /meta/groups` |
| 会话 | `GET /sessions`、`GET /sessions/summary`、`GET /sessions/{id}`、`GET /sessions/{id}/context`、`POST /sessions/{id}/takeover`、`POST /sessions/{id}/release`、`GET /sessions/{id}/replay` |
| 对话 | `POST /chat/turn`、`GET /chat/stream`（SSE） |
| 知识库 | `GET/POST /kb/chunks`、`GET/PUT/DELETE /kb/chunks/{id}`、`GET /kb/chunks/{id}/history`、`POST /kb/chunks/{id}/review`、`POST /kb/chunks/{id}/rollback`、`POST /kb/search`、`GET/POST /kb/feedback`、`PUT /kb/feedback/{feedback_id}`、`GET /kb/stats`、`POST /kb/import`、`POST /kb/import/preview`、`GET /kb/import/{import_id}/progress` |
| 指标 | `GET /metrics/overview`、`GET /metrics/trend`、`GET /metrics/distribution` |
| 配置 | `GET/PUT /config/handoff-rules`、`PUT /config/handoff-rules/test`、`GET/PUT /config/agent-groups`、`GET /config/agent-groups/conflicts`、`GET/PUT /config/channels`、`PUT /config/channels/{channel}` |
| 工单 | `GET /tickets`、`GET /tickets/{ticket_no}` |

> 共 **41 条**（其中 `GET /kb/chunks/{id}`、`GET /kb/stats`、`PUT /config/channels` 是后端增强、
> 前端 mock 未演，其余 46 条 mock 路由后端**全部实现**，对齐率 100%，由
> `scripts/check_bff.py` 的「契约对拍」一节逐条核对）。

统一响应：`{"code":0,"message":"ok","request_id":"trace_...","data":{...}}`；错误码见 `app/core/errors.py::BffErrorCode`（10001 参数 / 10002 未登录 / 10003 无权限 / 10004 不存在 / 10005 冲突 / 20001 限流 / 30001 下游不可用）。
