import { describe, expect, it } from 'vitest'
import { guardContext, CONTEXT_ERROR_MESSAGE } from '@/utils/context-guard'
import type { SessionContext } from '@/types/contract'

/**
 * 单测：七要素守卫 —— FE-02 DoD「七要素缺一即报错，绝不静默空白」。
 *
 * 这是整个前端唯一一条「宁可不显示也不许猜」的规则：
 * AI 上下文卡片驱动坐席的每一句回复，七要素缺任意一项，
 * 说明上游（AI-02 上下文构建）已经坏了。此时渲染半张卡片
 * 会让坐席误以为信息是完整的，把错误答案发给买家。
 *
 * 关键口径（来自 context-guard.ts 的实现）：
 *   - verified 必须非空（无事实可展示说明上下文没生成成功）；
 *   - tried_actions 允许空数组（一个动作都没尝试是合法业务状态）；
 *   - turn_count 允许 0（首轮），只拒绝 null / undefined；
 *   - unmet_demand 必须是非空字符串（空串视为缺失：真实无诉求时
 *     服务端会给出「无」之类的占位，而不是空字段）。
 */

function completeContext(): SessionContext {
  return {
    session_id: 's1',
    buyer_id: 'b1',
    channel: 'TAOBAO',
    waiting_seconds: 12,
    summary: '买家咨询奶油小熊 20cm 的尺寸',
    verified: [
      { type: 'product', label: '商品名', value: '奶油小熊 20cm' },
      { type: 'logistics', label: '尺寸', value: '20cm' },
    ],
    tried_actions: [{ action: '检索商品知识卡', ok: true }],
    unmet_demand: '无',
    suggested_action: '回答尺寸问题',
    emotion: { level: 'NEUTRAL', intensity: '轻度' },
    turn_count: 3,
    attachments: [],
    ticket_no: null,
    handoff_url: null,
  }
}

describe('guardContext', () => {
  it('七要素齐全时通过', () => {
    const result = guardContext(completeContext())
    expect(result.ok).toBe(true)
    expect(result.missing).toEqual([])
    expect(result.context).not.toBeNull()
  })

  it('unmet_demand 为「无」时算完整（服务端对无诉求的占位口径）', () => {
    const result = guardContext(completeContext())
    expect(result.ok).toBe(true)
  })

  it('unmet_demand 为空串即失败（空字段说明上下文没生成成功）', () => {
    const result = guardContext({ ...completeContext(), unmet_demand: '' })
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('unmet_demand')
  })

  it('unmet_demand 有值时也算完整', () => {
    const result = guardContext({ ...completeContext(), unmet_demand: '想要小票' })
    expect(result.ok).toBe(true)
  })

  it('缺 summary 即失败', () => {
    const context = completeContext()
    delete (context as unknown as Record<string, unknown>).summary
    const result = guardContext(context)
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('summary')
    expect(result.missingLabels).toContain('对话摘要')
  })

  it('缺 emotion 即失败', () => {
    const context = completeContext()
    delete (context as unknown as Record<string, unknown>).emotion
    const result = guardContext(context)
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('emotion')
  })

  it('缺 tried_actions 即失败', () => {
    const context = completeContext()
    delete (context as unknown as Record<string, unknown>).tried_actions
    const result = guardContext(context)
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('tried_actions')
  })

  it('verified 为空数组即失败（无事实可展示说明上下文没生成成功）', () => {
    const result = guardContext({ ...completeContext(), verified: [] })
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('verified')
  })

  it('tried_actions 为空数组合法（一个动作都没尝试是合法业务状态）', () => {
    const result = guardContext({ ...completeContext(), tried_actions: [] })
    expect(result.ok).toBe(true)
  })

  it('turn_count 为 0 合法（首轮）', () => {
    const result = guardContext({ ...completeContext(), turn_count: 0 })
    expect(result.ok).toBe(true)
  })

  it('turn_count 为 null 即失败', () => {
    const context = completeContext()
    ;(context as unknown as Record<string, unknown>).turn_count = null
    const result = guardContext(context)
    expect(result.ok).toBe(false)
  })

  it('emotion 缺 intensity 即失败', () => {
    const result = guardContext({ ...completeContext(), emotion: { level: 'ANGRY' } as never })
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('emotion')
  })

  it('空字符串字段算缺失', () => {
    const result = guardContext({ ...completeContext(), summary: '   ' })
    expect(result.ok).toBe(false)
    expect(result.missing).toContain('summary')
  })

  it('整个上下文为 null 时七要素全部缺失', () => {
    const result = guardContext(null)
    expect(result.ok).toBe(false)
    expect(result.missing).toHaveLength(7)
  })

  it('失败时必须给出固定错误文案', () => {
    expect(CONTEXT_ERROR_MESSAGE).toBe('上下文生成异常，请联系技术支持')
  })
})
