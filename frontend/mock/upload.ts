/**
 * 上传文件解析 —— FE-06 批量导入（CSV / XLSX / MD）。
 *
 * 为什么自己解析 multipart：
 * Vite 的 Connect 中间件把请求体交给我们时是裸 Buffer，引入 busboy / formidable
 * 只为 Mock 的预览功能不划算；multipart 的边界格式非常简单（RFC 7578），
 * 这里用最小实现即可，同时避免多一个运行时依赖。
 */
import { readXlsxFirstSheet } from './xlsx'
import type { KbType } from '../src/types/contract'

export interface MultipartPart {
  name: string
  filename: string | null
  contentType: string | null
  data: Buffer
}

/** 解析 multipart/form-data 请求体 */
export function parseMultipart(body: Buffer, contentType: string): MultipartPart[] {
  const match = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType)
  const boundary = (match?.[1] ?? match?.[2] ?? '').trim()
  if (!boundary) return []

  const delimiter = Buffer.from(`--${boundary}`)
  const parts: MultipartPart[] = []
  let cursor = body.indexOf(delimiter)

  while (cursor !== -1) {
    const afterDelimiter = cursor + delimiter.length
    // 结束边界：--boundary--
    if (body[afterDelimiter] === 0x2d && body[afterDelimiter + 1] === 0x2d) break

    // 跳过边界后的 CRLF
    let head = afterDelimiter
    while (head < body.length && (body[head] === 0x0d || body[head] === 0x0a)) head += 1

    const headerEnd = body.indexOf('\r\n\r\n', head)
    if (headerEnd === -1) break

    const headerText = body.slice(head, headerEnd).toString('utf-8')
    const nextDelimiter = body.indexOf(delimiter, headerEnd)
    // 数据段尾部有一个 CRLF 属于分隔符，不属于内容
    const dataEnd = nextDelimiter === -1 ? body.length : Math.max(headerEnd + 4, nextDelimiter - 2)

    parts.push({
      name: readHeaderParam(headerText, 'name') ?? '',
      filename: readHeaderParam(headerText, 'filename'),
      contentType: /content-type:\s*([^\r\n;]+)/i.exec(headerText)?.[1]?.trim() ?? null,
      data: body.slice(headerEnd + 4, dataEnd),
    })

    cursor = nextDelimiter
  }

  return parts
}

function readHeaderParam(headerText: string, key: string): string | null {
  const pattern = new RegExp(`${key}="([^"]*)"`, 'i')
  return pattern.exec(headerText)?.[1] ?? null
}

/** 按扩展名解码上传内容；XLSX 走 ZIP 解析并转成「表头：值」文本 */
export function readUploadedText(part: MultipartPart): { text: string; rows: string[][] | null } {
  const name = (part.filename ?? '').toLowerCase()
  if (name.endsWith('.xlsx') || name.endsWith('.xls')) {
    const rows = readXlsxFirstSheet(part.data)
    return { text: rowsToText(rows), rows }
  }
  return { text: decodeText(part.data), rows: null }
}

/** UTF-8 优先，出现替换字符说明不是 UTF-8，退回 GBK（商家导出 Excel 常见编码） */
export function decodeText(buf: Buffer): string {
  const utf8 = buf.toString('utf-8')
  if (!utf8.includes('\uFFFD')) return utf8
  try {
    return new TextDecoder('gbk').decode(buf)
  } catch {
    return utf8
  }
}

/** 把二维表转成「表头：值；表头：值」的段落文本，便于复用同一套切分逻辑 */
export function rowsToText(rows: string[][]): string {
  if (rows.length === 0) return ''
  const header = rows[0]!.map((cell, index) => (cell?.trim() ? cell.trim() : `第${index + 1}列`))
  const lines: string[] = []
  for (const row of rows.slice(1)) {
    const filled = row.map((cell) => (cell ?? '').trim())
    if (filled.every((cell) => !cell)) continue
    const segments = filled
      .map((cell, index) => (cell ? `${header[index]}：${cell}` : ''))
      .filter(Boolean)
    if (segments.length > 0) lines.push(segments.join('；'))
  }
  return lines.join('\n\n')
}

