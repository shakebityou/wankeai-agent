/**
 * 指标 Mock —— FE-09 指标看板（12 项验收指标）。
 *
 * 三条口径约束：
 *  1. 12 项指标 ID 与任务书 FE-09 逐条对应，不得增删；
 *  2. **等于目标值算达标**（FE-09 DoD），因此比较用 >= / <= 而非 >；
 *  3. HC-2：只产出聚合值，绝不携带任何买家/会话明细。
 *
 * 数值刻意做成「9 达标 / 2 预警 / 1 未达标」，用于验证看板的
 * 三态配色与「未达标指标置顶」排序，避免演示时全是绿色看不出效果。
 */
import type {
  HandoffFunnelStage,
  MetricDefinition,
  MetricsIntentDistribution,
  MetricsTrendPoint,
  MetricId,
} from '../../src/types/contract'
import type { SeededRandom } from './random'
import type { MockSession } from './sessions'

/** 判定预警的容差：与目标的相对差距在 3% 以内算预警，超过算未达标 */
const WARNING_TOLERANCE = 0.03

interface MetricSeed {
  metric_id: MetricId
  name: string
  unit: string
  target: number
  direction: 'GTE' | 'LTE'
  current: number
  baseline: number | null
  definition: string
  /** 趋势基准（30 天前的起点），用于生成有走向的折线 */
  trend_start: number
}

const SEEDS: MetricSeed[] = [
  {
    metric_id: 'INTENT_ACCURACY',
    name: '意图准确率',
    unit: '%',
    target: 93,
    direction: 'GTE',
    current: 94.2,
    baseline: 61.2,
    definition: '抽样标注集上，意图识别结果与人工标注一致的占比（分母为全部标注轮次）',
    trend_start: 88.4,
  },
  {
    metric_id: 'RAG_FACT_ACCURACY',
    name: 'RAG 事实准确率',
    unit: '%',
    target: 95,
    direction: 'GTE',
    current: 96.1,
    baseline: 61.2,
    definition: '回答中的事实陈述与知识库/接口取数一致的占比，由人工抽检 + 规则校验共同判定',
    trend_start: 90.2,
  },
  {
    metric_id: 'NO_EVIDENCE_REFUSAL',
    name: '无依据拒答率',
    unit: '%',
    target: 100,
    direction: 'GTE',
    current: 100,
    baseline: null,
    definition: '无知识命中且接口无数据时，选择拒答或转人工的占比（要求 100%，不允许编造）',
    trend_start: 97.5,
  },
  {
    metric_id: 'ASR_ACCURACY',
    name: 'ASR 字准率',
    unit: '%',
    target: 92,
    direction: 'GTE',
    current: 93.4,
    baseline: 88.6,
    definition: '语音消息转写文本与人工校对文本的字符级一致率（编辑距离口径）',
    trend_start: 90.1,
  },
  {
    metric_id: 'PRODUCT_IMAGE_TOPK3',
    name: '商品图 Top-3 命中率',
    unit: '%',
    target: 90,
    direction: 'GTE',
    current: 86.2,
    baseline: null,
    definition: '买家发图后，正确商品出现在检索结果前 3 名的占比（图文一致性评测集）',
    trend_start: 82.7,
  },
  {
    metric_id: 'ORDER_QUERY_SUCCESS',
    name: '订单查询成功率',
    unit: '%',
    target: 99,
    direction: 'GTE',
    current: 99.4,
    baseline: null,
    definition: '商家订单/物流接口调用返回有效数据的占比（不含业务性空结果）',
    trend_start: 98.1,
  },
  {
    metric_id: 'FIRST_RESPONSE_P95',
    name: '首响 P95',
    unit: 's',
    target: 2,
    direction: 'LTE',
    current: 1.62,
    baseline: null,
    definition: '从买家消息到机器人首个 delta 的耗时 P95（秒），越小越好',
    trend_start: 2.14,
  },
  {
    metric_id: 'E2E_RESOLUTION',
    name: '端到端解决率',
    unit: '%',
    target: 70,
    direction: 'GTE',
    current: 68.4,
    baseline: 58.4,
    definition: '未转人工且在会话结束后 24 小时内无二次进线的会话占比',
    trend_start: 64.9,
  },
  {
    metric_id: 'HANDOFF_RATE',
    name: '转人工率',
    unit: '%',
    target: 30,
    direction: 'LTE',
    current: 13.3,
    baseline: 13.3,
    definition:
      '触发转人工的会话占全部会话的比值（越小越好）。验收口径 ≤30%；本项目的优化目标为 ≤8%',
    trend_start: 15.8,
  },
  {
    metric_id: 'HANDOFF_SUCCESS',
    name: '转接成功率',
    unit: '%',
    target: 99.5,
    direction: 'GTE',
    current: 99.7,
    baseline: null,
    definition: '转人工请求被成功接起并分配给坐席的占比（含并发冲突重试后成功）',
    trend_start: 99.1,
  },
  {
    metric_id: 'CONTEXT_COMPLETENESS',
    name: '上下文完整率',
    unit: '%',
    target: 100,
    direction: 'GTE',
    current: 98.6,
    baseline: null,
    definition: 'AI 上下文七要素（摘要/已验证事实/已尝试动作/未满足诉求/建议动作/情绪/轮次）齐全的占比',
    trend_start: 96.2,
  },
  {
    metric_id: 'AVAILABILITY',
    name: '可用性',
    unit: '%',
    target: 99.9,
    direction: 'GTE',
    current: 99.94,
    baseline: null,
    definition: '月度可用性 = 1 - 不可用时长/总时长（按 5 分钟粒度探测 BFF 与下游）',
    trend_start: 99.82,
  },
]

