/**
 * BFF Mock 路由总表 —— 严格对齐任务书 2.5「BFF 接口总览」。
 *
 * 设计要点：
 * 1. **有状态**：接管/释放、审核、规则保存、导入等写操作会真实修改 store，
 *    演示时能立刻看到效果（这是 Mock 库做不到的部分）。
 * 2. **统一包装**：所有响应都走 2.5.5 的 {code,message,request_id,data}。
 * 3. **错误码可注入**：任意接口加 ?__fail=10005 即可验证前端拦截器分支。
 * 4. **SSE 逐帧推送**：/v1/chat/stream 按 2.5.4 事件表真实推流，
 *    用于验证打字机效果与断线重连。
 */
import type { MockRequest, MockResult } from './http'
import { fail, ok, paginate, readQueryInt, readQueryList, writeJson, delay } from './http'
import { parseMultipart, readUploadedText, parseCsv, detectKbType, decodeText } from './upload'
import { store, nowIso, minutesAgo, type ImportTaskState } from './seed/store'
import { findSession, createLiveSession, type MockSession } from './seed/sessions'
import { splitIntoChunks, CHUNK_OVERLAP, type MockChunk, type MockVersion } from './seed/kb'
import {
  buildMetrics,
  sortMetricsForDashboard,
  buildTrend,
  buildChannelDistribution,
  buildIntentDistribution,
  buildHandoffFunnel,
} from './seed/metrics'
import { detectConflicts } from './seed/agent-groups'
import { handleAuthRoutes } from './handlers-auth'
import type {
  AgentGroup,
  Channel,
  ChatMessage,
  ChatTurnRequest,
  FeedbackItem,
  HandoffRule,
  HandoffRuleTestResult,
  ImportCommitRequest,
  ImportPreviewChunk,
  ImportPreviewResponse,
  ImportProgress,
  KbChunkPayload,
  KbReviewPayload,
  KbSearchHit,
  KbSearchRequest,
  MetricId,
  ReviewStatus,
  Sender,
  SessionContext,
} from '../src/types/contract'
import { CHUNK_LENGTH_RANGE, CONTEXT_REQUIRED_FIELDS, METRIC_IDS } from '../src/types/contract'

/**
 * 当前操作人。
 *
 * 真实环境由 BFF 从会话态取；这里从中间件解析出的登录 Cookie 取。
 * 未登录时给一个匿名占位，保证只读接口不会被 500 打断 ——
 * 需要鉴权的路由在 handlers-auth.ts 里逐个用 auth 判定拦下。
 */
function currentActor(req: MockRequest): { agent_id: string; agent_name: string; role: string } {
  const auth = req.auth
  if (!auth) return { agent_id: 'anonymous', agent_name: '未登录用户', role: 'GUEST' }
  return { agent_id: auth.user_id, agent_name: auth.display_name, role: auth.role }
}

export async function handleBffRequest(req: MockRequest): Promise<boolean> {
  // 调试注入：?__fail=10005 强制返回指定错误码；?__delay=1500 模拟慢接口
  const injectDelay = readQueryInt(req.query, '__delay', 0)
  if (injectDelay > 0) await delay(injectDelay)

  const forced = req.query.get('__fail')
  if (forced) {
    const forcedResult = forcedError(Number(forced))
    if (forcedResult) {
      writeJson(req.res, forcedResult)
      return true
    }
  }

  const handled = await route(req)
  if (handled === null) return false
  if (handled.raw) return true
  writeJson(req.res, handled)
  return true
}

