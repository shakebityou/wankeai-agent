/**
 * 工单 Mock —— FE-02 工单列表 / 详情。
 *
 * 只把「已转人工」的会话生成工单，保证工单与会话数量口径一致
 * （避免演示时出现「有工单没会话」的破绽）。
 */
import type { HandoffReason, Priority, TicketDetail, TicketStatus } from '../../src/types/contract'
import type { SeededRandom } from './random'
import type { MockSession } from './sessions'

export interface MockTicket extends TicketDetail {}

const REASON_FROM_HANDOFF: Record<string, HandoffReason> = {
  USER_REQUEST: 'USER_REQUEST',
  EMOTION: 'EMOTION',
  UNRESOLVED: 'UNRESOLVED',
  HIGH_RISK: 'HIGH_RISK',
  SYSTEM_ERROR: 'SYSTEM_ERROR',
}

const ASSIGNEES = ['李敏', '王芳', '陈静', '刘洋', '赵磊', '孙悦']

export function buildTickets(random: SeededRandom, sessions: MockSession[]): MockTicket[] {
  const handoffSessions = sessions.filter((item) => item.summary.ticket_no)

  return handoffSessions.map((session, index) => {
    const summary = session.summary
    const reason = pickReason(random, session)
    const priority = pickPriority(random, reason)
    const status = random.weighted<TicketStatus>(['OPEN', 'PROCESSING', 'CLOSED'], [0.35, 0.4, 0.25])
    const createdAt = summary.last_message_at
    const waitingSeconds = Math.max(0, Math.round((Date.now() - Date.parse(createdAt)) / 1000))
    // SLA：紧急 30 分钟、高 2 小时、普通 8 小时、低 24 小时
    const slaSeconds =
      priority === 'URGENT'
        ? 1800
        : priority === 'HIGH'
          ? 7200
          : priority === 'NORMAL'
            ? 28800
            : 86400

    const subject =
      session.context.unmet_demand ?? `买家就「${session.messages[0]?.text.slice(0, 24) ?? '咨询'}」要求人工处理`

    const ticket: MockTicket = {
      ticket_no: summary.ticket_no!,
      session_id: summary.session_id,
      buyer_display: summary.buyer_display,
      channel: summary.channel,
      reason,
      priority,
      status,
      subject,
      assignee: status === 'OPEN' ? null : random.pick(ASSIGNEES),
      group_name: summary.group_name,
      created_at: createdAt,
      updated_at: new Date(Date.parse(createdAt) + random.int(1, 60) * 60_000).toISOString(),
      waiting_seconds: waitingSeconds,
      sla_breached: waitingSeconds > slaSeconds && status !== 'CLOSED',
      context_snapshot: session.contextBroken ? null : (session.context as TicketDetail['context_snapshot']),
      timeline: buildTimeline(random, session, status, createdAt),
      related_chunk_ids: session.messages
        .flatMap((message) => message.chunk_ids)
        .slice(0, 3),
    }
    void index
    return ticket
  })
}

function pickReason(random: SeededRandom, session: MockSession): HandoffReason {
  const message = session.messages.find((item) => item.handoff_reason)
  if (message?.handoff_reason) return REASON_FROM_HANDOFF[message.handoff_reason] ?? 'USER_REQUEST'
  return random.weighted<HandoffReason>(
    ['USER_REQUEST', 'EMOTION', 'UNRESOLVED', 'HIGH_RISK', 'SYSTEM_ERROR'],
    [0.42, 0.24, 0.18, 0.11, 0.05],
  )
}

function pickPriority(random: SeededRandom, reason: HandoffReason): Priority {
  if (reason === 'HIGH_RISK') return random.weighted<Priority>(['URGENT', 'HIGH'], [0.7, 0.3])
  if (reason === 'EMOTION') return random.weighted<Priority>(['HIGH', 'NORMAL'], [0.45, 0.55])
  if (reason === 'SYSTEM_ERROR') return random.weighted<Priority>(['HIGH', 'NORMAL'], [0.4, 0.6])
  return random.weighted<Priority>(['NORMAL', 'HIGH', 'LOW'], [0.62, 0.24, 0.14])
}

function buildTimeline(
  random: SeededRandom,
  session: MockSession,
  status: TicketStatus,
  createdAt: string,
): TicketDetail['timeline'] {
  const base = Date.parse(createdAt)
  const entries: TicketDetail['timeline'] = [
    {
      at: new Date(base).toISOString(),
      actor: '系统',
      action: '创建工单',
      detail: `由会话 ${session.summary.session_id} 转人工自动创建，渠道 ${session.summary.channel}`,
    },
    {
      at: new Date(base + random.int(5, 40) * 1000).toISOString(),
      actor: '系统',
      action: 'AI 上下文快照',
      detail: '已将七要素上下文（摘要/已验证事实/已尝试动作/未满足诉求/建议动作/情绪/轮次）写入工单',
    },
  ]

  if (status !== 'OPEN') {
    entries.push({
      at: new Date(base + random.int(1, 10) * 60_000).toISOString(),
      actor: '客服主管·李敏',
      action: '指派',
      detail: `指派给 ${random.pick(ASSIGNEES)} 处理`,
    })
  }

  if (status === 'CLOSED') {
    entries.push({
      at: new Date(base + random.int(20, 120) * 60_000).toISOString(),
      actor: random.pick(ASSIGNEES),
      action: '关闭工单',
      detail: '已与买家确认解决方案，买家认可，工单关闭',
    })
  }

  return entries
}
