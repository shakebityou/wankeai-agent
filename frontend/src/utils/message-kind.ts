import type { ChatMessage, MessageKind } from '@/types/contract'

/**
 * 消息展示类型推导 —— 任务书 FE-01 DoD 要求「五种 msg_type 渲染正确」。
 *
 * 后端只回 TEXT / IMAGE 两种，其余三种是推导出来的：
 *   - VOICE        ：TEXT 且 text_source = ASR（语音转文字）
 *   - OCR          ：TEXT 且 text_source = OCR / VLM（图片识别结果）
 *   - HANDOFF_CARD ：带 handoff_reason 的消息（转人工卡片）
 *
 * 集中在这里推导，避免 MessageBubble / 回放页 / 工单页各写一套判断。
 */
export function resolveMessageKind(message: ChatMessage): MessageKind {
  if (message.handoff_reason) return 'HANDOFF_CARD'
  if (message.msg_type === 'IMAGE') return 'IMAGE'
  if (message.text_source === 'ASR') return 'VOICE'
  if (message.text_source === 'OCR' || message.text_source === 'VLM') return 'OCR'
  return 'TEXT'
}

export const MESSAGE_KIND_LABELS: Record<MessageKind, string> = {
  TEXT: '文本',
  IMAGE: '图片',
  VOICE: '语音转写',
  OCR: '图片识别',
  HANDOFF_CARD: '转人工卡片',
}

/** 各类消息的角标图标（纯文本，不用图标字体，避免外链字体） */
export const MESSAGE_KIND_GLYPHS: Record<MessageKind, string> = {
  TEXT: '',
  IMAGE: '🖼️',
  VOICE: '🎙️',
  OCR: '🔍',
  HANDOFF_CARD: '🎫',
}

/**
 * HC-1 合规守卫：判断一段文本里是否含可点击外链。
 *
 * 用途有两处：
 *   1. 渲染前拦截 —— 命中则不渲染为链接（气泡内一律按纯文本处理）；
 *   2. 自动化检查 —— compliance-check.mjs 复用同一正则，保证「代码里守的」与
 *      「脚本查的」是同一套口径。
 */
export const URL_PATTERN = /(https?:\/\/|www\.)[^\s，。；）)】」]+/gi

export function containsUrl(text: string | null | undefined): boolean {
  if (!text) return false
  URL_PATTERN.lastIndex = 0
  return URL_PATTERN.test(text)
}

/**
 * 把文本里可能出现的外链替换为「[链接已屏蔽]」。
 *
 * 注意：HC-1 要求「不生成、不发送任何外链」，所以这里不是「让链接不可点」，
 * 而是从根本上不让链接出现在用户可见内容里。
 */
export function stripUrls(text: string | null | undefined): string {
  if (!text) return ''
  return text.replace(URL_PATTERN, '[链接已屏蔽]')
}
