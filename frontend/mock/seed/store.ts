/**
 * Mock 数据仓库 —— 服务端内存态。
 *
 * 关键设计：Mock 必须是「有状态」的，否则 FE-03（接管/释放）、
 * FE-05（审核）、FE-13（规则保存）这类写操作无法在演示中体现效果。
 * 这里用一个模块级单例保存全部可变数据，Vite dev server 重启即复位。
 */
import { SeededRandom } from './random'
import { buildSessions, type MockSession } from './sessions'
import { buildKbChunks, type MockChunk } from './kb'
import { buildTickets, type MockTicket } from './tickets'
import { buildFeedback, type MockFeedback } from './feedback'
import { buildChannels, buildHandoffRules, type MockChannel, type MockRule } from './config'
import { buildAgentGroups, type MockAgentGroup } from './agent-groups'

export interface MockStore {
  random: SeededRandom
  sessions: MockSession[]
  tickets: MockTicket[]
  chunks: MockChunk[]
  feedback: MockFeedback[]
  channels: MockChannel[]
  rules: MockRule[]
  groups: MockAgentGroup[]
  /** 导入任务进度表：import_id -> progress */
  imports: Map<string, ImportTaskState>
  /** 规则试算的历史留痕，用于验证「变更留日志」 */
  ruleChangeLog: Array<{ at: string; actor: string; detail: string }>
  channelChangeLog: Array<{ at: string; actor: string; detail: string }>
}

export interface ImportTaskState {
  import_id: string
  status: 'RUNNING' | 'DONE' | 'PARTIAL_FAILED' | 'FAILED'
  total: number
  succeeded: number
  failed: number
  failures: Array<{ row: number; reason: string; raw: string }>
  started_at: string
  finished_at: string | null
  /** 已生成的 chunk（预览后确认导入的） */
  chunks: MockChunk[]
}

function createStore(): MockStore {
  const random = new SeededRandom(20260101)
  const sessions = buildSessions(random)
  const tickets = buildTickets(random, sessions)
  const chunks = buildKbChunks(random)
  const feedback = buildFeedback(random, sessions, chunks)

  return {
    random,
    sessions,
    tickets,
    chunks,
    feedback,
    channels: buildChannels(random),
    rules: buildHandoffRules(),
    groups: buildAgentGroups(),
    imports: new Map(),
    ruleChangeLog: [],
    channelChangeLog: [],
  }
}

let singleton: MockStore | null = null

/** 获取（或首次创建）Mock 数据仓库 */
export function store(): MockStore {
  if (!singleton) singleton = createStore()
  return singleton
}

/** 供测试重置 */
export function resetStore(): void {
  singleton = null
}

export function nowIso(): string {
  return new Date().toISOString()
}

/** 相对当前时间偏移 n 分钟，返回 ISO 字符串 */
export function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
}

export function daysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString()
}

export function daysFromNow(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString()
}
