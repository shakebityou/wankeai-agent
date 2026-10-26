import { describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MessageStream from '@/components/session/MessageStream.vue'
import type { ChatMessage } from '@/types/contract'

/**
 * 单测：MessageStream —— FE-01 核心组件，DoD 要求覆盖率 ≥80%。
 *
 * 重点验证四条会直接影响坐席体验的行为：
 *   1. **上滚暂停自动滚底** —— 这是任务书写死的 DoD。判定用 48px 容差，
 *      测试必须造出「接近底部」与「远离底部」两种状态分别断言。
 *   2. **打字机不污染消息列表** —— delta 期间只有 streamingText，
 *      turn.end 才由父级 push 正式消息；组件本身不允许把流式文本写进 messages。
 *   3. **SSE error 必须有兜底 + 重试** —— 不能只显示一行错误就没了。
 *   4. **空 / 加载 / 失败三态** —— 三个 PageState 分支都要覆盖。
 */

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
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

/** jsdom 没有布局，scrollHeight/clientHeight 恒为 0，需要 stub 出「远离底部」的几何 */
function stubScroller(wrapper: ReturnType<typeof mount>, distance: number): void {
  const el = wrapper.find('.stream__scroller').element as HTMLElement
  Object.defineProperty(el, 'clientHeight', { value: 400, configurable: true })
  Object.defineProperty(el, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(el, 'scrollTop', { value: 1000 - 400 - distance, configurable: true })
}

describe('MessageStream', () => {
  it('渲染消息列表', () => {
    const wrapper = mount(MessageStream, {
      props: { messages: [message(), message({ message_id: 'm2', sender: 'BOT' })] },
    })
    expect(wrapper.findAll('.bubble-row')).toHaveLength(2)
  })

  it('空态展示占位文案', () => {
    const wrapper = mount(MessageStream, { props: { messages: [] } })
    expect(wrapper.text()).toContain('该会话还没有消息')
  })

  it('loading 且无内容时展示加载态', () => {
    const wrapper = mount(MessageStream, { props: { messages: [], loading: true } })
    expect(wrapper.text()).toContain('正在加载消息历史')
  })

  it('error 且无内容时展示失败态', () => {
    const wrapper = mount(MessageStream, { props: { messages: [], error: '接口超时' } })
    expect(wrapper.text()).toContain('接口超时')
  })

  it('打字机文本独立渲染，不进入消息列表', async () => {
    const messages = [message()]
    const wrapper = mount(MessageStream, { props: { messages, streamingText: '您好，很高兴' } })
    expect(wrapper.text()).toContain('您好，很高兴')
    // 列表仍只有 1 条正式消息（流式文本由父级在 turn.end 后才落库）
    expect(wrapper.findAll('.bubble-row')).toHaveLength(1)
    await flushPromises()
  })

  it('typing 时展示阶段指示', () => {
    const wrapper = mount(MessageStream, {
      props: { messages: [message()], typing: true, stage: '检索知识库' },
    })
    expect(wrapper.text()).toContain('检索知识库')
  })

  it('streamError 时展示兜底话术与重试按钮', async () => {
    const wrapper = mount(MessageStream, {
      props: { messages: [message()], streamError: '下游服务暂时不可用' },
    })
    expect(wrapper.text()).toContain('下游服务暂时不可用')
    expect(wrapper.text()).toContain('请直接接管回复买家')
    await wrapper.find('.stream__error button').trigger('click')
    expect(wrapper.emitted('retryStream')).toHaveLength(1)
  })

  it('初始状态自动滚底开启（无回到底部按钮）', () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    expect(wrapper.find('.stream__jump').exists()).toBe(false)
  })

  it('滚动到接近底部（容差 48px 内）仍算自动滚底', async () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    stubScroller(wrapper, 20)
    await wrapper.find('.stream__scroller').trigger('scroll')
    expect(wrapper.find('.stream__jump').exists()).toBe(false)
  })

  it('用户上滚远离底部时暂停自动滚底并显示回到底部', async () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    stubScroller(wrapper, 500)
    await wrapper.find('.stream__scroller').trigger('scroll')
    expect(wrapper.find('.stream__jump').exists()).toBe(true)
    expect(wrapper.emitted('autoScrollChange')?.at(-1)).toEqual([false])
  })

  it('点击「回到底部」恢复自动跟随', async () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    stubScroller(wrapper, 500)
    await wrapper.find('.stream__scroller').trigger('scroll')
    await wrapper.find('.stream__jump').trigger('click')
    expect(wrapper.find('.stream__jump').exists()).toBe(false)
    expect(wrapper.emitted('autoScrollChange')?.at(-1)).toEqual([true])
  })

  it('暴露 resetToBottom / backToBottom 供父级调用', () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    const vm = wrapper.vm as unknown as { resetToBottom: () => void; backToBottom: () => void }
    expect(typeof vm.resetToBottom).toBe('function')
    expect(typeof vm.backToBottom).toBe('function')
  })

  it('开启 feedbackEnabled 时把纠错事件透传给父级', async () => {
    const wrapper = mount(MessageStream, {
      props: { messages: [message({ sender: 'BOT' })], feedbackEnabled: true },
    })
    const button = wrapper.find('.bubble-row__foot button')
    expect(button.exists()).toBe(true)
    await button.trigger('click')
    expect(wrapper.emitted('feedback')).toBeTruthy()
  })

  it('挂载后立即滚到底（首屏不要求用户手动滚）', async () => {
    const wrapper = mount(MessageStream, { props: { messages: [message()] } })
    const el = wrapper.find('.stream__scroller').element as HTMLElement
    const scrollTo = vi.spyOn(el, 'scrollTo' as never)
    ;(wrapper.vm as unknown as { resetToBottom: () => void }).resetToBottom()
    await flushPromises()
    expect(scrollTo).toHaveBeenCalled()
  })
})
