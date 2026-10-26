import type {
  SseAction,
  SseDelta,
  SseError,
  SseEvent,
  SseHandoff,
  SseIntent,
  SseRetrieval,
  SseTurnEnd,
  SseTurnStart,
} from '@/types/contract'

/**
 * SSE 流式客户端 —— 任务书 4.1 明确「原生 EventSource（SSE）流式（不用 WebSocket）」。
 *
 * 2.5.4 事件表：
 *   turn.start {trace_id,session_id}   -> 显示正在输入
 *   intent     {intent,confidence}     -> 可选显示意图标签
 *   retrieval  {hits,top_score}        -> 可选显示「已命中 N 条知识」
 *   delta      {text}                  -> 打字机追加
 *   action     {action,status}         -> 状态条「正在查询订单…」
 *   turn.end   {reply,actions,latency_ms} -> 结束并落库
 *   handoff    {reason,ticket_no,queue_position} -> 转人工卡片
 *   error      {code,message}          -> 兜底话术 + 重试按钮
 *
 * 关键设计：
 *   1. 断线 3 秒内重连且不重复渲染 —— 原生 EventSource 自带 Last-Event-ID 重连，
 *      但我们额外记录已渲染的 trace_id，重连后若同一 trace_id 重放 delta 则跳过；
 *   2. delta 只用于展示，最终以 turn.end 的完整 reply 为准落库（任务书原文）；
 *   3. 事件分发用类型收窄，避免 any。
 */

export const SSE_EVENT_NAMES = [
  'turn.start',
  'intent',
  'retrieval',
  'delta',
  'action',
  'turn.end',
  'handoff',
  'error',
] as const

export type SseEventName = (typeof SSE_EVENT_NAMES)[number]

export interface SseHandlers {
  onOpen?: () => void
  onTurnStart?: (data: SseTurnStart) => void
  onIntent?: (data: SseIntent) => void
  onRetrieval?: (data: SseRetrieval) => void
  onDelta?: (data: SseDelta) => void
  onAction?: (data: SseAction) => void
  onTurnEnd?: (data: SseTurnEnd) => void
  onHandoff?: (data: SseHandoff) => void
  onError?: (data: SseError) => void
  /** 连接层状态变化（用于 UI 显示「重连中」） */
  onStatusChange?: (status: SseConnectionStatus) => void
}

export type SseConnectionStatus = 'IDLE' | 'CONNECTING' | 'OPEN' | 'RECONNECTING' | 'CLOSED'

export interface SseClientOptions extends SseHandlers {
  /** 重连基础间隔（毫秒）。EventSource 默认 3s，任务书要求 3 秒内重连 */
  baseRetryMs?: number
  /** 最大重连次数，超过后置为 CLOSED 并提示 */
  maxRetries?: number
}

/** 原生 EventSource 封装：含重连、去重、优雅关闭 */
export class SseClient {
  private source: EventSource | null = null
  private readonly url: string
  private readonly options: SseClientOptions
  private retries = 0
  private closedByUser = false
  private retryTimer: number | null = null
  /** 已完整渲染过的 trace_id，用于重连去重 */
  private readonly renderedTraces = new Set<string>()
  /** 当前正在渲染的 trace_id */
  private activeTraceId: string | null = null
  private status: SseConnectionStatus = 'IDLE'

  constructor(sessionId: string, options: SseClientOptions = {}) {
    // 只连 BFF，且 SSE 端点固定为 /bff/v1/chat/stream
    this.url = `/bff/v1/chat/stream?session_id=${encodeURIComponent(sessionId)}`
    this.options = { baseRetryMs: 1000, maxRetries: 5, ...options }
  }

  get connectionStatus(): SseConnectionStatus {
    return this.status
  }

  get currentTraceId(): string | null {
    return this.activeTraceId
  }

  connect(): void {
    if (this.source) return
    this.closedByUser = false
    this.setStatus('CONNECTING')

    const source = new EventSource(this.url, { withCredentials: true })
    this.source = source

    source.onopen = () => {
      this.retries = 0
      this.setStatus('OPEN')
      this.options.onOpen?.()
    }

    for (const name of SSE_EVENT_NAMES) {
      source.addEventListener(name, (event) => {
        this.dispatch(name, event as MessageEvent<string>)
      })
    }

    source.onerror = () => {
      // EventSource 的 onerror 也用于「服务端正常结束」，需区分：
      // readyState === CLOSED 表示不会自动重连，需要我们自己退避重连。
      if (this.closedByUser) return
      if (source.readyState === EventSource.CLOSED) {
        this.scheduleReconnect()
      } else {
        this.setStatus('RECONNECTING')
      }
    }
  }

  close(): void {
    this.closedByUser = true
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
    this.source?.close()
    this.source = null
    this.setStatus('CLOSED')
  }

  /** 供 UI 判断某个 trace 是否已渲染完成（重连去重） */
  hasRendered(traceId: string): boolean {
    return this.renderedTraces.has(traceId)
  }

  private scheduleReconnect(): void {
    if (this.retryTimer !== null) return
    if (this.retries >= (this.options.maxRetries ?? 5)) {
      this.setStatus('CLOSED')
      this.options.onError?.({
        code: 30001,
        message: '实时消息通道多次重连失败，请刷新页面后重试',
      })
      return
    }
    this.retries += 1
    this.setStatus('RECONNECTING')
    // 退避：1s、2s、3s… 首次重连控制在任务书要求的 3 秒内
    const delay = Math.min((this.options.baseRetryMs ?? 1000) * this.retries, 5000)
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null
      this.source?.close()
      this.source = null
      this.connect()
    }, delay)
  }

  private setStatus(status: SseConnectionStatus): void {
    if (this.status === status) return
    this.status = status
    this.options.onStatusChange?.(status)
  }

  private dispatch(name: SseEventName, event: MessageEvent<string>): void {
    const parsed = safeParse(event.data)
    if (parsed === undefined) return

    switch (name) {
      case 'turn.start': {
        const data = parsed as SseTurnStart
        // 重连后同一 trace 重放：不重复开启新的渲染轮次
        if (this.renderedTraces.has(data.trace_id)) return
        this.activeTraceId = data.trace_id
        this.options.onTurnStart?.(data)
        break
      }
      case 'intent':
        this.options.onIntent?.(parsed as SseIntent)
        break
      case 'retrieval':
        this.options.onRetrieval?.(parsed as SseRetrieval)
        break
      case 'delta': {
        // 已落库的轮次不再追加，避免重连后消息重复
        if (this.activeTraceId && this.renderedTraces.has(this.activeTraceId)) return
        this.options.onDelta?.(parsed as SseDelta)
        break
      }
      case 'action':
        this.options.onAction?.(parsed as SseAction)
        break
      case 'turn.end': {
        const data = parsed as SseTurnEnd
        // 落库以 turn.end 的完整 reply 为准；同时标记该 trace 已渲染
        if (this.activeTraceId) this.renderedTraces.add(this.activeTraceId)
        this.activeTraceId = null
        this.options.onTurnEnd?.(data)
        break
      }
      case 'handoff':
        this.options.onHandoff?.(parsed as SseHandoff)
        break
      case 'error':
        this.options.onError?.(parsed as SseError)
        break
      default:
        break
    }
  }
}

function safeParse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

/** 把 SSE 事件名映射到联合类型的 type 字段（供测试与调试面板使用） */
export function toSseEvent(name: SseEventName, data: unknown): SseEvent {
  return { type: name, data } as SseEvent
}
