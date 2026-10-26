import { post } from './client'
import type { ChatTurnRequest, ChatTurnResponse } from '@/types/contract'

/**
 * 对话接口 —— 坐席台内发送消息（FE-01）。
 *
 * 注意：流式回复不走本文件，走 src/api/sse.ts 的原生 EventSource（任务书 4.1 锁定）。
 */

/** POST /bff/v1/chat/turn —— 会话内发送消息；坐席接管后 sender=AGENT */
export function sendChatTurn(payload: ChatTurnRequest): Promise<ChatTurnResponse> {
  return post<ChatTurnResponse>('/chat/turn', payload)
}
