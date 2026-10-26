import { onScopeDispose, ref, shallowRef } from 'vue'
import { SseClient, type SseConnectionStatus } from '@/api/sse'
import type { ChatMessage, SseTurnEnd } from '@/types/contract'

/**
 * 实时消息流组合式函数 —— FE-01。
 *
 * 职责边界（为什么单独抽一层）：
 *   SseClient 只管「连接与去重」，组件只管「渲染」，
 *   中间这层负责「把事件翻译成 UI 需要的状态」——
 *   正在输入 / 阶段文字 / 检索命中 / 意图 / 流式文本 / 兜底错误。
 *   若不抽这层，MessageStream 的父组件会被 8 个 ref 淹没。
 *
 * 关键行为（对齐任务书 2.5.4）：
 *   - turn.start  -> typing = true，重置本轮所有临时状态
 *   - intent      -> intent / confidence
 *   - retrieval   -> retrievalHits / retrievalTopScore
 *   - action      -> stage（「正在查询订单…」）
 *   - delta       -> 累积到 streamingText（打字机）
 *   - turn.end    -> 用完整 reply 落库（streamingText 清空，交给调用方决定如何落库）
 *   - handoff     -> 由调用方处理（转人工卡片）
 *   - error       -> streamError，并结束 typing
 *
 * 「落库以 turn.end 的完整 reply 为准」的落地方式：
 *   onTurnEnd 回调把 SseTurnEnd 交给调用方，由它构造 ChatMessage 追加到 messages；
 *   本层同时清空 streamingText，因此 delta 期间的内容绝不会变成两条消息。
 */
export interface ChatStreamHandlers {
  /** 一轮完整结束：调用方在此把 reply 落库为一条 BOT 消息 */
  onTurnEnd?: (payload: SseTurnEnd) => void
  /** 转人工：调用方在此插入转人工卡片并刷新会话状态 */
  onHandoff?: (payload: { reason: string; ticket_no: string; queue_position: number }) => void
  /** 消息需要刷新（例如收到落库消息后重新拉详情） */
  onMessageSettled?: (message: ChatMessage) => void
}

export function useChatStream(sessionId: string, handlers: ChatStreamHandlers = {}) {
  const client = shallowRef<SseClient | null>(null)
  const status = ref<SseConnectionStatus>('IDLE')

  const typing = ref(false)
  const stage = ref('')
  const retrievalHits = ref<number | null>(null)
  const retrievalTopScore = ref<number | null>(null)
  const intent = ref('')
  const confidence = ref<number | null>(null)
  const streamingText = ref('')
  const streamError = ref<string | null>(null)

  /** 重置本轮临时状态（turn.start 与 turn.end 都要调用） */
  function resetTurnState(): void {
    stage.value = ''
    retrievalHits.value = null
    retrievalTopScore.value = null
    intent.value = ''
    confidence.value = null
    streamingText.value = ''
  }

  function connect(): void {
    if (client.value) return

    const instance = new SseClient(sessionId, {
      baseRetryMs: 1000,
      maxRetries: 5,
      onStatusChange: (next) => {
        status.value = next
      },
      onTurnStart: () => {
        resetTurnState()
        streamError.value = null
        typing.value = true
      },
      onIntent: (data) => {
        intent.value = data.intent
        confidence.value = data.confidence
      },
      onRetrieval: (data) => {
        retrievalHits.value = data.hits
        retrievalTopScore.value = data.top_score
      },
      onAction: (data) => {
        // action 事件带 status（running / success），running 时才展示阶段文字
        stage.value = data.status === 'running' ? data.action : ''
      },
      onDelta: (data) => {
        // 首个 delta 到达即结束「正在输入」，切换为打字机
        typing.value = false
        streamingText.value += data.text
      },
      onTurnEnd: (data) => {
        typing.value = false
        resetTurnState()
        handlers.onTurnEnd?.(data)
      },
      onHandoff: (data) => {
        typing.value = false
        resetTurnState()
        handlers.onHandoff?.({
          reason: data.reason,
          ticket_no: data.ticket_no,
          queue_position: data.queue_position,
        })
      },
      onError: (data) => {
        typing.value = false
        stage.value = ''
        streamError.value = data.message || '实时消息通道异常，请重试'
      },
    })

    client.value = instance
    instance.connect()
  }

  function close(): void {
    client.value?.close()
    client.value = null
    status.value = 'CLOSED'
    typing.value = false
    resetTurnState()
  }

  /** 重试：断开后重连（error 事件后的「重试」按钮） */
  function retry(): void {
    streamError.value = null
    close()
    connect()
  }

  /** 由父级在手动发送消息后调用，模拟进入「正在输入」 */
  function beginLocalTurn(): void {
    resetTurnState()
    streamError.value = null
    typing.value = true
  }

  onScopeDispose(close)

  return {
    status,
    typing,
    stage,
    retrievalHits,
    retrievalTopScore,
    intent,
    confidence,
    streamingText,
    streamError,
    connect,
    close,
    retry,
    beginLocalTurn,
  }
}
