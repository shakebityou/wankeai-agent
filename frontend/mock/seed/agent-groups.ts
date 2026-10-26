/**
 * 坐席分组 Mock —— 基于商家《客服坐席分组表.csv》的 6 个真实分组。
 *
 * 在线时段字段形如 "09:00–22:00"，需要转成 7×24 的布尔网格
 * （FE-12 要求「6 分组 / 7 时段数据完整导入并可视化」）。
 * 7 时段口径：08-10 / 10-12 / 12-14 / 14-16 / 16-18 / 18-20 / 20-22。
 */
import type { Channel } from '../../src/types/contract'
import { groups as merchantGroups } from './data-loader'

export interface MockAgentGroup {
  group_id: string
  group_name: string
  service_scope: string
  agent_count: number
  supervisor: string
  /** 7 天 × 7 时段 */
  schedule: boolean[][]
  channels: Channel[]
  daily_volume: number
  peak_volume: number
  note: string
  /** 原始在线时段文本，展示用 */
  online_window: string
}

/** FE-12 的 7 个时段边界（小时） */
export const SLOT_BOUNDARIES = [8, 10, 12, 14, 16, 18, 20, 22] as const

export const SLOT_LABELS = [
  '08:00–10:00',
  '10:00–12:00',
  '12:00–14:00',
  '14:00–16:00',
  '16:00–18:00',
  '18:00–20:00',
  '20:00–22:00',
] as const

export const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] as const

const CHANNEL_NAME_MAP: Record<string, Channel> = {
  淘宝: 'TAOBAO',
  天猫: 'TMALL',
  拼多多: 'PDD',
  抖音小店: 'DOUYIN',
  抖音: 'DOUYIN',
  微信客服: 'WECHAT',
  微信: 'WECHAT',
}

function parseChannels(raw: string): Channel[] {
  return raw
    .split(/[、,，/]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .map((name) => CHANNEL_NAME_MAP[name])
    .filter((channel): channel is Channel => Boolean(channel))
}

/** 解析 "09:00–22:00" -> {start:9, end:22} */
function parseWindow(raw: string): { start: number; end: number } {
  const match = raw.match(/(\d{1,2}):(\d{2})\s*[–~-]\s*(\d{1,2}):(\d{2})/)
  if (!match) return { start: 9, end: 21 }
  return { start: Number(match[1]), end: Number(match[3]) }
}

/** 生成 7×7 排班网格：时段落在在线窗口内即为在线 */
function buildSchedule(startHour: number, endHour: number): boolean[][] {
  return Array.from({ length: 7 }, () =>
    SLOT_BOUNDARIES.slice(0, -1).map((slotStart, index) => {
      const slotEnd = SLOT_BOUNDARIES[index + 1] ?? slotStart + 2
      // 时段与在线窗口有交集即算在线（坐席交接班按覆盖计算）
      return slotEnd > startHour && slotStart < endHour
    }),
  )
}

export function buildAgentGroups(): MockAgentGroup[] {
  return merchantGroups().map((group) => {
    const { start, end } = parseWindow(group.online_window)
    return {
      group_id: group.group_id,
      group_name: group.group_name,
      service_scope: group.service_scope,
      agent_count: group.agent_count,
      supervisor: group.supervisor,
      schedule: buildSchedule(start, end),
      channels: parseChannels(group.channels_raw),
      daily_volume: group.daily_volume,
      peak_volume: group.peak_volume,
      note: group.note,
      online_window: group.online_window,
    }
  })
}

/**
 * 排班冲突检测（FE-12 DoD「排班冲突检测」）。
 * 判定口径：同一分组同一时段「在线但无坐席可用」视为冲突；
 * 这里给出可演示的两类冲突：① 在线时段内坐席数为 0；② 单时段峰值超容量。
 */
export interface ScheduleConflict {
  group_id: string
  group_name: string
  weekday: number
  slot: number
  type: 'NO_AGENT' | 'OVER_CAPACITY'
  detail: string
}

export function detectConflicts(groups: MockAgentGroup[]): ScheduleConflict[] {
  const conflicts: ScheduleConflict[] = []
  for (const group of groups) {
    for (let day = 0; day < 7; day += 1) {
      const row = group.schedule[day] ?? []
      for (let slot = 0; slot < row.length; slot += 1) {
        if (!row[slot]) continue
        if (group.agent_count <= 0) {
          conflicts.push({
            group_id: group.group_id,
            group_name: group.group_name,
            weekday: day,
            slot,
            type: 'NO_AGENT',
            detail: `${group.group_name} 在 ${WEEKDAY_LABELS[day]} ${SLOT_LABELS[slot]} 在线但无坐席`,
          })
        }
      }
    }

    // 峰值超容量：该组峰值接待量超过「坐席数 × 单坐席峰值处理量（150 通/时段）」
    const capacity = group.agent_count * 150
    if (group.peak_volume > capacity) {
      const busiest = group.schedule[0]?.findIndex((online) => online) ?? 0
      conflicts.push({
        group_id: group.group_id,
        group_name: group.group_name,
        weekday: 0,
        slot: Math.max(0, busiest),
        type: 'OVER_CAPACITY',
        detail: `${group.group_name} 峰值接待量 ${group.peak_volume} 超过容量 ${capacity}（${group.agent_count} 人 × 150）`,
      })
    }
  }
  return conflicts
}