/**
 * 判定达标状态。
 * FE-09 DoD：**等于目标值算达标**；未达标再按容差区分预警与失败。
 */
export function judgeMetric(
  current: number,
  target: number,
  direction: 'GTE' | 'LTE',
): MetricDefinition['status'] {
  const met = direction === 'GTE' ? current >= target : current <= target
  if (met) return 'OK'
  // 相对差距：GTE 用缺口/目标，LTE 用超出/目标
  const gap = direction === 'GTE' ? (target - current) / target : (current - target) / target
  return gap <= WARNING_TOLERANCE ? 'WARNING' : 'FAIL'
}

export function buildMetrics(random: SeededRandom): MetricDefinition[] {
  const measuredAt = new Date().toISOString()
  void random
  return SEEDS.map((seed) => ({
    metric_id: seed.metric_id,
    name: seed.name,
    unit: seed.unit,
    target: seed.target,
    direction: seed.direction,
    current: seed.current,
    baseline: seed.baseline,
    status: judgeMetric(seed.current, seed.target, seed.direction),
    definition: seed.definition,
    measured_at: measuredAt,
  }))
}

/** 未达标置顶排序（FE-09 要求）：FAIL -> WARNING -> OK，同组内保持任务书原始顺序 */
export function sortMetricsForDashboard(metrics: MetricDefinition[]): MetricDefinition[] {
  const weight: Record<MetricDefinition['status'], number> = { FAIL: 0, WARNING: 1, OK: 2 }
  return [...metrics].sort((a, b) => {
    const diff = weight[a.status] - weight[b.status]
    if (diff !== 0) return diff
    return SEEDS.findIndex((s) => s.metric_id === a.metric_id) - SEEDS.findIndex((s) => s.metric_id === b.metric_id)
  })
}

/**
 * 生成趋势点：以 trend_start 为起点向 current 收敛，叠加小幅噪声。
 * 用种子随机而非 Math.random —— 保证每次刷新折线形状一致，便于截图走查。
 */
export function buildTrend(
  random: SeededRandom,
  granularity: 'DAY' | 'WEEK',
  metricIds: MetricId[],
  bucketCount: number,
): MetricsTrendPoint[] {
  const seeds = SEEDS.filter((seed) => metricIds.includes(seed.metric_id))
  const points: MetricsTrendPoint[] = []
  const base = Date.now()

  for (let i = bucketCount - 1; i >= 0; i -= 1) {
    const ratio = (bucketCount - 1 - i) / Math.max(1, bucketCount - 1)
    const bucket =
      granularity === 'DAY'
        ? new Date(base - i * 86_400_000).toISOString().slice(0, 10)
        : weekLabel(new Date(base - i * 7 * 86_400_000))

    const values: Partial<Record<MetricId, number>> = {}
    for (const seed of seeds) {
      const linear = seed.trend_start + (seed.current - seed.trend_start) * ratio
      // 噪声幅度与目标量级相关，避免小数值指标被噪声淹没
      const noiseScale = Math.max(Math.abs(seed.current) * 0.012, 0.02)
      const value = linear + (random.next() - 0.5) * noiseScale
      values[seed.metric_id] = Number(value.toFixed(seed.unit === 's' ? 2 : 1))
    }
    points.push({ bucket, values })
  }

  return points
}

