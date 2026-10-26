import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { ElSelect } from 'element-plus'
import KbEditor from '@/components/kb/KbEditor.vue'
import type { KbChunk } from '@/types/contract'

/**
 * 单测：KbEditor —— FE-05 核心组件，DoD 要求覆盖率 ≥80%。
 *
 * 重点覆盖四条会影响数据质量的行为：
 *   1. **字数区间**：按 kb_type 切区间（商品 200–400 / FAQ 50–200…），超长过短都要提示；
 *      口径是去空白字符，与后端 char_count 一致。
 *   2. **必填校验**：authority 缺失时不能提交（溯源断了这条知识就不能进向量库）。
 *   3. **预览去外链**：HC-1 —— 预览区 v-html 也必须把裸 URL 替换掉，且 HTML 被转义。
 *   4. **编辑已有条目时回填**，包括生效区间与 SKU 列表。
 */

function chunk(overrides: Partial<KbChunk> = {}): KbChunk {
  return {
    chunk_id: 'KB-001',
    title: '奶油小熊 20cm 洗护说明',
    content: '使用中性洗涤剂手洗，阴凉处平铺晾干，不可烘干。',
    kb_type: 'PRODUCT',
    category: '洗护保养',
    sku_ids: ['WK-BEAR-20'],
    authority: '商品主数据表·洗护方式字段',
    review_status: 'APPROVED',
    channel: 'ALL',
    effective_from: '2026-01-01',
    effective_to: null,
    updated_by: '李敏',
    updated_at: '2026-01-15T10:00:00Z',
    char_count: 28,
    version: 3,
    history: [],
    ...overrides,
  } as KbChunk
}

