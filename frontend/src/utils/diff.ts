/**
 * 行级差异对比 —— FE-07（版本与生效时间管理）。
 *
 * 任务书 DoD：「差异对比新增/删除/修改三色标注」
 *
 * 为什么不引 diff 库：
 *   知识条目的正文是几十到几百行的 Markdown，LCS 动态规划在这个规模下完全够用
 *   （400 行 × 400 行的 DP 表 = 16 万次操作，毫秒级），
 *   而引一个 diff 包要多一份依赖与一次体积增长，收益不成比例。
 *
 * 算法：经典 LCS（最长公共子序列）→ 回溯生成编辑脚本 → 合并相邻的删+增为「修改」。
 * 「修改」不是 LCS 的原始输出（原始输出只有插入与删除），
 * 但运营看版本对比时关心的是「这一句被改了」，所以相邻的 删除+新增 会被合并成 modify。
 */

export type DiffKind = 'equal' | 'add' | 'remove' | 'modify'

export interface DiffLine {
  kind: DiffKind
  /** 旧版本行号（1 基），新增行为 null */
  oldLine: number | null
  /** 新版本行号（1 基），删除行为 null */
  newLine: number | null
  /** 旧文本（新增行为空串） */
  oldText: string
  /** 新文本（删除行为空串） */
  newText: string
}

export interface DiffSummary {
  added: number
  removed: number
  modified: number
}

function splitLines(text: string): string[] {
  return text.replace(/\r\n/g, '\n').split('\n')
}

/**
 * 计算两个文本的行级差异。
 *
 * @param oldText 旧版本正文
 * @param newText 新版本正文
 * @param mergeModified 是否把相邻的「删除 + 新增」合并为「修改」（默认 true）
 */
export function diffLines(oldText: string, newText: string, mergeModified = true): DiffLine[] {
  const oldLines = splitLines(oldText)
  const newLines = splitLines(newText)

  // LCS 长度表
  const m = oldLines.length
  const n = newLines.length
  const table: number[][] = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0))

  for (let i = m - 1; i >= 0; i -= 1) {
    for (let j = n - 1; j >= 0; j -= 1) {
      table[i][j] =
        oldLines[i] === newLines[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }

  // 回溯生成原始编辑脚本
  const raw: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (oldLines[i] === newLines[j]) {
      raw.push({ kind: 'equal', oldLine: i + 1, newLine: j + 1, oldText: oldLines[i], newText: newLines[j] })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      raw.push({ kind: 'remove', oldLine: i + 1, newLine: null, oldText: oldLines[i], newText: '' })
      i += 1
    } else {
      raw.push({ kind: 'add', oldLine: null, newLine: j + 1, oldText: '', newText: newLines[j] })
      j += 1
    }
  }
  while (i < m) {
    raw.push({ kind: 'remove', oldLine: i + 1, newLine: null, oldText: oldLines[i], newText: '' })
    i += 1
  }
  while (j < n) {
    raw.push({ kind: 'add', oldLine: null, newLine: j + 1, oldText: '', newText: newLines[j] })
    j += 1
  }

  if (!mergeModified) return raw

  // 合并相邻的 remove+add 为 modify：先收集连续的非 equal 段，再一一配对
  const merged: DiffLine[] = []
  let index = 0
  while (index < raw.length) {
    const current = raw[index]
    if (current.kind === 'equal') {
      merged.push(current)
      index += 1
      continue
    }

    // 收集连续的非 equal 段
    const segment: DiffLine[] = []
    while (index < raw.length && raw[index].kind !== 'equal') {
      segment.push(raw[index])
      index += 1
    }

    const removes = segment.filter((line) => line.kind === 'remove')
    const adds = segment.filter((line) => line.kind === 'add')
    const paired = Math.min(removes.length, adds.length)

    // 配对的部分标为 modify，成对出现在同一行上，运营一眼能看出「这句被改了什么」
    for (let k = 0; k < paired; k += 1) {
      merged.push({
        kind: 'modify',
        oldLine: removes[k].oldLine,
        newLine: adds[k].newLine,
        oldText: removes[k].oldText,
        newText: adds[k].newText,
      })
    }
    for (let k = paired; k < removes.length; k += 1) merged.push(removes[k])
    for (let k = paired; k < adds.length; k += 1) merged.push(adds[k])
  }

  return merged
}

/** 差异统计 —— 用于在版本时间轴上直接显示「+3 / -1 / ~2」 */
export function summarizeDiff(lines: DiffLine[]): DiffSummary {
  return lines.reduce<DiffSummary>(
    (acc, line) => {
      if (line.kind === 'add') acc.added += 1
      else if (line.kind === 'remove') acc.removed += 1
      else if (line.kind === 'modify') acc.modified += 1
      return acc
    },
    { added: 0, removed: 0, modified: 0 },
  )
}
