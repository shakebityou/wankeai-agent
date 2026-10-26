/**
 * Mock 请求上下文与统一响应工具。
 *
 * 严格对齐任务书 2.5.5 统一响应包装：
 *   成功 {code:0, message:"success", request_id, data:{}}
 *   失败 {code:40001, message, request_id, data:null}
 * 以及 2.5.6 的 BFF 错误码（0/10001/10002/10003/10004/10005/20001/30001）。
 */
import type { ServerResponse } from 'node:http'

export type BffErrorCode =
  | 0
  | 10001
  | 10002
  | 10003
  | 10004
  | 10005
  | 20001
  | 30001
  | 40001

export interface MockRequest {
  method: string
  path: string
  query: URLSearchParams
  /** JSON 解析后的请求体；解析失败时为原始字符串 */
  body: unknown
  rawBody: string
  /** 原始字节 —— FE-06 的 XLSX 上传是二进制，不能按 UTF-8 字符串处理 */
  rawBuffer: Buffer
  headers: Record<string, string | string[] | undefined>
  res: ServerResponse
  /** 由中间件解析 Cookie 后注入的登录态；未登录为 null */
  auth?: MockAuth | null
}

/** 中间件从会话表解析出的当前登录用户 */
export interface MockAuth {
  token: string
  user_id: string
  username: string
  display_name: string
  role: 'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN'
  group_id: string | null
  group_name: string | null
  channels: string[]
}

export type MockHandler = (req: MockRequest) => MockResult | Promise<MockResult>

export interface MockResult {
  status?: number
  code?: BffErrorCode
  message?: string
  data?: unknown
  /** 直接接管响应（SSE 等流式场景）。返回 true 表示已处理完毕。 */
  raw?: boolean
}

let requestSeq = 0

export function nextRequestId(prefix = 'req'): string {
  requestSeq += 1
  return `${prefix}-${Date.now().toString(36)}-${requestSeq.toString(36)}`
}

export function ok(data: unknown, message = 'success'): MockResult {
  return { status: 200, code: 0, message, data }
}

export function fail(code: BffErrorCode, message: string, status = 200): MockResult {
  return { status, code, message, data: null }
}

/** 2.5.5 分页响应结构：{total,page,page_size,items:[]} */
export interface Paged<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

export function paginate<T>(all: T[], query: URLSearchParams, defaultSize = 20): Paged<T> {
  const page = Math.max(1, Number(query.get('page') ?? 1) || 1)
  // 2.5.5：page_size 默认 20，最大 50
  const rawSize = Number(query.get('page_size') ?? defaultSize) || defaultSize
  const pageSize = Math.min(50, Math.max(1, rawSize))
  const start = (page - 1) * pageSize
  return {
    total: all.length,
    page,
    page_size: pageSize,
    items: all.slice(start, start + pageSize),
  }
}

export function writeJson(res: ServerResponse, result: MockResult, requestId = nextRequestId()): void {
  const payload = {
    code: result.code ?? 0,
    message: result.message ?? (result.code === 0 || result.code === undefined ? 'success' : 'failed'),
    request_id: requestId,
    data: result.code === undefined || result.code === 0 ? (result.data ?? {}) : null,
  }
  res.statusCode = result.status ?? 200
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.setHeader('Cache-Control', 'no-store')
  // 前端拦截器依赖该头做幂等重试与错误码识别，Mock 与真实 BFF 保持一致。
  res.setHeader('X-Request-Id', requestId)
  res.end(JSON.stringify(payload))
}

/** 模拟网络延迟，用于验证骨架屏（>1s 显示）与超时重试（10s）。 */
export function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export function readQueryInt(query: URLSearchParams, key: string, fallback: number): number {
  const raw = query.get(key)
  if (raw === null || raw === '') return fallback
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function readQueryList(query: URLSearchParams, key: string): string[] {
  const raw = query.get(key)
  if (!raw) return []
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}
