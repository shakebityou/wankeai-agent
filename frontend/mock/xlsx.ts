/**
 * 极简 XLSX 文本读取器（仅用于 Mock 的 FE-06 导入预览）。
 *
 * 为什么自己写：
 *   任务书 FE-06 要求支持 CSV / XLSX / MD 三种上传，而 xlsx 本质是一个 ZIP
 *   容器（xl/sharedStrings.xml + xl/worksheets/sheetN.xml）。为了一个 Mock
 *   预览功能引入 exceljs / sheetjs 这类重量级依赖不划算，Node 自带的
 *   zlib.inflateRawSync 足以解出条目内容。
 *
 * 局限（够用即可，不是通用 Excel 库）：
 *   - 只读第一个 worksheet；
 *   - 只取单元格文本值（共享字符串 + 内联字符串 + 数字），忽略样式、公式、合并单元格；
 *   - 支持 store 模式（method 0 未压缩 / method 8 deflate），ZIP64 未处理。
 */

import { inflateRawSync } from 'node:zlib'

const EOCD_SIG = 0x06054b50
const CENTRAL_SIG = 0x02014b50
const LOCAL_SIG = 0x04034b50

interface ZipEntry {
  name: string
  method: number
  compressedSize: number
  localOffset: number
}

/** 从 ZIP 字节流读出中央目录 */
function readCentralDirectory(buf: Buffer): ZipEntry[] {
  // 从尾部向前找 EOCD（注释最长 65535 字节）
  let eocd = -1
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 65535); i -= 1) {
    if (buf.readUInt32LE(i) === EOCD_SIG) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new Error('不是有效的 XLSX（未找到 ZIP 中央目录）')

  const count = buf.readUInt16LE(eocd + 10)
  let offset = buf.readUInt32LE(eocd + 16)
  const entries: ZipEntry[] = []

  for (let i = 0; i < count; i += 1) {
    if (offset + 46 > buf.length || buf.readUInt32LE(offset) !== CENTRAL_SIG) break
    const method = buf.readUInt16LE(offset + 10)
    const compressedSize = buf.readUInt32LE(offset + 20)
    const nameLen = buf.readUInt16LE(offset + 28)
    const extraLen = buf.readUInt16LE(offset + 30)
    const commentLen = buf.readUInt16LE(offset + 32)
    const localOffset = buf.readUInt32LE(offset + 42)
    const name = buf.toString('utf-8', offset + 46, offset + 46 + nameLen)
    entries.push({ name, method, compressedSize, localOffset })
    offset += 46 + nameLen + extraLen + commentLen
  }
  return entries
}

/** 取出某个条目的解压后内容 */
function readEntry(buf: Buffer, entry: ZipEntry): Buffer {
  const start = entry.localOffset
  if (buf.readUInt32LE(start) !== LOCAL_SIG) throw new Error(`ZIP 局部头损坏：${entry.name}`)
  const nameLen = buf.readUInt16LE(start + 26)
  const extraLen = buf.readUInt16LE(start + 28)
  const dataStart = start + 30 + nameLen + extraLen
  const raw = buf.subarray(dataStart, dataStart + entry.compressedSize)
  if (entry.method === 0) return Buffer.from(raw)
  if (entry.method === 8) return inflateRawSync(raw)
  throw new Error(`不支持的压缩方式 ${entry.method}（${entry.name}）`)
}

function decodeXmlEntities(text: string): string {
  return text
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, code: string) => String.fromCharCode(Number(code)))
    .replace(/&amp;/g, '&')
}

/** 解析 sharedStrings.xml，按出现顺序返回字符串表 */
function parseSharedStrings(xml: string): string[] {
  const strings: string[] = []
  const siRegex = /<si\b[^>]*>([\s\S]*?)<\/si>/g
  let match: RegExpExecArray | null
  while ((match = siRegex.exec(xml)) !== null) {
    const inner = match[1] ?? ''
    // 一个 si 内可能有多个 <t>（富文本分段），需要拼接
    const parts: string[] = []
    const tRegex = /<t\b[^>]*>([\s\S]*?)<\/t>/g
    let tMatch: RegExpExecArray | null
    while ((tMatch = tRegex.exec(inner)) !== null) parts.push(decodeXmlEntities(tMatch[1] ?? ''))
    strings.push(parts.join(''))
  }
  return strings
}

/** 列引用（A1 / BC12）转 0 基列号 */
function columnIndex(ref: string): number {
  const letters = ref.replace(/\d+/g, '').toUpperCase()
  let index = 0
  for (const char of letters) index = index * 26 + (char.charCodeAt(0) - 64)
  return Math.max(0, index - 1)
}

/**
 * 读取 XLSX 的第一个工作表，返回二维字符串数组（含表头行）。
 * 返回值与 CSV 解析结果同构，便于上游统一处理。
 */
export function readXlsxFirstSheet(buf: Buffer): string[][] {
  const entries = readCentralDirectory(buf)
  const find = (name: string): ZipEntry | undefined => entries.find((item) => item.name === name)

  const sharedEntry = find('xl/sharedStrings.xml')
  const shared = sharedEntry ? parseSharedStrings(readEntry(buf, sharedEntry).toString('utf-8')) : []

  // 第一个 worksheet：优先 sheet1.xml，否则按名称排序取第一个
  const sheetEntry =
    find('xl/worksheets/sheet1.xml') ??
    entries
      .filter((item) => item.name.startsWith('xl/worksheets/') && item.name.endsWith('.xml'))
      .sort((a, b) => a.name.localeCompare(b.name))[0]
  if (!sheetEntry) throw new Error('XLSX 中未找到工作表')

  const sheetXml = readEntry(buf, sheetEntry).toString('utf-8')
  const rows: string[][] = []
  const rowRegex = /<row\b[^>]*>([\s\S]*?)<\/row>/g
  let rowMatch: RegExpExecArray | null

  while ((rowMatch = rowRegex.exec(sheetXml)) !== null) {
    const cells: string[] = []
    const cellRegex = /<c\b([^>]*)(?:\/>|>([\s\S]*?)<\/c>)/g
    let cellMatch: RegExpExecArray | null

    while ((cellMatch = cellRegex.exec(rowMatch[1] ?? '')) !== null) {
      const attrs = cellMatch[1] ?? ''
      const inner = cellMatch[2] ?? ''
      const refMatch = attrs.match(/\br="([A-Z]+\d+)"/)
      const typeMatch = attrs.match(/\bt="([^"]+)"/)
      const type = typeMatch?.[1] ?? 'n'

      let value = ''
      if (type === 's') {
        const vMatch = inner.match(/<v>([\s\S]*?)<\/v>/)
        const index = vMatch ? Number(vMatch[1]) : -1
        value = shared[index] ?? ''
      } else if (type === 'inlineStr') {
        const parts: string[] = []
        const tRegex = /<t\b[^>]*>([\s\S]*?)<\/t>/g
        let tMatch: RegExpExecArray | null
        while ((tMatch = tRegex.exec(inner)) !== null) parts.push(decodeXmlEntities(tMatch[1] ?? ''))
        value = parts.join('')
      } else {
        const vMatch = inner.match(/<v>([\s\S]*?)<\/v>/)
        value = vMatch ? decodeXmlEntities(vMatch[1] ?? '') : ''
      }

      const col = refMatch ? columnIndex(refMatch[1]!) : cells.length
      // 补齐中间空缺列，避免错位
      while (cells.length < col) cells.push('')
      cells[col] = value.trim()
    }

    // 全空行跳过
    if (cells.some((cell) => cell !== '')) rows.push(cells)
  }

  return rows
}
