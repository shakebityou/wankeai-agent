/**
 * 展示层格式化与脱敏 —— 任务书 4.5.2「脱敏展示」的统一出口。
 *
 * 重要口径（HC-2）：真正的脱敏由后端完成，前端这里只做**兜底**：
 * 若上游漏传明文，前端也必须把它掩掉，绝不让原始手机号/姓名渲染到界面上。
 * 因此这些函数是「宁可多掩一次」的策略，不是唯一的脱敏点。
 */

/** 手机号：138****8888 */
export function maskPhone(input: string | null | undefined): string {
  if (!input) return ''
  return input.replace(/\b(1[3-9]\d)\d{4}(\d{4})\b/g, '$1****$2')
}

/** 姓名：张**（保留姓，其余掩掉；已脱敏形态原样返回） */
export function maskName(input: string | null | undefined): string {
  if (!input) return ''
  const trimmed = input.trim()
  if (trimmed.includes('*')) return trimmed
  const chars = [...trimmed]
  if (chars.length <= 1) return trimmed
  // 复姓（欧阳/司马…）保留两字，其余保留一字
  const keep = /^(欧阳|司马|上官|诸葛|东方|独孤|南宫|慕容)/.test(trimmed) ? 2 : 1
  return `${chars.slice(0, keep).join('')}${'*'.repeat(Math.max(1, chars.length - keep))}`
}

/** 订单号 / 运单号：前 4 后 4，中间掩掉 */
export function maskOrderNo(input: string | null | undefined): string {
  if (!input) return ''
  const trimmed = input.trim()
  if (trimmed.includes('*')) return trimmed
  if (trimmed.length <= 8) return trimmed
  return `${trimmed.slice(0, 4)}****${trimmed.slice(-4)}`
}

/**
 * 地址：只保留省市级，后面的详细地址一律截断。
 * 与 Mock 阶段的口径一致（HC-2：地址最小必要）。
 */
export function maskAddress(input: string | null | undefined): string {
  if (!input) return ''
  const trimmed = input.trim()
  const match = /^(.*?(?:省|自治区|特别行政区))?(.*?(?:市|自治州|地区|盟))/.exec(trimmed)
  if (match && match[0]) return match[0]
  return trimmed.split(/[\s,，]/)[0] ?? trimmed
}

/** 买家展示名（形如「张**（138****8888）」）整体再过一遍掩码 */
export function maskBuyerDisplay(input: string | null | undefined): string {
  if (!input) return ''
  return maskPhone(maskName(input))
}

/**
 * 从任意文本里抹掉明文手机号与身份证号。
 * 用于气泡正文、工单摘要等自由文本字段的兜底。
 */
export function scrubText(input: string | null | undefined): string {
  if (!input) return ''
  return input
    .replace(/\b(1[3-9]\d)\d{4}(\d{4})\b/g, '$1****$2')
    .replace(/\b(\d{6})\d{8}(\d{4})\b/g, '$1********$2')
}

// ─────────────────────────────────────────────────────────────
// 时间与时长
// ─────────────────────────────────────────────────────────────

const DATE_TIME = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const DATE_ONLY = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/** 01-17 09:20 —— 列表与气泡里最常用的紧凑格式 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const at = Date.parse(value)
  if (Number.isNaN(at)) return '—'
  return DATE_TIME.format(at).replace(/\//g, '-')
}

/** 2026-01-17 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const at = Date.parse(value)
  if (Number.isNaN(at)) return '—'
  return DATE_ONLY.format(at).replace(/\//g, '-')
}

/** 09:20:33 —— 回放瀑布图需要到秒 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const at = Date.parse(value)
  if (Number.isNaN(at)) return '—'
  const date = new Date(at)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 相对时间：刚刚 / 3 分钟前 / 2 小时前 / 5 天前 */
export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const at = Date.parse(value)
  if (Number.isNaN(at)) return '—'
  const diff = Date.now() - at
  if (diff < 0) return formatDateTime(value)
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return '刚刚'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return formatDate(value)
}

/** 秒数 -> 「2 分 13 秒」；用于等待时长 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${Math.round(seconds % 60)} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

/** 毫秒 -> 「1.24s」/「820ms」；用于耗时展示 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

/** 指标值格式化：百分比保留 1 位，秒保留 2 位 */
export function formatMetricValue(value: number, unit: string): string {
  if (unit === '%') return `${value.toFixed(1)}%`
  if (unit === 's') return `${value.toFixed(2)}s`
  return `${value}${unit}`
}

/** 千分位，用于会话量等计数 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('zh-CN')
}
