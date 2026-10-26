/**
 * BFF 错误码与统一响应契约 —— 对齐任务书 2.5.5 / 2.5.6。
 *
 * 注意：这 8 个码是「项目组自用」的 BFF 错误码，与商家侧错误码严格区隔，
 * 不允许在业务代码里用裸数字判断，一律引用本文件常量。
 */

/** 2.5.6 BFF 错误码 */
export const BFF_CODE = {
  SUCCESS: 0,
  /** 400 参数校验失败 -> 表单标红 */
  PARAM_INVALID: 10001,
  /** 401 坐席未登录 / 登录态过期 -> 跳登录页 */
  UNAUTHORIZED: 10002,
  /** 403 无权限 -> 提示「无权限查看」 */
  FORBIDDEN: 10003,
  /** 404 资源不存在 -> 提示并返回列表 */
  NOT_FOUND: 10004,
  /** 409 并发冲突 -> 提示「该会话已被 XXX 接管」并刷新 */
  CONFLICT: 10005,
  /** 429 请求过于频繁 -> 按钮禁用 + 倒计时 */
  RATE_LIMITED: 20001,
  /** 503 下游（商家接口 / LLM）不可用 -> 降级提示并禁用相关操作 */
  DOWNSTREAM_UNAVAILABLE: 30001,
  /** 兜底：未归类的服务端错误 */
  SERVER_ERROR: 40001,
} as const

export type BffCode = (typeof BFF_CODE)[keyof typeof BFF_CODE]

/** 2.5.5 统一响应包装 */
export interface BffEnvelope<T> {
  code: number
  message: string
  request_id: string
  data: T
}

/** 2.5.5 分页响应：{total,page,page_size,items:[]} */
export interface Paged<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

/** 2.5.5 分页请求参数：page 默认 1，page_size 默认 20 最大 50 */
export interface PageQuery {
  page?: number
  page_size?: number
}

/** 统一的业务错误对象：拦截器抛出，页面 catch 后按需展示。 */
export class BffError extends Error {
  readonly code: number
  readonly requestId: string
  readonly httpStatus: number

  constructor(code: number, message: string, requestId: string, httpStatus: number) {
    super(message)
    this.name = 'BffError'
    this.code = code
    this.requestId = requestId
    this.httpStatus = httpStatus
  }

  get isUnauthorized(): boolean {
    return this.code === BFF_CODE.UNAUTHORIZED
  }

  get isForbidden(): boolean {
    return this.code === BFF_CODE.FORBIDDEN
  }

  get isConflict(): boolean {
    return this.code === BFF_CODE.CONFLICT
  }

  get isRateLimited(): boolean {
    return this.code === BFF_CODE.RATE_LIMITED
  }

  get isDownstreamDown(): boolean {
    return this.code === BFF_CODE.DOWNSTREAM_UNAVAILABLE
  }
}

/** 错误码 -> 用户可见文案（4.5.1 要求的统一提示口径） */
export function messageForCode(code: number): string {
  switch (code) {
    case BFF_CODE.PARAM_INVALID:
      return '提交的参数有误，请检查标红字段后重试'
    case BFF_CODE.UNAUTHORIZED:
      return '登录态已过期，请重新登录'
    case BFF_CODE.FORBIDDEN:
      return '无权限查看'
    case BFF_CODE.NOT_FOUND:
      return '资源不存在或已被删除'
    case BFF_CODE.CONFLICT:
      return '该会话已被其他坐席接管'
    case BFF_CODE.RATE_LIMITED:
      return '操作过于频繁，请稍后再试'
    case BFF_CODE.DOWNSTREAM_UNAVAILABLE:
      return '下游服务暂不可用，相关操作已暂时禁用'
    default:
      return '服务异常，请稍后重试'
  }
}
