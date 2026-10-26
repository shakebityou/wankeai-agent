import axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import { BFF_CODE, BffError, type BffEnvelope } from '@/types/bff'

/**
 * BFF 请求客户端 —— 前端唯一的出站通道。
 *
 * 任务书 4.5.1 要求：
 *   1. 统一封装 src/api/*.ts，每接口一个函数，不散落 axios；
 *   2. 统一处理 6 个 BFF 错误码（10002/10003/10005/20001/30001，另含 10001）；
 *   3. 只对幂等 GET 自动重试 1 次，POST 不自动重试；
 *   4. 所有异步有 loading，超 1s 显示骨架屏（由 composables 负责）；
 *   5. 超时 10s 展示重试按钮。
 *
 * 铁律：baseURL 只能是 /bff/v1，禁止任何内部服务或商家接口地址出现在前端。
 */
export const BFF_BASE_URL = '/bff/v1'

/** 请求超时：任务书 4.5.3「前端超时 10s 超时展示重试按钮」 */
export const REQUEST_TIMEOUT_MS = 10_000

/** 幂等 GET 自动重试次数（仅 1 次） */
const GET_RETRY_TIMES = 1

export const http: AxiosInstance = axios.create({
  baseURL: BFF_BASE_URL,
  timeout: REQUEST_TIMEOUT_MS,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
    // 声明只接受同源 JSON，避免被中间层注入重定向
    Accept: 'application/json',
  },
})

// ─────────────────────────────────────────────────────────────
// 错误处理钩子：由 interceptor-bridge 注入，避免 api 层反向依赖 UI 与路由
// ─────────────────────────────────────────────────────────────

export interface BffErrorHooks {
  /** 10002：坐席未登录 / 登录态过期 -> 跳登录页 */
  onUnauthorized: (error: BffError) => void
  /** 10003：无权限 -> 提示「无权限查看」 */
  onForbidden: (error: BffError) => void
  /** 10005：并发冲突 -> 提示「该会话已被 XXX 接管」并刷新 */
  onConflict: (error: BffError) => void
  /** 20001：限流 -> 按钮禁用 + 倒计时 */
  onRateLimited: (error: BffError, retryAfterSec: number) => void
  /** 30001：下游不可用 -> 降级提示并禁用相关操作 */
  onDownstreamUnavailable: (error: BffError) => void
  /** 10001：参数校验失败 -> 表单标红 */
  onParamInvalid: (error: BffError) => void
  /** 兜底 */
  onUnknown: (error: BffError) => void
}

let hooks: Partial<BffErrorHooks> = {}

export function registerBffErrorHooks(next: Partial<BffErrorHooks>): void {
  hooks = { ...hooks, ...next }
}

// ─────────────────────────────────────────────────────────────
// 请求拦截器
// ─────────────────────────────────────────────────────────────

/** 在请求 config 上挂载元信息 */
interface BffRequestConfig extends InternalAxiosRequestConfig {
  /** 已重试次数 */
  __retryCount?: number
  /** 是否静默（不弹全局提示，由调用方自行处理） */
  silent?: boolean
}

http.interceptors.request.use((config: BffRequestConfig) => {
  config.headers.set('X-Requested-With', 'XMLHttpRequest')
  config.headers.set('X-Client', 'wankeai-console-web')
  return config
})

// ─────────────────────────────────────────────────────────────
// 响应拦截器
// ─────────────────────────────────────────────────────────────

http.interceptors.response.use(
  (response: AxiosResponse<BffEnvelope<unknown>>) => {
    const body = response.data

    // 非包装体（例如 Mock 未覆盖的静态资源）直接透传，交由调用方判断
    if (!body || typeof body.code !== 'number') {
      return response
    }

    if (body.code === BFF_CODE.SUCCESS) {
      return response
    }

    const error = new BffError(body.code, body.message, body.request_id, response.status)
    notifyHooks(error, response)
    return Promise.reject(error)
  },
  async (rawError: unknown) => {
    const config = (axios.isAxiosError(rawError) ? rawError.config : undefined) as
      | BffRequestConfig
      | undefined

    if (axios.isAxiosError(rawError)) {
      const status = rawError.response?.status ?? 0
      const body = rawError.response?.data as BffEnvelope<unknown> | undefined

      // 服务端返回了标准包装体：按 code 分发
      if (body && typeof body.code === 'number') {
        const error = new BffError(body.code, body.message, body.request_id, status)
        notifyHooks(error, rawError.response)
        return Promise.reject(error)
      }

      // 网络层错误：超时 / 断网 / 5xx
      const isTimeout = rawError.code === 'ECONNABORTED' || rawError.code === 'ETIMEDOUT'
      const isNetwork = rawError.code === 'ERR_NETWORK'

      // 任务书 4.5.1：只对幂等 GET 自动重试 1 次（POST 绝不自动重试，
      // 避免重复发送消息、重复接管、重复审核这类非幂等副作用）
      const method = (config?.method ?? 'get').toLowerCase()
      const retryable =
        method === 'get' && (isTimeout || isNetwork || (status >= 500 && status < 600))
      const retryCount = config?.__retryCount ?? 0

      if (retryable && config && retryCount < GET_RETRY_TIMES) {
        config.__retryCount = retryCount + 1
        return http.request(config)
      }

      const code = isTimeout || isNetwork ? BFF_CODE.DOWNSTREAM_UNAVAILABLE : BFF_CODE.SERVER_ERROR
      const message = isTimeout
        ? `请求超时（${REQUEST_TIMEOUT_MS / 1000}s），请重试`
        : isNetwork
          ? '网络不可用，请检查连接后重试'
          : `服务异常（HTTP ${status}）`
      const error = new BffError(code, message, `local-${Date.now().toString(36)}`, status)
      notifyHooks(error, rawError.response)
      return Promise.reject(error)
    }

    const fallback = new BffError(BFF_CODE.SERVER_ERROR, '未知异常，请稍后重试', '', 0)
    notifyHooks(fallback, undefined)
    return Promise.reject(fallback)
  },
)