/** 解析 CSV（支持双引号包裹与转义的双引号） */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let inQuotes = false

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]!
    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        field += char
      }
      continue
    }
    if (char === '"') {
      inQuotes = true
    } else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (char !== '\r') {
      field += char
    }
  }
  if (field || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows.filter((item) => item.some((cell) => cell.trim()))
}

// ─────────────────────────────────────────────────────────────
// 知识类型自动识别（FE-06 DoD：支持 4 种知识类型自动识别）
// ─────────────────────────────────────────────────────────────

const TYPE_HINTS: Record<KbType, string[]> = {
  FAQ: ['标准问', '相似问', '问题', '答案', 'question', 'answer', 'faq', '怎么', '吗？', '呢？'],
  POLICY: ['政策', '规则', '条款', '售后', '退换', '运费', '时效', '优惠券', '授权', '合规'],
  PRODUCT: ['sku', '商品', '材质', '填充物', '尺寸', '面料', '洗护', '库存', '价格', '卖点'],
  LONG_DOC: ['指南', '说明', '手册', '流程', '文档', '规范', '第一章', '##'],
}

export interface TypeDetection {
  detected_type: KbType
  candidates: Array<{ kb_type: KbType; confidence: number }>
  warnings: string[]
}

export function detectKbType(rows: string[][] | null, text: string, fileName: string): TypeDetection {
  const sample = `${rows?.[0]?.join(' ') ?? ''} ${text.slice(0, 2000)} ${fileName}`.toLowerCase()
  const scores: Record<KbType, number> = { FAQ: 0, POLICY: 0, PRODUCT: 0, LONG_DOC: 0 }

  for (const [type, hints] of Object.entries(TYPE_HINTS) as Array<[KbType, string[]]>) {
    for (const hint of hints) {
      let from = 0
      // 每个提示词最多计 3 次，避免长文档里同一个词刷分
      for (let hit = 0; hit < 3; hit += 1) {
        const at = sample.indexOf(hint.toLowerCase(), from)
        if (at === -1) break
        scores[type] += 1
        from = at + hint.length
      }
    }
  }

  // 结构先验：列数少且含问句形态 -> FAQ；平均单元很长 -> 长文档
  const columnCount = rows?.[0]?.length ?? 0
  const avgLength = text.length / Math.max(1, rows ? rows.length : text.split(/\n{2,}/).length)
  if (columnCount > 0 && columnCount <= 4 && /[?？]/.test(text.slice(0, 3000))) scores.FAQ += 3
  if (avgLength > 400) scores.LONG_DOC += 3
  if (columnCount >= 8) scores.PRODUCT += 2
  if (fileName.toLowerCase().endsWith('.md')) scores.LONG_DOC += 2

  const total = Object.values(scores).reduce((sum, value) => sum + value, 0) || 1
  const candidates = (Object.entries(scores) as Array<[KbType, number]>)
    .map(([kb_type, score]) => ({
      kb_type,
      // 保留两位小数，且给每个类型一个下限，避免出现 0.00 的噪声项
      confidence: Number(Math.max(0.02, score / total).toFixed(2)),
    }))
    .sort((a, b) => b.confidence - a.confidence)

  const detected = candidates[0]!.kb_type
  const warnings: string[] = []
  if (candidates[0]!.confidence < 0.4) {
    warnings.push('知识类型自动识别置信度偏低，请人工确认后再导入')
  }
  if (!rows && text.length === 0) {
    warnings.push('文件内容为空，无法切分')
  }
  if (candidates[0]!.confidence - (candidates[1]?.confidence ?? 0) < 0.1) {
    warnings.push('存在多个相近的知识类型候选，建议核对切分长度区间')
  }

  return { detected_type: detected, candidates, warnings }
}
