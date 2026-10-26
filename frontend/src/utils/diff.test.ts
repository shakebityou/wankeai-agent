import { describe, expect, it } from 'vitest'
import { diffLines, summarizeDiff } from '@/utils/diff'

/**
 * 单测：行级差异 —— FE-07 DoD「差异对比新增/删除/修改三色标注」。
 *
 * 三色 diff 是这个功能的核心交付：运营要知道「这一版到底改了什么」。
 * 这里的关键不只是「能算出差异」，而是**相邻的 remove+add 必须合并成 modify**：
 * LCS 的原始输出只有插入与删除，若原样渲染，
 * 运营看到的是「删掉一整句 + 新增一整句」，而不是「这一句被改了」，
 * 在长句微调（改一个字）的场景下完全不可读。
 *
 * 注意：实际 DiffSummary 字段是 `added / removed / modified`（无 equal 统计）。
 */

describe('diffLines', () => {
  it('完全相同的文本只有 equal', () => {
    const lines = diffLines('a\nb\nc', 'a\nb\nc')
    expect(lines.every((line) => line.kind === 'equal')).toBe(true)
    expect(lines).toHaveLength(3)
  })

  it('纯新增标为 add', () => {
    const lines = diffLines('a\nc', 'a\nb\nc')
    const added = lines.filter((line) => line.kind === 'add')
    expect(added).toHaveLength(1)
    expect(added[0].newText).toBe('b')
  })

  it('纯删除标为 remove', () => {
    const lines = diffLines('a\nb\nc', 'a\nc')
    const removed = lines.filter((line) => line.kind === 'remove')
    expect(removed).toHaveLength(1)
    expect(removed[0].oldText).toBe('b')
  })

  it('相邻的删除+新增合并为 modify', () => {
    const lines = diffLines('价格是 49 元', '价格是 59 元')
    const kinds = lines.map((line) => line.kind)
    expect(kinds).toContain('modify')
    expect(kinds).not.toContain('remove')
    expect(kinds).not.toContain('add')
  })

  it('行数不变时的单行修改仍为 modify', () => {
    const lines = diffLines('第一行\n第二行\n第三行', '第一行\n第二行改了\n第三行')
    const modified = lines.filter((line) => line.kind === 'modify')
    expect(modified).toHaveLength(1)
    expect(modified[0].oldText).toBe('第二行')
    expect(modified[0].newText).toBe('第二行改了')
  })

  it('空旧文本对比有内容：一条 modify（空行→首行）+ 一条 add', () => {
    const lines = diffLines('', '第一行\n第二行')
    expect(lines.filter((line) => line.kind === 'modify')).toHaveLength(1)
    expect(lines.filter((line) => line.kind === 'add')).toHaveLength(1)
  })

  it('旧文本有内容、新文本为空：一条 modify + 一条 remove', () => {
    const lines = diffLines('第一行\n第二行', '')
    expect(lines.filter((line) => line.kind === 'modify')).toHaveLength(1)
    expect(lines.filter((line) => line.kind === 'remove')).toHaveLength(1)
  })

  it('mergeModified=false 时保留原始 remove/add', () => {
    const lines = diffLines('价格是 49 元', '价格是 59 元', false)
    const kinds = lines.map((line) => line.kind)
    expect(kinds).toContain('remove')
    expect(kinds).toContain('add')
    expect(kinds).not.toContain('modify')
  })
})

describe('summarizeDiff', () => {
  it('统计 add / remove / modify 数量', () => {
    // 旧：a / b / c / d；新：a / B / c / e
    // b→B 与 d→e 都是单行替换，均合并为 modify
    const lines = diffLines('a\nb\nc\nd', 'a\nB\nc\ne')
    const summary = summarizeDiff(lines)
    expect(summary.modified).toBe(2)
    expect(summary.added).toBe(0)
    expect(summary.removed).toBe(0)
  })

  it('中间插入一行时统计出 1 条 add', () => {
    const summary = summarizeDiff(diffLines('a\nc', 'a\nb\nc'))
    expect(summary.added).toBe(1)
    expect(summary.modified).toBe(0)
  })

  it('无变更时全部计数为 0', () => {
    const summary = summarizeDiff(diffLines('a', 'a'))
    expect(summary.added + summary.removed + summary.modified).toBe(0)
  })
})
