import { describe, expect, it } from 'vitest'
import { resolveMessageKind, containsUrl, stripUrls, MESSAGE_KIND_LABELS } from '@/utils/message-kind'
import type { ChatMessage } from '@/types/contract'

/**
 * 单测：消息展示类型推导 —— FE-01 DoD「五种 msg_type 渲染正确」。
 *
 * 这一层是全项目最容易静默出错的地方：msg_type 只有 TEXT / IMAGE 两种，
 * VOICE / OCR / HANDOFF_CARD 全靠推导，推导规则一旦写反，
 * 坐席会把语音转写当成普通文本回，把转人工卡片当成机器人消息忽略。
 */

function build(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    message_id: 'm1',
    session_id: 's1',
    msg_type: 'TEXT',
    sender: 'USER',
    text: '你好',
    text_source: 'ORIGINAL',
    created_at: '2026-01-15T10:00:00Z',
    image_ref: null,
    feedback_marked: false,
    handoff_reason: null,
    ...overrides,
  } as ChatMessage
}

describe('resolveMessageKind', () => {
  it('普通文本 → TEXT', () => {
    expect(resolveMessageKind(build())).toBe('TEXT')
  })

  it('IMAGE 类型 → IMAGE', () => {
    expect(resolveMessageKind(build({ msg_type: 'IMAGE' }))).toBe('IMAGE')
  })

  it('ASR 来源 → VOICE', () => {
    expect(resolveMessageKind(build({ text_source: 'ASR' }))).toBe('VOICE')
  })

  it('OCR / VLM 来源 → OCR', () => {
    expect(resolveMessageKind(build({ text_source: 'OCR' }))).toBe('OCR')
    expect(resolveMessageKind(build({ text_source: 'VLM' }))).toBe('OCR')
  })

  it('带 handoff_reason → HANDOFF_CARD（优先级最高）', () => {
    expect(
      resolveMessageKind(build({ msg_type: 'IMAGE', text_source: 'ASR', handoff_reason: 'USER_REQUEST' })),
    ).toBe('HANDOFF_CARD')
  })

  it('每种展示类型都有中文标签', () => {
    for (const label of Object.values(MESSAGE_KIND_LABELS)) {
      expect(label).toBeTruthy()
    }
  })
})

describe('URL 识别（HC-1 不外链）', () => {
  it('识别 http / https 链接', () => {
    expect(containsUrl('看这个 https://item.taobao.com/xxx')).toBe(true)
    expect(containsUrl('看这个 http://a.com')).toBe(true)
  })

  it('普通文本不含链接', () => {
    expect(containsUrl('请问什么时候发货？')).toBe(false)
  })

  it('stripUrls 剥离所有链接并留占位', () => {
    const stripped = stripUrls('链接一 https://a.com 中间文字 http://b.cn/x 结尾')
    expect(stripped).not.toContain('http')
    expect(stripped).toContain('链接一')
    expect(stripped).toContain('结尾')
  })
})
