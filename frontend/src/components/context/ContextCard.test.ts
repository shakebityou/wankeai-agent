import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ContextCard from '@/components/context/ContextCard.vue'
import type { SessionContext } from '@/types/contract'

/**
 * 单测：ContextCard —— FE-02 核心组件，DoD 要求覆盖率 ≥80%。
 *
 * 测试重点是**守卫行为**而不是渲染细节：
 *   - 七要素齐全 → 渲染七要素本体；
 *   - 缺任一要素 → 整卡换成「上下文生成异常」错误态，且列出缺失项；
 *   - 绝不静默空白（这是任务书写死的 DoD）。
 *
 * 其余状态（loading / error / retry）各测一条，保证三态齐全。
 */

function completeContext(): SessionContext {
  return {
    session_id: 's1',
    buyer_id: 'b1',
    channel: 'TAOBAO',
    waiting_seconds: 95,
    summary: '买家咨询奶油小熊 20cm 的尺寸与发货时间',
    verified: [
      { type: 'product', label: '商品名', value: '奶油小熊 20cm' },
      { type: 'logistics', label: '发货地', value: '浙江杭州' },
    ],
    tried_actions: [
      { action: '检索商品知识卡', ok: true },
      { action: '查询订单物流', ok: false, detail: '接口超时' },
    ],
    unmet_demand: '希望今天内发货',
    suggested_action: '确认加急发货并回复预计时效',
    emotion: { level: 'DISSATISFIED', intensity: '中度', trigger: '等太久了' },
    turn_count: 4,
    attachments: [{ type: 'PRODUCT_IMAGE', tag: '主图', url: '/mock-assets/a.jpg' }],
    ticket_no: 'TK-20260115-001',
    handoff_url: null,
  }
}

describe('ContextCard', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  it('七要素齐全时渲染摘要与各要素，不出现错误态', () => {
    const wrapper = mount(ContextCard, {
      props: { context: completeContext(), sessionId: 's1' },
    })
    expect(wrapper.find('[data-testid="context-guard-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('买家咨询奶油小熊 20cm 的尺寸与发货时间')
    expect(wrapper.text()).toContain('已验证事实')
    expect(wrapper.text()).toContain('已尝试动作')
  })

  it('七要素缺一即整卡报错（绝不静默空白）', () => {
    const context = completeContext()
    delete (context as unknown as Record<string, unknown>).summary
    const wrapper = mount(ContextCard, {
      props: { context, sessionId: 's1' },
    })
    const error = wrapper.find('[data-testid="context-guard-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('上下文生成异常，请联系技术支持')
    expect(error.text()).toContain('对话摘要')
    // 报错态不应再渲染正常正文
    expect(wrapper.text()).not.toContain('已验证事实')
  })

  it('报错态同时列出全部缺失要素（便于坐席上报）', () => {
    const wrapper = mount(ContextCard, {
      props: { context: null, sessionId: 's1' },
    })
    const error = wrapper.find('[data-testid="context-guard-error"]')
    expect(error.text()).toContain('缺少 7 项必需要素')
  })

  it('缺字段时上报埋点（写入 sessionStorage 留痕）', () => {
    const context = completeContext()
    delete (context as unknown as Record<string, unknown>).emotion
    mount(ContextCard, { props: { context, sessionId: 's1' } })
    const raw = window.sessionStorage.getItem('wankeai.telemetry.context')
    expect(raw).toBeTruthy()
    const buffer = JSON.parse(raw as string) as Array<{ missing_fields: string[] }>
    expect(buffer.at(-1)?.missing_fields).toContain('emotion')
  })

  it('要素齐全时不上报埋点', () => {
    mount(ContextCard, { props: { context: completeContext(), sessionId: 's1' } })
    expect(window.sessionStorage.getItem('wankeai.telemetry.context')).toBeNull()
  })

  it('loading 时展示骨架屏', () => {
    const wrapper = mount(ContextCard, {
      props: { context: null, sessionId: 's1', loading: true },
    })
    expect(wrapper.find('.el-skeleton').exists()).toBe(true)
    expect(wrapper.find('[data-testid="context-guard-error"]').exists()).toBe(false)
  })

  it('加载失败时展示错误态与重试按钮', async () => {
    const wrapper = mount(ContextCard, {
      props: { context: null, sessionId: 's1', error: '网络异常' },
    })
    expect(wrapper.text()).toContain('上下文加载失败')
    expect(wrapper.text()).toContain('网络异常')
    await wrapper.find('.context-card__error button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('未满足诉求有值时以警示形式展示', () => {
    const wrapper = mount(ContextCard, {
      props: { context: completeContext(), sessionId: 's1' },
    })
    expect(wrapper.text()).toContain('希望今天内发货')
  })

  it('等待时长格式化为分+秒', () => {
    const wrapper = mount(ContextCard, {
      props: { context: completeContext(), sessionId: 's1' },
    })
    expect(wrapper.text()).toContain('1 分 35 秒')
  })

  it('工单号存在时展示工单号', () => {
    const wrapper = mount(ContextCard, {
      props: { context: completeContext(), sessionId: 's1' },
    })
    expect(wrapper.text()).toContain('TK-20260115-001')
  })

  it('报错态同样提供重试按钮', async () => {
    const wrapper = mount(ContextCard, { props: { context: null, sessionId: 's1' } })
    await wrapper.find('[data-testid="context-guard-error"] button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('无 sessionId 时不上报埋点（会话未确定，上报无意义）', () => {
    mount(ContextCard, { props: { context: completeContext(), sessionId: '' } })
    expect(window.sessionStorage.getItem('wankeai.telemetry.context')).toBeNull()
  })

  it('附件图片不带可点击链接（HC-1）', () => {
    const wrapper = mount(ContextCard, {
      props: { context: completeContext(), sessionId: 's1' },
    })
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.find('img').attributes('src')).toBe('/mock-assets/a.jpg')
  })

  it('无附件时不渲染关联素材区', () => {
    const context = completeContext()
    context.attachments = []
    const wrapper = mount(ContextCard, { props: { context, sessionId: 's1' } })
    expect(wrapper.text()).not.toContain('关联素材')
  })

  it('attachments 为空数组也算合法（不触发七要素守卫）', () => {
    const context = completeContext()
    context.attachments = []
    const wrapper = mount(ContextCard, { props: { context, sessionId: 's1' } })
    expect(wrapper.find('[data-testid="context-guard-error"]').exists()).toBe(false)
  })
})