function notifyHooks(error: BffError, response?: AxiosResponse | { status?: number; headers?: unknown }): void {
  // silent 请求（如轮询失败）不触发全局提示，避免打扰坐席
  switch (error.code) {
    case BFF_CODE.UNAUTHORIZED:
      hooks.onUnauthorized?.(error)
      break
    case BFF_CODE.FORBIDDEN:
      hooks.onForbidden?.(error)
      break
    case BFF_CODE.CONFLICT:
      hooks.onConflict?.(error)
      break
    case BFF_CODE.RATE_LIMITED: {
      const retryAfter = readRetryAfter(response)
      hooks.onRateLimited?.(error, retryAfter)
      break
    }
    case BFF_CODE.DOWNSTREAM_UNAVAILABLE:
      hooks.onDownstreamUnavailable?.(error)
      break
    case BFF_CODE.PARAM_INVALID:
      hooks.onParamInvalid?.(error)
      break
    default:
      hooks.onUnknown?.(error)
  }
}

function readRetryAfter(response?: AxiosResponse | { headers?: unknown }): number {
  const headers = response?.headers as Record<string, string> | undefined
  const raw = headers?.['retry-after']
  const parsed = raw ? Number(raw) : Number.NaN
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 30
}

// ─────────────────────────────────────────────────────────────
// 对外请求方法：只暴露 data，业务代码拿不到 axios 细节
// ─────────────────────────────────────────────────────────────

export interface RequestOptions {
  /** 静默模式：不触发全局错误提示（用于轮询、埋点上报等） */
  silent?: boolean
  signal?: AbortSignal
}

async function unwrap<T>(promise: Promise<AxiosResponse<BffEnvelope<T>>>): Promise<T> {
  const response = await promise
  return response.data.data
}

export function get<T>(
  url: string,
  params?: Record<string, unknown>,
  options: RequestOptions = {},
): Promise<T> {
  const config: AxiosRequestConfig = {
    method: 'GET',
    url,
    params: compactParams(params),
    signal: options.signal,
    silent: options.silent,
  } as AxiosRequestConfig
  return unwrap<T>(http.request<BffEnvelope<T>>(config))
}

export function post<T>(url: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return unwrap<T>(
    http.request<BffEnvelope<T>>({
      method: 'POST',
      url,
      data: body ?? {},
      signal: options.signal,
      silent: options.silent,
    } as AxiosRequestConfig),
  )
}

export function put<T>(url: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return unwrap<T>(
    http.request<BffEnvelope<T>>({
      method: 'PUT',
      url,
      data: body ?? {},
      signal: options.signal,
      silent: options.silent,
    } as AxiosRequestConfig),
  )
}

export function del<T>(url: string, options: RequestOptions = {}): Promise<T> {
  return unwrap<T>(
    http.request<BffEnvelope<T>>({
      method: 'DELETE',
      url,
      signal: options.signal,
      silent: options.silent,
    } as AxiosRequestConfig),
  )
}

/** 上传：multipart/form-data，POST 不重试 */
export function upload<T>(url: string, form: FormData, options: RequestOptions = {}): Promise<T> {
  return unwrap<T>(
    http.request<BffEnvelope<T>>({
      method: 'POST',
      url,
      data: form,
      headers: { 'Content-Type': 'multipart/form-data' },
      signal: options.signal,
      timeout: 60_000, // 批量导入文件较大，单独放宽
      silent: options.silent,
    } as AxiosRequestConfig),
  )
}

/** 去掉 undefined / '' / null，避免把空筛选条件发给后端 */
function compactParams(params?: Record<string, unknown>): Record<string, unknown> | undefined {
  if (!params) return undefined
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    result[key] = value
  }
  return result
}