describe('KbEditor', () => {
  it('空表单禁用提交并列出全部必填项', () => {
    const wrapper = mount(KbEditor, { props: {} })
    expect(wrapper.text()).toContain('以下字段必填')
    expect(wrapper.text()).toContain('标题')
    expect(wrapper.text()).toContain('正文')
    expect(wrapper.text()).toContain('分类')
    expect(wrapper.text()).toContain('权威来源')
    const submit = wrapper.findAll('button').find((b) => b.text().includes('创建条目'))
    expect(submit?.attributes('disabled')).toBeDefined()
  })

  it('编辑已有条目时回填标题与正文', () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    expect(wrapper.find('input').element.value).toBe('奶油小熊 20cm 洗护说明')
    expect(wrapper.find('textarea').element.value).toContain('中性洗涤剂手洗')
  })

  it('编辑模式按钮文案为「保存修改」', () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    expect(wrapper.text()).toContain('保存修改')
  })

  it('字数统计去掉空白字符（与后端 char_count 口径一致）', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('一二三\n四  五')
    expect(wrapper.text()).toContain('5 字')
  })

  it('PRODUCT 类型按 200–400 区间提示', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('短')
    expect(wrapper.text()).toContain('不足 200 字')
  })

  it('超出区间上限时提示会被切分', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('长'.repeat(401))
    expect(wrapper.text()).toContain('超过 400 字')
    expect(wrapper.text()).toContain('入库时会被切分')
  })

  it('字数落在区间内时给出正常提示', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('护'.repeat(250))
    expect(wrapper.text()).toContain('字数在目标区间 200–400 字内')
  })

  it('切换知识类型后字数区间随之变化（FAQ 50–200）', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    // el-select 的下拉用 teleport + popper，jsdom 下点不开；
    // 但模板里 v-model 绑的是 update:modelValue，emit 它才等价于真实选中。
    await wrapper.findAllComponents(ElSelect).at(0)?.vm.$emit('update:modelValue', 'FAQ')
    await new Promise((r) => setTimeout(r, 50))
    expect(wrapper.text()).toContain('目标 50–200')
  })

  it('authority 缺失时不能提交（溯源断了这条知识就不能进向量库）', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk({ authority: '' }) } })
    const submit = wrapper.findAll('button').find((b) => b.text().includes('保存修改'))
    expect(submit?.attributes('disabled')).toBeDefined()
  })

  it('必填齐全后提交 emit 完整 payload', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    const submit = wrapper.findAll('button').find((b) => b.text().includes('保存修改'))
    await submit?.trigger('click')
    const payload = wrapper.emitted('submit')?.[0]?.[0] as Record<string, unknown>
    expect(payload.title).toBe('奶油小熊 20cm 洗护说明')
    expect(payload.authority).toBe('商品主数据表·洗护方式字段')
    expect(payload.sku_ids).toEqual(['WK-BEAR-20'])
    expect(payload.channel).toBe('ALL')
  })

  it('预览渲染 Markdown 标题与加粗', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('# 标题一\n\n**加粗内容**')
    const preview = wrapper.find('.preview__body')
    expect(preview.find('h2').text()).toBe('标题一')
    expect(preview.find('strong').text()).toBe('加粗内容')
  })

  it('预览把裸 URL 替换为 [链接已屏蔽]（HC-1）', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('详情见 https://item.taobao.com/12345')
    const preview = wrapper.find('.preview__body')
    expect(preview.text()).toContain('[链接已屏蔽]')
    expect(preview.text()).not.toContain('item.taobao.com')
    expect(preview.find('a').exists()).toBe(false)
  })

  it('预览转义 HTML 标签（v-html 也不能注入链接）', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.find('textarea').setValue('<a href="https://x.com">点我</a>')
    const preview = wrapper.find('.preview__body')
    expect(preview.find('a').exists()).toBe(false)
    expect(preview.text()).toContain('<a href=')
  })

  it('正文为空时预览显示占位', () => {
    const wrapper = mount(KbEditor, { props: {} })
    expect(wrapper.text()).toContain('尚未填写正文')
  })

  it('submitting 时提交按钮进入 loading', () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk(), submitting: true } })
    expect(wrapper.find('.el-button--primary').classes()).toContain('is-loading')
  })

  it('编辑带生效区间的条目时回填日期区间', () => {
    const wrapper = mount(KbEditor, {
      props: { chunk: chunk({ effective_from: '2026-01-01', effective_to: '2026-12-31' }) },
    })
    // el-date-picker 的区间值以两个 input 呈现
    const inputs = wrapper.findAll('.kb-editor__pane--side input')
    const dateInputs = inputs.filter((i) => i.attributes('placeholder')?.includes('日期'))
    expect((dateInputs.at(0)?.element as HTMLInputElement).value).toBe('2026-01-01')
    expect((dateInputs.at(1)?.element as HTMLInputElement).value).toBe('2026-12-31')
  })

  it('编辑无生效区间的条目时日期为空', () => {
    const wrapper = mount(KbEditor, {
      props: { chunk: chunk({ effective_from: null, effective_to: null }) },
    })
    const inputs = wrapper.findAll('.kb-editor__pane--side input')
    const dateInputs = inputs.filter((i) => i.attributes('placeholder')?.includes('日期'))
    expect((dateInputs.at(0)?.element as HTMLInputElement).value).toBe('')
    expect((dateInputs.at(1)?.element as HTMLInputElement).value).toBe('')
  })

  it('取消 emit cancel', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    const cancel = wrapper.findAll('button').find((b) => b.text().includes('取消'))
    await cancel?.trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('SKU 输入框支持顿号分隔', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk({ sku_ids: [] }) } })
    // 右侧元数据区第 5 个 .field 是关联 SKU
    const fields = wrapper.findAll('.kb-editor__pane--side .field')
    const skuInput = fields.at(4)?.find('input')
    expect(skuInput?.exists()).toBe(true)
    await skuInput?.setValue('WK-BEAR-20、WK-CAT-30')
    await wrapper.findAll('button').find((b) => b.text().includes('保存修改'))?.trigger('click')
    const payload = wrapper.emitted('submit')?.[0]?.[0] as Record<string, unknown>
    expect(payload.sku_ids).toEqual(['WK-BEAR-20', 'WK-CAT-30'])
  })

  it('分类与权威来源可直接录入并进入 payload', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk({ category: '', authority: '' }) } })
    const fields = wrapper.findAll('.kb-editor__pane--side .field')
    await fields.at(1)?.find('input').setValue('退换货政策')
    await fields.at(2)?.find('input').setValue('售后政策 4.2 条')
    await wrapper.findAll('button').find((b) => b.text().includes('保存修改'))?.trigger('click')
    const payload = wrapper.emitted('submit')?.[0]?.[0] as Record<string, unknown>
    expect(payload.category).toBe('退换货政策')
    expect(payload.authority).toBe('售后政策 4.2 条')
  })

  it('适用渠道可切换为指定渠道', async () => {
    const wrapper = mount(KbEditor, { props: { chunk: chunk() } })
    await wrapper.findAllComponents(ElSelect).at(1)?.vm.$emit('update:modelValue', 'DOUYIN')
    await new Promise((r) => setTimeout(r, 50))
    await wrapper.findAll('button').find((b) => b.text().includes('保存修改'))?.trigger('click')
    const payload = wrapper.emitted('submit')?.[0]?.[0] as Record<string, unknown>
    expect(payload.channel).toBe('DOUYIN')
  })
})