/** 返回 null 表示该路由不归 Mock 管（交给 Vite 后续中间件） */
async function route(req: MockRequest): Promise<MockResult | null> {
  const { method, path } = req
  const data = store()
  const actorName = currentActor(req).agent_name

  // ── 账号（各角色登录 / 用户管理）────────────────────────────

  const authResult = await handleAuthRoutes(req)
  if (authResult) return authResult

  // ── 会话（FE-01 / FE-02 / FE-03 / FE-10）────────────────────

  if (method === 'GET' && path === '/v1/sessions') {
    const keyword = (req.query.get('keyword') ?? '').trim().toLowerCase()
    const channel = req.query.get('channel') ?? ''
    const status = req.query.get('status') ?? 'ALL'
    const groupId = req.query.get('group_id') ?? ''
    const takenOverRaw = req.query.get('taken_over')
    const startTime = req.query.get('start_time')
    const endTime = req.query.get('end_time')

    let items = data.sessions.map((session) => session.summary)

    if (channel) items = items.filter((item) => item.channel === channel)
    if (groupId) items = items.filter((item) => item.group_id === groupId)
    if (status === 'BOT') items = items.filter((item) => item.owner === 'BOT')
    if (status === 'AGENT') items = items.filter((item) => item.owner === 'AGENT')
    // WAITING：机器人归属且最后一条是买家消息 —— 即真正在等人工介入的会话
    if (status === 'WAITING') {
      items = items.filter((item) => item.owner === 'BOT' && item.last_sender === 'USER')
    }
    if (takenOverRaw === 'true') items = items.filter((item) => item.taken_over)
    if (takenOverRaw === 'false') items = items.filter((item) => !item.taken_over)
    if (startTime) {
      items = items.filter((item) => Date.parse(item.last_message_at) >= Date.parse(startTime))
    }
    if (endTime) {
      items = items.filter((item) => Date.parse(item.last_message_at) <= Date.parse(endTime))
    }
    if (keyword) {
      items = items.filter(
        (item) =>
          item.session_id.toLowerCase().includes(keyword) ||
          item.buyer_display.toLowerCase().includes(keyword) ||
          item.last_message_preview.toLowerCase().includes(keyword) ||
          (item.ticket_no ?? '').toLowerCase().includes(keyword),
      )
    }

    return ok(paginate(items, req.query))
  }

  if (method === 'GET' && path === '/v1/sessions/summary') {
    // 侧栏角标用：各状态会话数
    const all = data.sessions.map((item) => item.summary)
    return ok({
      total: all.length,
      waiting: all.filter((item) => item.owner === 'BOT' && item.last_sender === 'USER').length,
      taken_over: all.filter((item) => item.taken_over).length,
      handoff: all.filter((item) => item.ticket_no).length,
    })
  }

  const sessionDetailMatch = /^\/v1\/sessions\/([^/]+)$/.exec(path)
  if (method === 'GET' && sessionDetailMatch) {
    const session = findSession(data.sessions, decodeURIComponent(sessionDetailMatch[1]!))
    if (!session) return fail(10004, '会话不存在或已归档', 404)
    return ok({ session: session.summary, messages: session.messages, buyer: session.buyer })
  }

  const contextMatch = /^\/v1\/sessions\/([^/]+)\/context$/.exec(path)
  if (method === 'GET' && contextMatch) {
    const session = findSession(data.sessions, decodeURIComponent(contextMatch[1]!))
    if (!session) return fail(10004, '会话不存在或已归档', 404)
    // 七要素缺失的样本原样返回 —— 前端必须自己守卫并展示明确错误（FE-02 DoD）
    return ok(session.context)
  }

  const takeoverMatch = /^\/v1\/sessions\/([^/]+)\/takeover$/.exec(path)
  if (method === 'POST' && takeoverMatch) {
    const sessionId = decodeURIComponent(takeoverMatch[1]!)
    const session = findSession(data.sessions, sessionId)
    if (!session) return fail(10004, '会话不存在或已归档', 404)

    const body = (req.body ?? {}) as { agent_id?: string; ticket_no?: string }
    const actor = currentActor(req)
    // 坐席 ID 来自登录态，不接受前端自报 —— 否则谁都能冒充别人接管会话
    const agentId = actor.agent_id
    const agentName = actor.agent_name

    // 并发冲突：已被其他坐席接管时返回 10005（FE-03 DoD：两个坐席同时点只能一个成功）
    if (session.summary.taken_over && session.summary.agent_id && session.summary.agent_id !== agentId) {
      return fail(10005, `该会话已被 ${session.summary.agent_name ?? '其他坐席'} 接管`, 409)
    }

    const takenOverAt = nowIso()
    session.summary.owner = 'AGENT'
    session.summary.taken_over = true
    session.summary.bot_muted = true
    session.summary.agent_id = agentId
    session.summary.agent_name = agentName
    if (body.ticket_no) session.summary.ticket_no = body.ticket_no

    // 接管后追加一条系统提示，坐席台能立刻看到状态变化
    session.messages.push({
      message_id: `${sessionId}-M${session.messages.length + 1}`,
      session_id: sessionId,
      sender: 'AGENT',
      msg_type: 'TEXT',
      text: `坐席 ${agentName} 已接管该会话，机器人自动回复已暂停。`,
      text_source: 'ORIGINAL',
      image_ref: null,
      image_alt: null,
      created_at: takenOverAt,
      trace_id: null,
      chunk_ids: [],
      decision: null,
      handoff_reason: null,
      latency_ms: null,
      feedback_marked: false,
    })

    return ok({
      session_id: sessionId,
      owner: 'AGENT',
      agent_id: agentId,
      agent_name: agentName,
      taken_over_at: takenOverAt,
      bot_muted: true,
    })
  }

  const releaseMatch = /^\/v1\/sessions\/([^/]+)\/release$/.exec(path)
  if (method === 'POST' && releaseMatch) {
    const sessionId = decodeURIComponent(releaseMatch[1]!)
    const session = findSession(data.sessions, sessionId)
    if (!session) return fail(10004, '会话不存在或已归档', 404)
    if (!session.summary.taken_over) {
      return fail(10005, '该会话当前不由人工坐席持有，无法释放', 409)
    }

    const releasedAt = nowIso()
    session.summary.owner = 'BOT'
    session.summary.taken_over = false
    session.summary.bot_muted = false
    session.summary.agent_id = null
    session.summary.agent_name = null

    session.messages.push({
      message_id: `${sessionId}-M${session.messages.length + 1}`,
      session_id: sessionId,
      sender: 'BOT',
      msg_type: 'TEXT',
      text: '已释放会话，机器人恢复自动应答。',
      text_source: 'ORIGINAL',
      image_ref: null,
      image_alt: null,
      created_at: releasedAt,
      trace_id: null,
      chunk_ids: [],
      decision: null,
      handoff_reason: null,
      latency_ms: null,
      feedback_marked: false,
    })

    return ok({ session_id: sessionId, owner: 'BOT', released_at: releasedAt, bot_muted: false })
  }

  const replayMatch = /^\/v1\/sessions\/([^/]+)\/replay$/.exec(path)
  if (method === 'GET' && replayMatch) {
    const session = findSession(data.sessions, decodeURIComponent(replayMatch[1]!))
    if (!session) return fail(10004, '会话不存在或已归档', 404)
    const turns = session.turns
    return ok({
      session_id: session.summary.session_id,
      channel: session.summary.channel,
      buyer_display: session.summary.buyer_display,
      started_at: turns[0]?.started_at ?? session.summary.last_message_at,
      ended_at: session.summary.last_message_at,
      turns,
    })
  }

  // ── 对话（FE-01 坐席发送）──────────────────────────────────

  if (method === 'POST' && path === '/v1/chat/turn') {
    const body = (req.body ?? {}) as ChatTurnRequest
    const session = findSession(data.sessions, body.session_id ?? '')
    if (!session) return fail(10004, '会话不存在或已归档', 404)
    if (!body.text || !body.text.trim()) return fail(10001, '消息内容不能为空', 400)

    const sender: Sender = body.sender ?? 'AGENT'
    const createdAt = nowIso()
    const traceId = `${session.summary.session_id}-T${session.turns.length + 1}`
    const messageId = `${session.summary.session_id}-M${session.messages.length + 1}`

    session.messages.push({
      message_id: messageId,
      session_id: session.summary.session_id,
      sender,
      msg_type: body.msg_type ?? 'TEXT',
      text: body.text,
      text_source: 'ORIGINAL',
      image_ref: body.image_ref ?? null,
      image_alt: null,
      created_at: createdAt,
      trace_id: sender === 'BOT' ? traceId : null,
      chunk_ids: [],
      decision: null,
      handoff_reason: null,
      latency_ms: null,
      feedback_marked: false,
    })

    session.summary.last_message_at = createdAt
    session.summary.last_message_preview = body.text.slice(0, 60)
    session.summary.last_sender = sender
    session.summary.waiting_seconds = 0

    return ok({
      message_id: messageId,
      session_id: session.summary.session_id,
      created_at: createdAt,
      trace_id: traceId,
    })
  }

  // ── SSE 流式（2.5.4 事件表）────────────────────────────────

  if (method === 'GET' && path === '/v1/chat/stream') {
    await handleSse(req)
    return { raw: true, code: 0 }
  }

  // ── 知识库（FE-04 / FE-05 / FE-06 / FE-07 / FE-08）─────────

  if (method === 'GET' && path === '/v1/kb/chunks') {
    const keyword = (req.query.get('keyword') ?? '').trim().toLowerCase()
    const category = req.query.get('category') ?? ''
    const skuId = req.query.get('sku_id') ?? ''
    const reviewStatus = req.query.get('review_status') ?? ''
    const channel = req.query.get('channel') ?? ''
    const kbType = req.query.get('kb_type') ?? ''

    let items = data.chunks as MockChunk[]
    if (category) items = items.filter((item) => item.category.includes(category))
    if (skuId) items = items.filter((item) => item.sku_ids.includes(skuId))
    if (reviewStatus) items = items.filter((item) => item.review_status === reviewStatus)
    if (channel) items = items.filter((item) => item.channel === channel)
    if (kbType) items = items.filter((item) => item.kb_type === kbType)
    if (keyword) {
      items = items.filter(
        (item) =>
          item.chunk_id.toLowerCase().includes(keyword) ||
          item.title.toLowerCase().includes(keyword) ||
          item.content.toLowerCase().includes(keyword) ||
          item.authority.toLowerCase().includes(keyword),
      )
    }

    // 列表不返回 history 字段，避免 payload 膨胀
    const page = paginate(items, req.query)
    return ok({ ...page, items: page.items.map(stripHistory) })
  }

  if (method === 'POST' && path === '/v1/kb/chunks') {
    const payload = (req.body ?? {}) as KbChunkPayload
    const missing = requiredChunkFields(payload)
    if (missing.length > 0) return fail(10001, `以下字段必填：${missing.join('、')}`, 400)

    const seq = data.chunks.length + 1
    const at = nowIso()
    const chunk: MockChunk = {
      chunk_id: `KB-NEW-${String(seq).padStart(3, '0')}`,
      title: payload.title,
      content: payload.content,
      kb_type: payload.kb_type,
      category: payload.category,
      sku_ids: payload.sku_ids ?? [],
      authority: payload.authority,
      // 新建条目一律 PENDING，必须经审核才能进向量库（FE-05 DoD）
      review_status: 'PENDING',
      reviewer: null,
      review_comment: null,
      effective_from: payload.effective_from ?? at,
      effective_to: payload.effective_to ?? null,
      channel: payload.channel ?? 'ALL',
      version: 1,
      created_by: actorName,
      updated_by: actorName,
      created_at: at,
      updated_at: at,
      char_count: payload.content.replace(/\s/g, '').length,
      history: [],
    }
    data.chunks.unshift(chunk)
    return ok(stripHistory(chunk))
  }

  const chunkIdMatch = /^\/v1\/kb\/chunks\/([^/]+)$/.exec(path)
  if (chunkIdMatch && (method === 'PUT' || method === 'DELETE')) {
    const chunkId = decodeURIComponent(chunkIdMatch[1]!)
    const chunk = data.chunks.find((item) => item.chunk_id === chunkId)
    if (!chunk) return fail(10004, '知识条目不存在', 404)

    if (method === 'DELETE') {
      // 软删：置 DISABLED 并保留版本历史，可恢复（FE-05 DoD）
      chunk.review_status = 'DISABLED'
      chunk.updated_by = actorName
      chunk.updated_at = nowIso()
      return ok({ chunk_id: chunkId, review_status: chunk.review_status })
    }

    const payload = (req.body ?? {}) as KbChunkPayload
    const missing = requiredChunkFields(payload)
    if (missing.length > 0) return fail(10001, `以下字段必填：${missing.join('、')}`, 400)

    // 编辑前把当前内容存入历史（FE-07 版本回溯）
    chunk.history.unshift({
      version: chunk.version,
      chunk_id: chunk.chunk_id,
      title: chunk.title,
      content: chunk.content,
      review_status: chunk.review_status,
      effective_from: chunk.effective_from,
      effective_to: chunk.effective_to,
      updated_by: chunk.updated_by,
      updated_at: chunk.updated_at,
      change_note: `编辑前快照（v${chunk.version}）`,
    })

    chunk.title = payload.title
    chunk.content = payload.content
    chunk.kb_type = payload.kb_type
    chunk.category = payload.category
    chunk.sku_ids = payload.sku_ids ?? []
    chunk.authority = payload.authority
    chunk.effective_from = payload.effective_from ?? chunk.effective_from
    chunk.effective_to = payload.effective_to ?? null
    chunk.channel = payload.channel ?? chunk.channel
    chunk.version += 1
    // 内容变更后必须重新审核
    chunk.review_status = 'PENDING'
    chunk.reviewer = null
    chunk.review_comment = null
    chunk.updated_by = actorName
    chunk.updated_at = nowIso()
    chunk.char_count = payload.content.replace(/\s/g, '').length

    return ok(stripHistory(chunk))
  }

  const reviewMatch = /^\/v1\/kb\/chunks\/([^/]+)\/review$/.exec(path)
  if (method === 'POST' && reviewMatch) {
    const chunkId = decodeURIComponent(reviewMatch[1]!)
    const chunk = data.chunks.find((item) => item.chunk_id === chunkId)
    if (!chunk) return fail(10004, '知识条目不存在', 404)

    const payload = (req.body ?? {}) as KbReviewPayload
    if (payload.decision !== 'APPROVED' && payload.decision !== 'REJECTED') {
      return fail(10001, '审核结论必须为 APPROVED 或 REJECTED', 400)
    }

    chunk.review_status = payload.decision
    chunk.reviewer = payload.reviewer || actorName
    chunk.review_comment = payload.comment ?? null
    chunk.updated_by = chunk.reviewer
    chunk.updated_at = nowIso()

    return ok(stripHistory(chunk))
  }

  const historyMatch = /^\/v1\/kb\/chunks\/([^/]+)\/history$/.exec(path)
  if (method === 'GET' && historyMatch) {
    const chunkId = decodeURIComponent(historyMatch[1]!)
    const chunk = data.chunks.find((item) => item.chunk_id === chunkId)
    if (!chunk) return fail(10004, '知识条目不存在', 404)

    const versions: MockVersion[] = [
      ...chunk.history,
      {
        version: chunk.version,
        chunk_id: chunk.chunk_id,
        title: chunk.title,
        content: chunk.content,
        review_status: chunk.review_status,
        effective_from: chunk.effective_from,
        effective_to: chunk.effective_to,
        updated_by: chunk.updated_by,
        updated_at: chunk.updated_at,
        change_note: '当前版本',
      },
    ].sort((a, b) => b.version - a.version)

    return ok({ chunk_id: chunkId, current_version: chunk.version, versions })
  }

  const rollbackMatch = /^\/v1\/kb\/chunks\/([^/]+)\/rollback$/.exec(path)
  if (method === 'POST' && rollbackMatch) {
    const chunkId = decodeURIComponent(rollbackMatch[1]!)
    const chunk = data.chunks.find((item) => item.chunk_id === chunkId)
    if (!chunk) return fail(10004, '知识条目不存在', 404)

    const body = (req.body ?? {}) as { version?: number }
    const target = chunk.history.find((item) => item.version === body.version)
    if (!target) return fail(10004, `未找到版本 v${body.version}`, 404)

    // 回滚 = 把目标版本内容写回，同时把当前内容压入历史（不丢数据）
    chunk.history.unshift({
      version: chunk.version,
      chunk_id: chunk.chunk_id,
      title: chunk.title,
      content: chunk.content,
      review_status: chunk.review_status,
      effective_from: chunk.effective_from,
      effective_to: chunk.effective_to,
      updated_by: chunk.updated_by,
      updated_at: chunk.updated_at,
      change_note: `回滚到 v${body.version} 前的快照`,
    })

    chunk.title = target.title
    chunk.content = target.content
    chunk.version += 1
    // 回滚后需重新生效审核，5 分钟内生效由后端保证
    chunk.review_status = 'PENDING'
    chunk.reviewer = null
    chunk.review_comment = null
    chunk.updated_by = actorName
    chunk.updated_at = nowIso()
    chunk.char_count = target.content.replace(/\s/g, '').length

    return ok({ chunk_id: chunkId, version: chunk.version, effective_at: nowIso() })
  }

  if (method === 'POST' && path === '/v1/kb/search') {
    const body = (req.body ?? {}) as KbSearchRequest
    const query = (body.query ?? '').trim()
    if (!query) return fail(10001, '检索关键词不能为空', 400)

    const started = Date.now()
    const topK = Math.min(20, Math.max(1, body.top_k ?? 5))
    // HC-3 / FE-04 DoD：只召回已审核通过的条目，未审核不得出现在坐席检索结果中
    const candidates = data.chunks.filter(
      (chunk) => chunk.review_status === 'APPROVED' && chunk.effective_to === null,
    )

    const scored = candidates
      .map((chunk) => ({ chunk, score: scoreChunk(chunk, query) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, topK)

    const hits: KbSearchHit[] = scored.map(({ chunk, score }) => ({
      chunk_id: chunk.chunk_id,
      title: chunk.title,
      snippet: buildSnippet(chunk.content, query),
      content: chunk.content,
      score: Number(score.toFixed(3)),
      category: chunk.category,
      authority: chunk.authority,
      kb_type: chunk.kb_type,
      review_status: chunk.review_status,
      matched_by: pickMatchedBy(chunk, query),
    }))

    // 延迟按命中数浮动，用于验证 FE-04「检索 P95 ≤500ms」的可观测性
    const latency = Date.now() - started + 40 + hits.length * 15
    return ok({
      query,
      hits,
      latency_ms: latency,
      // 无依据必须明确返回，前端据此提示拒答（HC-3）
      no_evidence: hits.length === 0 || hits[0]!.score < 0.35,
    })
  }

  if (method === 'POST' && path === '/v1/kb/import/preview') {
    return await previewImport(req)
  }

  if (method === 'POST' && path === '/v1/kb/import') {
    const body = (req.body ?? {}) as ImportCommitRequest
    const task = data.imports.get(body.import_id ?? '')
    if (!task) return fail(10004, '导入任务不存在或已过期，请重新上传', 404)
    const actorName = currentActor(req).agent_name

    const excluded = new Set(body.excluded_indexes ?? [])
    const accepted = task.chunks.filter((_, index) => !excluded.has(index))
    const at = nowIso()

    // 真正写入知识库：一律 PENDING，需审核后生效
    accepted.forEach((chunk, index) => {
      const seq = data.chunks.length + index + 1
      data.chunks.unshift({
        ...chunk,
        chunk_id: `KB-IMP-${String(seq).padStart(4, '0')}`,
        kb_type: body.kb_type ?? chunk.kb_type,
        category: body.category ?? chunk.category,
        authority: body.authority ?? chunk.authority,
        channel: body.channel ?? chunk.channel,
        review_status: 'PENDING',
        reviewer: null,
        review_comment: null,
        created_at: at,
        updated_at: at,
        created_by: actorName,
        updated_by: actorName,
      })
    })

    task.status = excluded.size > 0 ? 'PARTIAL_FAILED' : 'DONE'
    task.succeeded = accepted.length
    task.failed = excluded.size
    task.finished_at = at
    if (excluded.size > 0) {
      task.failures = [...excluded].map((index) => ({
        row: index + 1,
        reason: '人工在预览中剔除',
        raw: task.chunks[index]?.title ?? '',
      }))
    }

    return ok(toProgress(task))
  }

  const progressMatch = /^\/v1\/kb\/import\/([^/]+)\/progress$/.exec(path)
  if (method === 'GET' && progressMatch) {
    const task = data.imports.get(decodeURIComponent(progressMatch[1]!))
    if (!task) return fail(10004, '导入任务不存在', 404)
    return ok(toProgress(task))
  }

  if (method === 'POST' && path === '/v1/kb/feedback') {
    const body = (req.body ?? {}) as {
      message_id?: string
      session_id?: string
      chunk_ids?: string[]
      error_type?: FeedbackItem['error_type']
      comment?: string
    }
    if (!body.message_id || !body.session_id) {
      return fail(10001, 'message_id 与 session_id 必填', 400)
    }
    if (!body.error_type) return fail(10001, '请选择错误类型', 400)

    // 幂等：同一条消息重复标记时返回已有记录，避免待办重复堆积
    const existing = data.feedback.find((item) => item.message_id === body.message_id)
    if (existing) return ok(existing)

    const item: FeedbackItem = {
      feedback_id: `FB-${String(data.feedback.length + 1).padStart(4, '0')}`,
      message_id: body.message_id,
      session_id: body.session_id,
      chunk_ids: body.chunk_ids ?? [],
      error_type: body.error_type,
      comment: body.comment ?? '',
      submitted_by: actorName,
      submitted_at: nowIso(),
      status: 'OPEN',
      assignee: null,
      resolved_chunk_id: null,
      resolution: null,
    }
    data.feedback.unshift(item)

    // 同步会话里的消息标记，坐席台气泡上能立刻看到「已标记」
    const session = findSession(data.sessions, body.session_id)
    const message = session?.messages.find((m) => m.message_id === body.message_id)
    if (message) message.feedback_marked = true

    return ok(item)
  }

  if (method === 'GET' && path === '/v1/kb/feedback') {
    const errorType = req.query.get('error_type') ?? ''
    const status = req.query.get('status') ?? ''
    const assignee = req.query.get('assignee') ?? ''

    let items = [...data.feedback]
    if (errorType) items = items.filter((item) => item.error_type === errorType)
    if (status) items = items.filter((item) => item.status === status)
    if (assignee) items = items.filter((item) => item.assignee === assignee)
    items.sort((a, b) => Date.parse(b.submitted_at) - Date.parse(a.submitted_at))

    return ok(paginate(items, req.query))
  }

  const feedbackMatch = /^\/v1\/kb\/feedback\/([^/]+)$/.exec(path)
  if (method === 'PUT' && feedbackMatch) {
    const feedbackId = decodeURIComponent(feedbackMatch[1]!)
    const item = data.feedback.find((entry) => entry.feedback_id === feedbackId)
    if (!item) return fail(10004, '纠错记录不存在', 404)

    const body = (req.body ?? {}) as Partial<FeedbackItem>
    if (body.status) item.status = body.status
    if (body.assignee !== undefined) item.assignee = body.assignee
    if (body.resolved_chunk_id !== undefined) item.resolved_chunk_id = body.resolved_chunk_id
    if (body.resolution !== undefined) item.resolution = body.resolution

    return ok(item)
  }

  // ── 工单（FE-02）───────────────────────────────────────────

  if (method === 'GET' && path === '/v1/tickets') {
    const reason = req.query.get('reason') ?? ''
    const priority = req.query.get('priority') ?? ''
    const status = req.query.get('status') ?? ''
    const keyword = (req.query.get('keyword') ?? '').trim().toLowerCase()

    let items = [...data.tickets]
    if (reason) items = items.filter((item) => item.reason === reason)
    if (priority) items = items.filter((item) => item.priority === priority)
    if (status) items = items.filter((item) => item.status === status)
    if (keyword) {
      items = items.filter(
        (item) =>
          item.ticket_no.toLowerCase().includes(keyword) ||
          item.session_id.toLowerCase().includes(keyword) ||
          item.buyer_display.toLowerCase().includes(keyword) ||
          item.subject.toLowerCase().includes(keyword),
      )
    }

    // 紧急优先、超时优先，再按等待时长倒序 —— 坐席最该先处理谁一目了然
    const priorityWeight: Record<string, number> = { URGENT: 0, HIGH: 1, NORMAL: 2, LOW: 3 }
    items.sort((a, b) => {
      if (a.sla_breached !== b.sla_breached) return a.sla_breached ? -1 : 1
      const diff = (priorityWeight[a.priority] ?? 9) - (priorityWeight[b.priority] ?? 9)
      if (diff !== 0) return diff
      return b.waiting_seconds - a.waiting_seconds
    })

    return ok(paginate(items, req.query, 50))
  }

  const ticketMatch = /^\/v1\/tickets\/([^/]+)$/.exec(path)
  if (method === 'GET' && ticketMatch) {
    const ticket = data.tickets.find((item) => item.ticket_no === decodeURIComponent(ticketMatch[1]!))
    if (!ticket) return fail(10004, '工单不存在', 404)
    return ok(ticket)
  }

  // ── 指标（FE-09）──────────────────────────────────────────

  if (method === 'GET' && path === '/v1/metrics/overview') {
    const metrics = sortMetricsForDashboard(buildMetrics(data.random))
    const days = readQueryInt(req.query, 'days', 30)
    return ok({
      metrics,
      range: { start: minutesAgo(days * 24 * 60).slice(0, 10), end: nowIso().slice(0, 10) },
      data_complete: true,
      sample_size: data.sessions.length,
    })
  }

  if (method === 'GET' && path === '/v1/metrics/trend') {
    const granularity = (req.query.get('granularity') ?? 'DAY').toUpperCase() === 'WEEK' ? 'WEEK' : 'DAY'
    const requested = readQueryList(req.query, 'metric_ids') as MetricId[]
    const metricIds = requested.length > 0 ? requested : [...METRIC_IDS].slice(0, 4)
    const days = readQueryInt(req.query, 'days', 30)
    const buckets = granularity === 'WEEK' ? Math.min(12, Math.ceil(days / 7)) : Math.min(60, days)
    return ok({
      granularity,
      metric_ids: metricIds,
      points: buildTrend(data.random, granularity, metricIds, buckets),
    })
  }

  if (method === 'GET' && path === '/v1/metrics/distribution') {
    return ok({
      channels: buildChannelDistribution(data.sessions),
      intents: buildIntentDistribution(data.sessions),
      funnel: buildHandoffFunnel(data.sessions),
    })
  }

  // ── 配置（FE-11 / FE-12 / FE-13）──────────────────────────

  if (method === 'GET' && path === '/v1/config/channels') {
    // 返回前按当前时间重算 days_to_expiry，保证到期提醒始终基于真实时间
    const channels = data.channels.map((channel) => {
      const days = channel.expires_at ? daysUntil(channel.expires_at) : null
      let authStatus = channel.auth_status
      if (days !== null) {
        authStatus = days < 0 ? 'EXPIRED' : days <= 15 ? 'EXPIRING' : 'AUTHORIZED'
      }
      return { ...channel, days_to_expiry: days, auth_status: authStatus }
    })
    return ok({ channels })
  }

  const channelMatch = /^\/v1\/config\/channels\/([^/]+)$/.exec(path)
  if (method === 'PUT' && channelMatch) {
    const channelCode = decodeURIComponent(channelMatch[1]!) as Channel
    const channel = data.channels.find((item) => item.channel === channelCode)
    if (!channel) return fail(10004, '渠道不存在', 404)

    const body = (req.body ?? {}) as {
      service_provider_certified?: boolean
      service_provider_note?: string
    }
    if (body.service_provider_certified !== undefined) {
      channel.service_provider_certified = body.service_provider_certified
    }
    if (body.service_provider_note !== undefined) {
      channel.service_provider_note = body.service_provider_note
    }
    channel.updated_at = nowIso()
    channel.updated_by = actorName

    // 认证状态变更必须留日志（FE-11 DoD）
    data.channelChangeLog.unshift({
      at: channel.updated_at,
      actor: actorName,
      detail: `更新 ${channelCode} 服务商认证状态为 ${String(body.service_provider_certified)}`,
    })

    return ok(channel)
  }

  if (method === 'GET' && path === '/v1/config/agent-groups') {
    return ok({
      groups: data.groups.map(toAgentGroup),
      slot_labels: [...SLOT_LABELS],
      updated_at: nowIso(),
    })
  }

  if (method === 'PUT' && path === '/v1/config/agent-groups') {
    const body = (req.body ?? {}) as {
      groups?: Array<{ group_id: string; schedule: boolean[][] }>
    }
    for (const patch of body.groups ?? []) {
      const group = data.groups.find((item) => item.group_id === patch.group_id)
      if (!group) continue
      // 校验网格形状：7 天 × 7 时段，避免前端传半截数据把排班写坏
      if (patch.schedule.length !== 7 || patch.schedule.some((row) => row.length !== 7)) {
        return fail(10001, `分组 ${patch.group_id} 的排班网格必须为 7×7`, 400)
      }
      group.schedule = patch.schedule
    }
    return ok({
      groups: data.groups.map(toAgentGroup),
      slot_labels: [...SLOT_LABELS],
      updated_at: nowIso(),
    })
  }

  if (method === 'GET' && path === '/v1/config/agent-groups/conflicts') {
    return ok({ conflicts: detectConflicts(data.groups) })
  }

  if (method === 'GET' && path === '/v1/config/handoff-rules') {
    return ok({ rules: data.rules, test_sample_size: 50 })
  }

  if (method === 'PUT' && path === '/v1/config/handoff-rules') {
    const body = (req.body ?? {}) as {
      rules?: Array<{
        rule_id: string
        enabled: boolean
        priority: number
        params: Array<{ key: string; value: number | string | boolean }>
      }>
    }
    const changes: string[] = []

    for (const patch of body.rules ?? []) {
      const rule = data.rules.find((item) => item.rule_id === patch.rule_id)
      if (!rule) continue

      // FE-13 铁律：显式请求与高风险意图不允许关闭，也不允许降级到挽留分支
      if (rule.no_retention_locked && patch.enabled === false) {
        return fail(10001, `规则「${rule.name}」不允许关闭（不得挽留）`, 400)
      }

      if (rule.enabled !== patch.enabled) {
        changes.push(`${rule.name}：${rule.enabled ? '关闭' : '启用'}`)
        rule.enabled = patch.enabled
      }
      if (rule.priority !== patch.priority) {
        changes.push(`${rule.name}：优先级 ${rule.priority} → ${patch.priority}`)
        rule.priority = patch.priority
      }
      for (const param of patch.params ?? []) {
        const target = rule.params.find((item) => item.key === param.key)
        if (!target || target.value === param.value) continue
        // 数值型参数做范围校验，防止把「连续 N 轮」设成 0 或 999
        if (target.type === 'number' && typeof param.value === 'number') {
          const min = target.min ?? -Infinity
          const max = target.max ?? Infinity
          if (param.value < min || param.value > max) {
            return fail(10001, `「${target.label}」取值范围为 ${min} ~ ${max}`, 400)
          }
        }
        changes.push(`${rule.name}·${target.label}：${target.value} → ${param.value}`)
        target.value = param.value
      }
      rule.updated_at = nowIso()
      rule.updated_by = actorName
    }

    if (changes.length > 0) {
      data.ruleChangeLog.unshift({
        at: nowIso(),
        actor: actorName,
        detail: changes.join('；'),
      })
    }

    data.rules.sort((a, b) => a.priority - b.priority)
    return ok({ rules: data.rules, test_sample_size: 50 })
  }

  if (method === 'PUT' && path === '/v1/config/handoff-rules/test') {
    const body = (req.body ?? {}) as { rules?: HandoffRule[]; sample_size?: number }
    const sampleSize = Math.min(200, Math.max(1, body.sample_size ?? 50))
    const rules = body.rules ?? data.rules
    return ok(runRuleTest(data.sessions, rules, sampleSize))
  }

  return null
}

// ─────────────────────────────────────────────────────────────
// SSE：按 2.5.4 事件表逐帧推送
// ─────────────────────────────────────────────────────────────

async function handleSse(req: MockRequest): Promise<void> {
  const res = req.res
  const sessionId = req.query.get('session_id') ?? ''
  const data = store()
  const session = findSession(data.sessions, sessionId)

  res.statusCode = 200
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8')
  res.setHeader('Cache-Control', 'no-cache, no-transform')
  res.setHeader('Connection', 'keep-alive')
  // 关闭 Nginx/代理缓冲，否则事件会被攒着一次性下发，打字机效果就没了
  res.setHeader('X-Accel-Buffering', 'no')
  res.flushHeaders?.()

  const send = (event: string, payload: unknown): void => {
    res.write(`event: ${event}\n`)
    res.write(`data: ${JSON.stringify(payload)}\n\n`)
  }

  // 用最新的买家问题作为这一轮输入；没有会话时退化成一条通用咨询
  const lastUser = [...(session?.messages ?? [])].reverse().find((m) => m.sender === 'USER')
  const question = lastUser?.text ?? '这款玩偶的洗护方式是什么？'
  const traceId = `${sessionId || 'CS-LIVE-0000'}-T${(session?.turns.length ?? 0) + 1}`

  // ?__sse=error 注入错误事件，用于验证前端兜底话术 + 重试按钮
  const forceError = req.query.get('__sse') === 'error'
  // ?__sse=handoff 注入转人工卡片事件
  const forceHandoff = req.query.get('__sse') === 'handoff'

  const timers: NodeJS.Timeout[] = []
  const schedule = (ms: number, fn: () => void): void => {
    timers.push(setTimeout(fn, ms))
  }

  // 心跳：SSE 长连接下防止中间层因空闲断开（前端 3 秒内重连的前提是能感知断开）
  const heartbeat = setInterval(() => {
    res.write(': ping\n\n')
  }, 15000)

  const cleanup = (): void => {
    clearInterval(heartbeat)
    for (const timer of timers) clearTimeout(timer)
    res.end()
  }

  req.headers['x-mock-sse'] = '1'
  res.on('close', cleanup)

  schedule(120, () => send('turn.start', { trace_id: traceId, session_id: sessionId }))
  schedule(320, () => send('intent', { intent: guessIntent(question), confidence: 0.93 }))
  schedule(560, () => send('retrieval', { hits: 3, top_score: 0.91 }))

  if (forceError) {
    schedule(900, () => send('error', { code: 30001, message: '下游服务暂时不可用，请稍后重试' }))
    schedule(1200, cleanup)
    return
  }

  schedule(780, () => send('action', { action: 'query_order', status: 'running' }))
  schedule(1180, () => send('action', { action: 'query_order', status: 'success' }))

  const reply = buildReply(question)
  // 逐字下发 delta，前端据此实现打字机效果；落库以 turn.end 的完整 reply 为准
  const chunks = splitForStreaming(reply)
  let cursor = 1500
  for (const piece of chunks) {
    const text = piece
    schedule(cursor, () => send('delta', { text }))
    cursor += 60
  }

  schedule(cursor + 200, () =>
    send('turn.end', { reply, actions: ['query_order'], latency_ms: cursor }),
  )

  if (forceHandoff) {
    schedule(cursor + 500, () =>
      send('handoff', {
        reason: 'USER_REQUEST',
        ticket_no: session?.summary.ticket_no ?? 'TS202601010001',
        queue_position: 1,
      }),
    )
    schedule(cursor + 700, cleanup)
    return
  }

  // 不主动关闭：保持连接以观察心跳与「连接保持」状态；
  // 但演示场景下 30 秒后收尾，避免 dev server 上连接越积越多。
  schedule(cursor + 30000, cleanup)
}

function splitForStreaming(text: string): string[] {
  const pieces: string[] = []
  let buffer = ''
  for (const char of text) {
    buffer += char
    // 按标点或每 8 个字符切一刀，兼顾流畅度与事件数量
    if (/[，。！？；、~～]/.test(char) || buffer.length >= 8) {
      pieces.push(buffer)
      buffer = ''
    }
  }
  if (buffer) pieces.push(buffer)
  return pieces
}

function guessIntent(question: string): string {
  if (/物流|快递|到哪|发货|运单/.test(question)) return 'ORDER_LOGISTICS'
  if (/退|换|售后|质量/.test(question)) return 'AFTER_SALE'
  if (/优惠|活动|券|满减/.test(question)) return 'MARKETING'
  if (/投诉|举报|曝光|差评/.test(question)) return 'COMPLAINT'
  if (/正品|授权|品牌/.test(question)) return 'BRAND_TRUST'
  return 'PRODUCT_CONSULT'
}

function buildReply(question: string): string {
  if (/物流|快递|到哪|发货|运单/.test(question)) {
    return '亲，您的包裹目前正在运输途中，最新一条轨迹是「已到达【杭州转运中心】」，预计 1-2 天送达。需要我帮您催一下快递吗？'
  }
  if (/退|换|售后|质量/.test(question)) {
    return '亲，签收后 7 天内支持无理由退货，商品保持吊牌完整即可；如果是质量问题，退回运费由我们承担，您把订单号发我，我这就帮您登记。'
  }
  if (/洗护|清洗|保养|机洗/.test(question)) {
    return '这款玩偶建议 30℃ 以下温水手洗，装洗衣袋轻柔模式也可以，不要漂白、不要高温烘干，阴凉通风处平铺晾干后拍揉即可恢复蓬松。'
  }
  return '亲，这款目前是现货，下单后一般 48 小时内发出，支持 7 天无理由退换。您还想了解尺寸、材质还是洗护方式呢？'
}

// ─────────────────────────────────────────────────────────────
// FE-06 导入预览
// ─────────────────────────────────────────────────────────────

async function previewImport(req: MockRequest): Promise<MockResult> {
  const contentType = String(req.headers['content-type'] ?? '')
  const parts = parseMultipart(req.rawBuffer, contentType)
  const filePart = parts.find((part) => part.name === 'file') ?? parts[0]

  if (!filePart || filePart.data.length === 0) {
    return fail(10001, '未接收到上传文件，请选择 CSV / XLSX / MD 文件', 400)
  }

  const fileName = filePart.filename ?? 'unnamed.csv'
  const lower = fileName.toLowerCase()
  if (!/\.(csv|xlsx|xls|md|markdown|txt)$/.test(lower)) {
    return fail(10001, '仅支持 CSV / XLSX / MD 格式', 400)
  }

  const { text: decoded, rows: decodedRows } = readUploadedText(filePart)
  let rows = decodedRows
  let text = decoded

  if (lower.endsWith('.csv')) {
    rows = parseCsv(decodeText(filePart.data))
    text = rowsToPlainText(rows)
  }
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) {
    text = decodeText(filePart.data)
  }

  if (!text.trim()) return fail(10001, '文件内容为空，无法切分', 400)

  const detection = detectKbType(rows, text, fileName)
  const units = splitIntoChunks(text, detection.detected_type, CHUNK_OVERLAP)
  const range = CHUNK_LENGTH_RANGE[detection.detected_type]

  const importId = `IMP-${Date.now().toString(36)}`
  const previewChunks: ImportPreviewChunk[] = units.map((unit) => ({
    index: unit.index,
    title: buildChunkTitle(unit.text, unit.index, fileName),
    content: unit.text,
    char_count: unit.charCount,
    // 正确标记超长 / 过短块（FE-06 DoD）
    length_flag: unit.charCount > range.max ? 'TOO_LONG' : unit.charCount < range.min ? 'TOO_SHORT' : 'OK',
    source_ref: `第 ${unit.index} 段`,
  }))

  const warnings = [...detection.warnings]
  const tooLong = previewChunks.filter((item) => item.length_flag === 'TOO_LONG').length
  const tooShort = previewChunks.filter((item) => item.length_flag === 'TOO_SHORT').length
  if (tooLong > 0) warnings.push(`${tooLong} 个切分单元超出 ${range.max} 字上限，建议拆分后再导入`)
  if (tooShort > 0) warnings.push(`${tooShort} 个切分单元不足 ${range.min} 字，召回时可能缺乏上下文`)

  // 保存预览结果，commit 时按 import_id 取回（两步式导入：先预览，人工确认后才真导入）
  const task: ImportTaskState = {
    import_id: importId,
    status: 'RUNNING',
    total: previewChunks.length,
    succeeded: 0,
    failed: 0,
    failures: [],
    started_at: nowIso(),
    finished_at: null,
    chunks: previewChunks.map((item) => ({
      chunk_id: '',
      title: item.title,
      content: item.content,
      kb_type: detection.detected_type,
      category: '',
      sku_ids: [],
      authority: '',
      review_status: 'PENDING' as ReviewStatus,
      reviewer: null,
      review_comment: null,
      effective_from: null,
      effective_to: null,
      channel: 'ALL' as const,
      version: 1,
      created_by: currentActor(req).agent_name,
      updated_by: currentActor(req).agent_name,
      created_at: nowIso(),
      updated_at: nowIso(),
      char_count: item.char_count,
      history: [],
    })),
  }
  store().imports.set(importId, task)

  const response: ImportPreviewResponse = {
    import_id: importId,
    file_name: fileName,
    detected_type: detection.detected_type,
    total_chunks: previewChunks.length,
    chunks: previewChunks,
    overlap_ratio: CHUNK_OVERLAP,
    type_candidates: detection.candidates,
    warnings,
  }
  return ok(response)
}

function rowsToPlainText(rows: string[][]): string {
  if (rows.length === 0) return ''
  const header = rows[0]!.map((cell, index) => (cell?.trim() ? cell.trim() : `第${index + 1}列`))
  return rows
    .slice(1)
    .map((row) => {
      const filled = row.map((cell) => (cell ?? '').trim())
      if (filled.every((cell) => !cell)) return ''
      return filled.map((cell, index) => (cell ? `${header[index]}：${cell}` : '')).filter(Boolean).join('；')
    })
    .filter(Boolean)
    .join('\n\n')
}

function buildChunkTitle(text: string, index: number, fileName: string): string {
  const firstLine = text.split('\n').find((line) => line.trim()) ?? ''
  const clean = firstLine.replace(/^#+\s*/, '').replace(/\s+/g, ' ').trim()
  if (!clean) return `${fileName} 第 ${index} 单元`
  return clean.length > 40 ? `${clean.slice(0, 40)}…` : clean
}

// ─────────────────────────────────────────────────────────────
// FE-04 检索打分（Mock 的关键词 + 类目加权，真实实现走向量召回）
// ─────────────────────────────────────────────────────────────

function scoreChunk(chunk: MockChunk, query: string): number {
  const haystack = `${chunk.title} ${chunk.content} ${chunk.category} ${chunk.authority}`.toLowerCase()
  const target = query.toLowerCase()
  let score = 0

  // 整串命中权重最高（最接近语义召回的强相关）
  if (haystack.includes(target)) score += 0.7

  // 按 2 字滑窗做字符级匹配，中文下比空格分词更稳
  const grams = new Set<string>()
  for (let i = 0; i + 1 < target.length; i += 1) grams.add(target.slice(i, i + 2))
  let hit = 0
  for (const gram of grams) {
    if (gram.trim() && haystack.includes(gram)) hit += 1
  }
  if (grams.size > 0) score += (hit / grams.size) * 0.5

  // 商品知识里命中 SKU 时加权（坐席经常直接搜 SKU）
  if (chunk.sku_ids.some((sku) => target.includes(sku.toLowerCase()))) score += 0.25

  // FAQ 标题命中问题关键词时加权
  if (chunk.kb_type === 'FAQ' && target.length >= 2 && chunk.title.toLowerCase().includes(target)) {
    score += 0.15
  }

  return Math.min(0.99, score)
}

function buildSnippet(content: string, query: string): string {
  const clean = content.replace(/\s+/g, ' ').trim()
  const target = query.toLowerCase()
  const at = clean.toLowerCase().indexOf(target)
  if (at === -1) return clean.slice(0, 120)
  const start = Math.max(0, at - 40)
  const end = Math.min(clean.length, at + target.length + 80)
  return `${start > 0 ? '…' : ''}${clean.slice(start, end)}${end < clean.length ? '…' : ''}`
}

function pickMatchedBy(chunk: MockChunk, query: string): KbSearchHit['matched_by'] {
  const target = query.toLowerCase()
  if (chunk.sku_ids.some((sku) => target.includes(sku.toLowerCase()))) return 'sku'
  if (chunk.kb_type === 'FAQ' && chunk.title.toLowerCase().includes(target)) return 'faq'
  if (chunk.title.toLowerCase().includes(target)) return 'keyword'
  return 'vector'
}

// ─────────────────────────────────────────────────────────────
// FE-13 规则试算
// ─────────────────────────────────────────────────────────────

function runRuleTest(
  sessions: MockSession[],
  rules: HandoffRule[],
  sampleSize: number,
): HandoffRuleTestResult {
  // 固定种子抽样：每次试算结果一致，便于与后端抽样 50 条对齐核验
  const step = Math.max(1, Math.floor(sessions.length / sampleSize))
  const sample = sessions.filter((_, index) => index % step === 0).slice(0, sampleSize)

  const enabled = [...rules].filter((rule) => rule.enabled).sort((a, b) => a.priority - b.priority)
  const perRule = enabled.map((rule) => ({ rule_id: rule.rule_id, name: rule.name, hits: 0 }))

  const details: HandoffRuleTestResult['details'] = []
  let matched = 0

  for (const session of sample) {
    const hit = matchRule(session, enabled)
    if (hit) {
      matched += 1
      const entry = perRule.find((item) => item.rule_id === hit.rule_id)
      if (entry) entry.hits += 1
    }
    details.push({
      session_id: session.summary.session_id,
      matched_rule: hit?.rule_id ?? null,
      reason: hit ? `命中「${hit.name}」` : '未命中任何规则，继续机器人应答',
    })
  }

  // 与线上已保存配置的差异条数（草稿试算时用得上）
  const online = store().rules
  const diffCount = rules.filter((rule) => {
    const base = online.find((item) => item.rule_id === rule.rule_id)
    if (!base) return true
    if (base.enabled !== rule.enabled || base.priority !== rule.priority) return true
    return rule.params.some((param) => {
      const target = base.params.find((item) => item.key === param.key)
      return !target || target.value !== param.value
    })
  }).length

  return { sample_size: sample.length, matched, per_rule: perRule, diff_count: diffCount, details }
}

function matchRule(session: MockSession, rules: HandoffRule[]): HandoffRule | null {
  const ticket = session.summary.ticket_no
  if (!ticket) return null
  const reason = session.messages.find((m) => m.handoff_reason)?.handoff_reason

  for (const rule of rules) {
    if (rule.code === 'EXPLICIT_REQUEST' && (reason === 'USER_REQUEST' || reason === undefined)) return rule
    if (rule.code === 'HIGH_RISK' && reason === 'HIGH_RISK') return rule
    if (rule.code === 'EMOTION' && reason === 'EMOTION') return rule
    if (rule.code === 'UNRESOLVED' && reason === 'UNRESOLVED') return rule
    if (rule.code === 'SYSTEM_ERROR' && reason === 'SYSTEM_ERROR') return rule
  }
  return rules[0] ?? null
}

// ─────────────────────────────────────────────────────────────
// 工具函数
// ─────────────────────────────────────────────────────────────

function stripHistory(chunk: MockChunk): Omit<MockChunk, 'history'> {
  const { history: _history, ...rest } = chunk
  void _history
  return rest
}

function requiredChunkFields(payload: KbChunkPayload): string[] {
  const labels: Array<[keyof KbChunkPayload, string]> = [
    ['title', '标题'],
    ['content', '正文'],
    ['kb_type', '知识类型'],
    ['category', '类目'],
    ['authority', '权威来源'],
  ]
  return labels.filter(([key]) => !payload?.[key]).map(([, label]) => label)
}

function toProgress(task: ImportTaskState): ImportProgress {
  return {
    import_id: task.import_id,
    status: task.status,
    total: task.total,
    succeeded: task.succeeded,
    failed: task.failed,
    failures: task.failures,
    started_at: task.started_at,
    finished_at: task.finished_at,
  }
}

function toAgentGroup(group: {
  group_id: string
  group_name: string
  service_scope: string
  agent_count: number
  supervisor: string
  schedule: boolean[][]
  channels: Channel[]
  daily_volume: number
  peak_volume: number
  note: string
  online_window: string
}): AgentGroup & { online_window: string } {
  return {
    group_id: group.group_id,
    group_name: group.group_name,
    service_scope: group.service_scope,
    agent_count: group.agent_count,
    supervisor: group.supervisor,
    schedule: group.schedule,
    channels: group.channels,
    daily_volume: group.daily_volume,
    peak_volume: group.peak_volume,
    note: group.note,
    online_window: group.online_window,
  }
}

function daysUntil(date: string): number {
  const end = Date.parse(`${date}T23:59:59Z`)
  return Math.ceil((end - Date.now()) / 86_400_000)
}

/** 时段表头（与 mock/seed/agent-groups.ts 的 SLOT_LABELS 保持一致） */
const SLOT_LABELS = [
  '08:00–10:00',
  '10:00–12:00',
  '12:00–14:00',
  '14:00–16:00',
  '16:00–18:00',
  '18:00–20:00',
  '20:00–22:00',
]

/** 七要素完整性自检 —— Mock 也遵守同一口径，缺失时由前端展示明确错误 */
export function checkContextCompleteness(context: Partial<SessionContext>): string[] {
  return CONTEXT_REQUIRED_FIELDS.filter((field) => {
    const value = context[field]
    if (value === undefined || value === null) return true
    if (Array.isArray(value) && value.length === 0 && field !== 'tried_actions') return true
    return false
  })
}

/** 调试注入的错误码 —— 覆盖 2.5.6 全部 6 个业务错误码 */
function forcedError(code: number): MockResult | null {
  switch (code) {
    case 10001:
      return fail(10001, '参数校验失败', 400)
    case 10002:
      return fail(10002, '坐席未登录或登录态已过期', 401)
    case 10003:
      return fail(10003, '无权限查看该资源', 403)
    case 10004:
      return fail(10004, '资源不存在', 404)
    case 10005:
      return fail(10005, '该会话已被 李敏 接管', 409)
    case 20001:
      return fail(20001, '请求过于频繁，请稍后再试', 429)
    case 30001:
      return fail(30001, '下游服务（商家接口/LLM）暂不可用', 503)
    case 40001:
      return fail(40001, '服务器内部错误', 500)
    default:
      return null
  }
}

// 未使用但保留的导出，供后续「新建会话」演示与单测使用
export { createLiveSession, buildMessagesForTest }

function buildMessagesForTest(): ChatMessage[] {
  return []
}