function weekLabel(date: Date): string {
  const target = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()))
  const dayNum = target.getUTCDay() || 7
  target.setUTCDate(target.getUTCDate() + 4 - dayNum)
  const yearStart = new Date(Date.UTC(target.getUTCFullYear(), 0, 1))
  const week = Math.ceil(((target.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7)
  return `${target.getUTCFullYear()}-W${String(week).padStart(2, '0')}`
}

// ─────────────────────────────────────────────────────────────
// 渠道 / 意图分布与转人工漏斗 —— 全部由真实 324 条语料聚合得出
// ─────────────────────────────────────────────────────────────

const INTENT_LABELS: Record<string, string> = {
  PRODUCT_CONSULT: '商品咨询',
  ORDER_LOGISTICS: '订单物流',
  AFTER_SALE: '售后服务',
  MARKETING: '营销活动',
  COMPLAINT: '投诉',
  BRAND_TRUST: '品牌信任',
}

export function buildChannelDistribution(sessions: MockSession[]) {
  const map = new Map<string, { sessions: number; handoff: number }>()
  for (const session of sessions) {
    const key = session.summary.channel
    const entry = map.get(key) ?? { sessions: 0, handoff: 0 }
    entry.sessions += 1
    if (session.summary.ticket_no) entry.handoff += 1
    map.set(key, entry)
  }
  return [...map.entries()].map(([channel, entry]) => ({
    channel: channel as MockSession['summary']['channel'],
    sessions: entry.sessions,
    handoff: entry.handoff,
    handoff_rate: Number(((entry.handoff / entry.sessions) * 100).toFixed(1)),
  }))
}

export function buildIntentDistribution(sessions: MockSession[]): MetricsIntentDistribution[] {
  const map = new Map<string, { count: number; handoff: number }>()
  for (const session of sessions) {
    const intent = session.summary.intents[0] ?? 'PRODUCT_CONSULT'
    const entry = map.get(intent) ?? { count: 0, handoff: 0 }
    entry.count += 1
    if (session.summary.ticket_no) entry.handoff += 1
    map.set(intent, entry)
  }
  // 准确率：以「未转人工」作为机器人处理得当的近似代理指标，仅用于看板展示
  return [...map.entries()]
    .map(([intent, entry]) => ({
      intent: INTENT_LABELS[intent] ?? intent,
      count: entry.count,
      accuracy: Number((((entry.count - entry.handoff) / entry.count) * 100).toFixed(1)),
    }))
    .sort((a, b) => b.count - a.count)
}

export function buildHandoffFunnel(sessions: MockSession[]): HandoffFunnelStage[] {
  const total = sessions.length
  const handoff = sessions.filter((item) => item.summary.ticket_no).length
  const botResolved = total - handoff
  // 人工接通：转人工中排除尚未接起的（这里按 96% 接通率折算）
  const picked = Math.round(handoff * 0.96)
  const closed = Math.round(picked * 0.82)

  const stages: HandoffFunnelStage[] = [
    { stage: '会话总量', count: total, conversion: 100 },
    {
      stage: '机器人独立解决',
      count: botResolved,
      conversion: Number(((botResolved / total) * 100).toFixed(1)),
    },
    {
      stage: '触发转人工',
      count: handoff,
      conversion: Number(((handoff / total) * 100).toFixed(1)),
    },
    {
      stage: '人工接通',
      count: picked,
      conversion: Number(((picked / handoff) * 100).toFixed(1)),
    },
    {
      stage: '工单关闭',
      count: closed,
      conversion: Number(((closed / picked) * 100).toFixed(1)),
    },
  ]
  return stages
}
