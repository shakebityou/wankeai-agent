/**
 * 纠错闭环 Mock —— FE-08。
 *
 * 错误类型分布按商家历史分布对齐（任务书 FE-08 给出）：
 *   关键词误命中 40.5% / 话术过时 24.8% / 缺少上下文 20.3% / 政策口径错误 14.4%
 * 注意：任务书把 40.5% 记为「关键词误命中」，在 FE-08 的四选错误类型里
 * 最接近的是「答非所问（IRRELEVANT）」；映射关系写在 FEEDBACK_DISTRIBUTION_MAP，
 * 避免看板与任务书口径对不上。
 */
import type { FeedbackErrorType, FeedbackItem } from '../../src/types/contract'
import type { SeededRandom } from './random'
import type { MockChunk } from './kb'
import type { MockSession } from './sessions'

export interface MockFeedback extends FeedbackItem {}

/** 任务书原文分布 -> FE-08 四选错误类型 */
export const FEEDBACK_DISTRIBUTION_MAP: Record<FeedbackErrorType, number> = {
  IRRELEVANT: 40.5,
  OUTDATED: 24.8,
  FACT_ERROR: 20.3,
  POLICY_MISMATCH: 14.4,
}

const ERROR_TYPE_LABELS: Record<FeedbackErrorType, string> = {
  FACT_ERROR: '事实错误',
  OUTDATED: '话术过时',
  POLICY_MISMATCH: '口径错误',
  IRRELEVANT: '答非所问',
}

const SUBMITTERS = ['李敏', '王芳', '陈静', '刘洋', '赵磊', '孙悦']
const OPERATORS = ['知识库运营·张涛', '客服主管·李敏']

export function buildFeedback(
  random: SeededRandom,
  sessions: MockSession[],
  chunks: MockChunk[],
): MockFeedback[] {
  const items: MockFeedback[] = []
  const chunkIds = chunks.map((chunk) => chunk.chunk_id)

  // 被标记过 feedback_marked 的消息，逐条生成纠错待办
  for (const session of sessions) {
    for (const message of session.messages) {
      if (!message.feedback_marked || message.sender !== 'BOT') continue

      const errorType = random.weighted<FeedbackErrorType>(
        ['IRRELEVANT', 'OUTDATED', 'FACT_ERROR', 'POLICY_MISMATCH'],
        [40.5, 24.8, 20.3, 14.4],
      )
      const status = random.weighted<FeedbackItem['status']>(
        ['OPEN', 'ASSIGNED', 'RESOLVED', 'CLOSED'],
        [0.3, 0.28, 0.24, 0.18],
      )
      const submittedAt = new Date(
        Date.parse(message.created_at) + random.int(5, 120) * 60_000,
      ).toISOString()

      const linked = message.chunk_ids.length > 0 ? message.chunk_ids : [random.pick(chunkIds)]

      items.push({
        feedback_id: `FB-${String(items.length + 1).padStart(4, '0')}`,
        message_id: message.message_id,
        session_id: session.summary.session_id,
        chunk_ids: linked,
        error_type: errorType,
        comment: buildComment(random, errorType, message.text),
        submitted_by: random.pick(SUBMITTERS),
        submitted_at: submittedAt,
        status,
        assignee: status === 'OPEN' ? null : random.pick(OPERATORS),
        resolved_chunk_id: status === 'RESOLVED' || status === 'CLOSED' ? linked[0] ?? null : null,
        resolution:
          status === 'RESOLVED' || status === 'CLOSED'
            ? `已修订知识条目 ${linked[0]} 并重新提交审核，${ERROR_TYPE_LABELS[errorType]}问题已闭环`
            : null,
      })
    }
  }

  // 保证看板有足够样本量（至少 120 条），否则分布图看不出形状
  while (items.length < 120) {
    const session = random.pick(sessions)
    const botMessage = session.messages.find((message) => message.sender === 'BOT')
    if (!botMessage) continue

    const errorType = random.weighted<FeedbackErrorType>(
      ['IRRELEVANT', 'OUTDATED', 'FACT_ERROR', 'POLICY_MISMATCH'],
      [40.5, 24.8, 20.3, 14.4],
    )
    const status = random.weighted<FeedbackItem['status']>(
      ['OPEN', 'ASSIGNED', 'RESOLVED', 'CLOSED'],
      [0.3, 0.28, 0.24, 0.18],
    )
    const linked = botMessage.chunk_ids.length > 0 ? botMessage.chunk_ids : [random.pick(chunkIds)]

    items.push({
      feedback_id: `FB-${String(items.length + 1).padStart(4, '0')}`,
      message_id: botMessage.message_id,
      session_id: session.summary.session_id,
      chunk_ids: linked,
      error_type: errorType,
      comment: buildComment(random, errorType, botMessage.text),
      submitted_by: random.pick(SUBMITTERS),
      submitted_at: new Date(
        Date.parse(botMessage.created_at) + random.int(5, 300) * 60_000,
      ).toISOString(),
      status,
      assignee: status === 'OPEN' ? null : random.pick(OPERATORS),
      resolved_chunk_id: status === 'RESOLVED' || status === 'CLOSED' ? linked[0] ?? null : null,
      resolution:
        status === 'RESOLVED' || status === 'CLOSED'
          ? `已修订知识条目 ${linked[0]} 并重新提交审核`
          : null,
    })
  }

  return items
}

function buildComment(
  random: SeededRandom,
  errorType: FeedbackErrorType,
  messageText: string,
): string {
  const snippet = messageText.replace(/\s+/g, ' ').slice(0, 24)
  switch (errorType) {
    case 'FACT_ERROR':
      return random.pick([
        `回复中「${snippet}…」与商品主数据表不符，材质写错了`,
        `尺码数据引用错误，实际是 20cm 不是 25cm`,
        `库存状态写成了现货，实际该 SKU 是预售 7 天`,
      ])
    case 'OUTDATED':
      return random.pick([
        `这条话术还是 2024 年的口径，售后政策已更新为 7 天`,
        `优惠券规则过期了，本期活动已经结束`,
        `运费模板已调整，回复里的运费金额不对`,
      ])
    case 'POLICY_MISMATCH':
      return random.pick([
        `运费承担口径说反了，质量问题应该商家承担`,
        `退货时限说成了 15 天，实际是 7 天`,
        `和售后政策 4.2 条冲突，需要按政策口径统一`,
      ])
    case 'IRRELEVANT':
    default:
      return random.pick([
        `买家问的是洗护，回复的是物流，完全答非所问`,
        `关键词命中了「退」，但买家问的是「退休熊」这个 IP 名`,
        `没有结合上下文，重复了上一轮已经说过的内容`,
      ])
  }
}
