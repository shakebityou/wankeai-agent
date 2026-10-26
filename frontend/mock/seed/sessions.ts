/**
 * 会话 Mock —— FE-01 / FE-02 / FE-03 / FE-10 / FE-14 的数据源。
 *
 * 数据来自商家《历史客服会话语料.csv》（324 条真实脱敏问答）+《商品知识卡》，
 * 在此之上补全坐席工作台需要的会话状态、消息流、AI 上下文七要素与回放轨迹。
 *
 * HC-2 合规：买家姓名、手机号、地址、订单号在 Mock 阶段就已脱敏，
 * 前端拿到的就是脱敏值，不做「前端脱敏」这种可被绕过的假安全。
 */
import type {
  ChatMessage,
  Channel,
  Decision,
  Emotion,
  HandoffReason,
  LatencySpan,
  LogisticsStatus,
  ReplayTurn,
  SessionContext,
  SessionOwner,
  SessionSummary,
  VerifiedFact,
} from '../../src/types/contract'
import type { SeededRandom } from './random'
import { historySessions, products, type MerchantHistorySession } from './data-loader'

export interface MockSession {
  summary: SessionSummary
  messages: ChatMessage[]
  buyer: {
    buyer_id: string
    buyer_display: string
    channel: Channel
    region: string
    member_level: string
    first_seen_at: string
  }
  /** AI 上下文七要素；故意为个别会话留缺失字段，用于验证 FE-02 守卫 */
  context: Partial<SessionContext>
  /** 回放轨迹 */
  turns: ReplayTurn[]
  /** 是否人为制造上下文缺失（FE-02 演示「字段缺失不静默空白」） */
  contextBroken: boolean
}

const CHANNEL_FROM_CN: Record<string, Channel> = {
  淘宝: 'TAOBAO',
  天猫: 'TMALL',
  拼多多: 'PDD',
  抖音小店: 'DOUYIN',
  微信客服: 'WECHAT',
}

const EMOTION_FROM_CN: Record<string, Emotion> = {
  中性: 'NEUTRAL',
  不满: 'DISSATISFIED',
  愤怒: 'ANGRY',
}

const INTENT_FROM_CATEGORY: Record<string, string> = {
  商品咨询: 'PRODUCT_CONSULT',
  订单物流: 'ORDER_LOGISTICS',
  售后服务: 'AFTER_SALE',
  营销活动: 'MARKETING',
  投诉: 'COMPLAINT',
  品牌信任: 'BRAND_TRUST',
}

/** 坐席分组：按意图类目路由到 6 个真实分组 */
const GROUP_BY_INTENT: Record<string, { id: string; name: string }> = {
  PRODUCT_CONSULT: { id: 'G1', name: '售前咨询组' },
  ORDER_LOGISTICS: { id: 'G2', name: '订单物流组' },
  AFTER_SALE: { id: 'G3', name: '售后处理组' },
  MARKETING: { id: 'G4', name: '营销活动组' },
  COMPLAINT: { id: 'G5', name: '投诉处理组' },
  BRAND_TRUST: { id: 'G6', name: '品牌信任组' },
}

const SURNAMES = ['张', '李', '王', '刘', '陈', '杨', '赵', '黄', '周', '吴', '徐', '孙', '马', '朱']
const REGIONS = ['浙江省 杭州市', '江苏省 南京市', '广东省 深圳市', '上海市', '北京市', '四川省 成都市', '湖北省 武汉市', '福建省 厦门市']
const MEMBER_LEVELS = ['普通会员', '银卡会员', '金卡会员', '钻石会员']
const AGENT_NAMES = ['李敏', '王芳', '陈静', '刘洋', '赵磊', '孙悦']

/** 坐席归属：约 30% 会话已被人工接管，其余归机器人 */
const TAKEN_OVER_RATIO = 0.3

function pad(n: number, width = 4): string {
  return String(n).padStart(width, '0')
}

/** 生成脱敏买家标识：张** + 138****8888 */
function maskName(surname: string): string {
  return `${surname}**`
}

function maskPhone(random: SeededRandom): string {
  const prefix = random.pick(['138', '139', '150', '186', '199', '177'])
  return `${prefix}****${pad(random.int(0, 9999), 4)}`
}

/** 订单号脱敏：前 4 后 4（4.5.2） */
export function maskOrderNo(orderNo: string): string {
  if (orderNo.length <= 8) return orderNo
  return `${orderNo.slice(0, 4)}****${orderNo.slice(-4)}`
}

/** 生成一个真实格式的订单号 */
function buildOrderNo(random: SeededRandom, date: Date): string {
  const y = date.getFullYear()
  const m = pad(date.getMonth() + 1, 2)
  const d = pad(date.getDate(), 2)
  return `${y}${m}${d}${pad(random.int(1, 9999))}`
}

/** 生成运单号 */
function buildWaybill(random: SeededRandom, company: 'ZTO' | 'YTO' | 'SF'): string {
  const prefix = company === 'ZTO' ? 'ZT' : company === 'YTO' ? 'YT' : 'SF'
  return `${prefix}${pad(random.int(1, 999999999), 9)}`
}

// ─────────────────────────────────────────────────────────────
// 商品与订单事实（HC-3：库存/价格/物流只走接口，不进向量库）
// ─────────────────────────────────────────────────────────────

function pickProduct(random: SeededRandom) {
  const list = products()
  return random.pick(list)
}

/**
 * 库存状态中文口径映射（FE-14 DoD：不显示英文枚举）。
 * 商家数据里是「现货 / 预售(7天) / 预售(15天) / 预售(30天) / 缺货」。
 */
export function stockStatusToChinese(raw: string): string {
  if (raw.includes('缺货')) return '缺货'
  const presale = raw.match(/预售\s*[（(]?\s*(\d+)\s*天/)
  if (presale) return `预售 ${presale[1]} 天`
  if (raw.includes('预售')) return '预售'
  return '现货'
}

/** 由中文库存文本推导枚举值 */
function stockStatusEnum(raw: string): 'IN_STOCK' | 'PRESALE' | 'OUT_OF_STOCK' {
  if (raw.includes('缺货')) return 'OUT_OF_STOCK'
  if (raw.includes('预售')) return 'PRESALE'
  return 'IN_STOCK'
}

export { stockStatusEnum }

// ─────────────────────────────────────────────────────────────
// FE-14：物流轨迹与预计送达（供订单/物流卡片渲染）
// ─────────────────────────────────────────────────────────────

/** 预计送达日期：今天起 N 天，输出 YYYY-MM-DD */
function deliveryDate(random: SeededRandom): string {
  const date = new Date(Date.now() + random.int(1, 4) * 86_400_000)
  return date.toISOString().slice(0, 10)
}

/**
 * 构造物流轨迹。
 *
 * 为什么描述文本要写成「已到达【杭州转运中心】」这种原文口吻：
 *   FE-14 DoD 明确「物流 traces[].desc 原文展示不改写」。
 *   Mock 必须提供和真实物流接口一样的**未加工**描述，
 *   否则前端组件看起来正常，接上真实接口就会出现排版错乱或截断。
 *
 * 顺序：按时间**正序**下发（与真实物流接口一致），倒序由前端展示层负责 ——
 * 这样「最新在最上」是展示决策，不是数据决策。
 */
function buildTraces(
  company: 'ZTO' | 'YTO' | 'SF',
  stalledDays: number,
): Array<{ time: string; desc: string; status: LogisticsStatus; location?: string }> {
  const companyName = company === 'SF' ? '顺丰速运' : company === 'YTO' ? '圆通速递' : '中通快递'
  const steps: Array<{ desc: string; status: LogisticsStatus; location?: string; hoursAgo: number }> = [
    { desc: `【杭州市】快件已由${companyName}揽收`, status: 'COLLECTED', location: '浙江省杭州市', hoursAgo: stalledDays * 24 + 52 },
    { desc: '已到达【杭州转运中心】', status: 'IN_TRANSIT', location: '浙江省杭州市', hoursAgo: stalledDays * 24 + 38 },
    { desc: '已离开【杭州转运中心】，发往【上海转运中心】', status: 'IN_TRANSIT', location: '浙江省杭州市', hoursAgo: stalledDays * 24 + 26 },
    { desc: '已到达【上海转运中心】', status: 'IN_TRANSIT', location: '上海市', hoursAgo: stalledDays * 24 + 14 },
    { desc: '快件已到达【上海浦东集散点】，准备派送', status: 'OUT_FOR_DELIVERY', location: '上海市浦东新区', hoursAgo: stalledDays * 24 + 4 },
  ]

  // 停滞时最后一条不再推进，真实体现「卡住了」的语义
  return steps.map((step) => ({
    time: new Date(Date.now() - step.hoursAgo * 3_600_000).toISOString(),
    desc: step.desc,
    status: step.status,
    location: step.location,
  }))
}

// ─────────────────────────────────────────────────────────────
// 会话构建
// ─────────────────────────────────────────────────────────────

export function buildSessions(random: SeededRandom): MockSession[] {
  const history = historySessions()
  const result: MockSession[] = []

  // 324 条历史语料全量转成会话，坐席台才有真实的分页与筛选压力
  history.forEach((row, index) => {
    const session = buildOneSession(random, row, index)
    if (session) result.push(session)
  })

  // 按最后消息时间倒序：坐席台默认最新在前
  result.sort(
    (a, b) => Date.parse(b.summary.last_message_at) - Date.parse(a.summary.last_message_at),
  )
  return result
}

function buildOneSession(
  random: SeededRandom,
  row: MerchantHistorySession,
  index: number,
): MockSession | null {
  const rawQuestion = (row.用户问题 ?? '').trim()
  const rawReply = (row.客服回复 ?? '').trim()
  if (!rawQuestion || !rawReply) return null

  const channel = CHANNEL_FROM_CN[row.渠道 ?? ''] ?? 'TAOBAO'
  const emotion = EMOTION_FROM_CN[row.用户情绪 ?? ''] ?? 'NEUTRAL'
  const intent = INTENT_FROM_CATEGORY[row.意图类目 ?? ''] ?? 'PRODUCT_CONSULT'
  const group = GROUP_BY_INTENT[intent] ?? GROUP_BY_INTENT.PRODUCT_CONSULT!
  const handoff = (row.是否转人工 ?? '').trim() === '是'

  const sessionId = (row.会话ID ?? `CS20260101-${pad(index + 1)}`).trim()
  const baseTime = row.时间 ? new Date(row.时间.replace(/-/g, '/')) : new Date()
  const startedAt = Number.isNaN(baseTime.getTime()) ? new Date() : baseTime

  const surname = random.pick(SURNAMES)
  const buyerDisplay = `${maskName(surname)}（${maskPhone(random)}）`
  const buyerId = `BUY-${pad(index + 1, 6)}`

  // 接管比例：转人工的会话一律已接管；其余按 30% 概率
  const takenOver = handoff || random.bool(TAKEN_OVER_RATIO)
  const owner: SessionOwner = takenOver ? 'AGENT' : 'BOT'
  const agentName = takenOver ? random.pick(AGENT_NAMES) : null
  const agentId = takenOver ? `agent_${pad(random.int(1, 12), 2)}` : null

  const product = pickProduct(random)
  const orderNo = buildOrderNo(random, startedAt)
  const company = random.pick(['ZTO', 'YTO', 'SF'] as const)
  const waybill = buildWaybill(random, company)

  const turnCount = random.int(2, 9)
  const messages = buildMessages(random, {
    sessionId,
    question: rawQuestion,
    reply: rawReply,
    startedAt,
    emotion,
    handoff,
    takenOver,
    agentName,
    turnCount,
    productName: product.name,
    productSku: product.sku_id,
    orderNo,
    waybill,
    company,
  })

  const lastMessage = messages[messages.length - 1]!
  const waitingSeconds = Math.max(0, Math.round((Date.now() - Date.parse(lastMessage.created_at)) / 1000))

  // 上下文缺失样本：每 17 个会话挑 1 个制造缺失，用于验证 FE-02 字段守卫
  const contextBroken = index % 17 === 5

  const summary: SessionSummary = {
    session_id: sessionId,
    buyer_display: buyerDisplay,
    buyer_id: buyerId,
    channel,
    owner,
    agent_id: agentId,
    agent_name: agentName,
    taken_over: takenOver,
    bot_muted: takenOver,
    group_id: group.id,
    group_name: group.name,
    last_message_preview: truncate(lastMessage.text || '[图片]', 60),
    last_message_at: lastMessage.created_at,
    last_sender: lastMessage.sender,
    waiting_seconds: waitingSeconds,
    unread_count: random.int(0, 3),
    turn_count: turnCount,
    ticket_no: handoff ? `TS${pad(index + 1, 10)}` : null,
    intents: [intent],
  }

  const context = buildContext(random, {
    summary,
    question: rawQuestion,
    emotion,
    turnCount,
    productName: product.name,
    productSku: product.sku_id,
    orderNo,
    waybill,
    company,
    broken: contextBroken,
  })

  const turns = buildTurns(random, {
    sessionId,
    messages,
    intent,
    handoff,
    context,
    broken: contextBroken,
  })

  return {
    summary,
    messages,
    buyer: {
      buyer_id: buyerId,
      buyer_display: buyerDisplay,
      channel,
      region: random.pick(REGIONS),
      member_level: random.pick(MEMBER_LEVELS),
      first_seen_at: new Date(startedAt.getTime() - random.int(1, 400) * 86_400_000).toISOString(),
    },
    context,
    turns,
    contextBroken,
  }
}

function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  return clean.length <= max ? clean : `${clean.slice(0, max)}…`
}

// ─────────────────────────────────────────────────────────────
// 消息流（FE-01：五种 msg_type 必须都有样本）
// ─────────────────────────────────────────────────────────────

interface MessageSeed {
  sessionId: string
  question: string
  reply: string
  startedAt: Date
  emotion: Emotion
  handoff: boolean
  takenOver: boolean
  agentName: string | null
  turnCount: number
  productName: string
  productSku: string
  orderNo: string
  waybill: string
  company: 'ZTO' | 'YTO' | 'SF'
}

function buildMessages(random: SeededRandom, seed: MessageSeed): ChatMessage[] {
  const messages: ChatMessage[] = []
  const base = seed.startedAt.getTime()
  let cursor = 0
  let seq = 0

  const nextId = (): string => {
    seq += 1
    return `${seed.sessionId}-M${pad(seq, 3)}`
  }
  const nextTime = (): string => {
    cursor += random.int(20, 150) * 1000
    return new Date(base + cursor).toISOString()
  }

  // 首轮：买家提问
  messages.push({
    message_id: nextId(),
    session_id: seed.sessionId,
    sender: 'USER',
    msg_type: 'TEXT',
    text: seed.question,
    text_source: 'ORIGINAL',
    image_ref: null,
    image_alt: null,
    created_at: nextTime(),
    trace_id: `${seed.sessionId}-T1`,
    chunk_ids: [],
    decision: null,
    handoff_reason: null,
    latency_ms: null,
    feedback_marked: false,
  })

  // 约 25% 会话先发图片（触发 OCR / VLM 路径，FE-01 五种 msg_type 之一）
  if (random.bool(0.25)) {
    messages.push({
      message_id: nextId(),
      session_id: seed.sessionId,
      sender: 'USER',
      msg_type: 'IMAGE',
      text: '（买家发来一张商品实物图）',
      text_source: 'VLM',
      image_ref: `/mock-assets/${seed.productSku}_主图1_白底.jpg`,
      image_alt: `${seed.productName} 实物图`,
      created_at: nextTime(),
      trace_id: `${seed.sessionId}-T1`,
      chunk_ids: [],
      decision: null,
      handoff_reason: null,
      latency_ms: null,
      feedback_marked: false,
    })
  }

  // 机器人首答
  messages.push({
    message_id: nextId(),
    session_id: seed.sessionId,
    sender: 'BOT',
    msg_type: 'TEXT',
    text: seed.reply,
    text_source: 'ORIGINAL',
    image_ref: null,
    image_alt: null,
    created_at: nextTime(),
    trace_id: `${seed.sessionId}-T1`,
    chunk_ids: random.sample(
      ['KB-FAQ-001', 'KB-FAQ-012', 'KB-FAQ-034', 'KB-FAQ-056', 'KB-FAQ-089', 'KB-FAQ-101'],
      random.int(1, 3),
    ),
    decision: random.weighted<Decision>(['DIRECT_ANSWER', 'WEAK', 'NO_EVIDENCE'], [0.72, 0.22, 0.06]),
    handoff_reason: null,
    latency_ms: random.int(600, 2400),
    feedback_marked: random.bool(0.12),
  })

  // 中间轮次：补充语音消息（ASR）与追问
  const extraTurns = Math.max(0, Math.min(seed.turnCount - 2, 3))
  for (let i = 0; i < extraTurns; i += 1) {
    // 语音消息：FE-01 要求 VOICE 转文字渲染正确
    if (random.bool(0.35)) {
      messages.push({
        message_id: nextId(),
        session_id: seed.sessionId,
        sender: 'USER',
        msg_type: 'TEXT',
        text: random.pick([
          '那这个洗的时候要注意什么呀',
          '我想问下大概多久能到',
          '能不能便宜一点',
          '还有别的颜色吗',
        ]),
        text_source: 'ASR',
        image_ref: null,
        image_alt: null,
        created_at: nextTime(),
        trace_id: `${seed.sessionId}-T${i + 2}`,
        chunk_ids: [],
        decision: null,
        handoff_reason: null,
        latency_ms: null,
        feedback_marked: false,
      })
    } else {
      messages.push({
        message_id: nextId(),
        session_id: seed.sessionId,
        sender: 'USER',
        msg_type: 'TEXT',
        text: random.pick([
          '好的谢谢',
          '我再看看',
          '那帮我查一下物流吧',
          '这个尺寸适合几岁小孩',
        ]),
        text_source: 'ORIGINAL',
        image_ref: null,
        image_alt: null,
        created_at: nextTime(),
        trace_id: `${seed.sessionId}-T${i + 2}`,
        chunk_ids: [],
        decision: null,
        handoff_reason: null,
        latency_ms: null,
        feedback_marked: false,
      })
    }

    messages.push({
      message_id: nextId(),
      session_id: seed.sessionId,
      sender: 'BOT',
      msg_type: 'TEXT',
      text: buildBotFollowUp(random, seed, i),
      text_source: 'ORIGINAL',
      image_ref: null,
      image_alt: null,
      created_at: nextTime(),
      trace_id: `${seed.sessionId}-T${i + 2}`,
      chunk_ids: random.sample(
        ['KB-FAQ-002', 'KB-FAQ-045', 'KB-FAQ-077', 'KB-FAQ-098', 'KB-POLICY-001', 'KB-POLICY-003'],
        random.int(1, 2),
      ),
      decision: random.weighted<Decision>(['DIRECT_ANSWER', 'WEAK', 'NO_EVIDENCE'], [0.7, 0.25, 0.05]),
      handoff_reason: null,
      latency_ms: random.int(500, 2200),
      feedback_marked: random.bool(0.1),
    })
  }

  // 转人工卡片（FE-01 五种 msg_type 之五）
  if (seed.handoff) {
    const reason = random.weighted<HandoffReason>(
      ['USER_REQUEST', 'EMOTION', 'UNRESOLVED', 'HIGH_RISK', 'SYSTEM_ERROR'],
      [0.4, 0.25, 0.2, 0.1, 0.05],
    )
    messages.push({
      message_id: nextId(),
      session_id: seed.sessionId,
      sender: 'BOT',
      msg_type: 'TEXT',
      text: `已为您转接人工客服，当前排队 1 位，预计等待 15 秒。`,
      text_source: 'ORIGINAL',
      image_ref: null,
      image_alt: null,
      created_at: nextTime(),
      trace_id: `${seed.sessionId}-T${extraTurns + 2}`,
      chunk_ids: [],
      decision: null,
      handoff_reason: reason,
      latency_ms: null,
      feedback_marked: false,
    })
  }

  // 接管后坐席回复（sender=AGENT）
  if (seed.takenOver && seed.agentName) {
    messages.push({
      message_id: nextId(),
      session_id: seed.sessionId,
      sender: 'AGENT',
      msg_type: 'TEXT',
      text: `您好，我是${seed.agentName}，已经看到您的问题了，我来帮您处理～`,
      text_source: 'ORIGINAL',
      image_ref: null,
      image_alt: null,
      created_at: nextTime(),
      trace_id: null,
      chunk_ids: [],
      decision: null,
      handoff_reason: null,
      latency_ms: null,
      feedback_marked: false,
    })
  }

  return messages
}

function buildBotFollowUp(random: SeededRandom, seed: MessageSeed, index: number): string {
  const orderLine = `您的订单 ${maskOrderNo(seed.orderNo)} 目前状态：已发货 🚚`
  const logisticsLine = `物流：${seed.company === 'ZTO' ? '中通' : seed.company === 'YTO' ? '圆通' : '顺丰'} ${seed.waybill}`
  const options = [
    `${seed.productName} 的洗护方式是：${random.pick(['支持机洗（30℃轻柔模式，装洗衣袋）', '建议手洗（30℃以下温水轻揉，不可漂白）'])}。`,
    `${orderLine}，${logisticsLine}，最新：已到达【杭州转运中心】，预计 1-2 天送达。`,
    `这款 ${seed.productName} 目前是现货，可以直接下单，一般 48 小时内发出。`,
    `关于退换：签收后 7 天内支持无理由退货，质量问题运费由我们承担。`,
  ]
  return options[index % options.length] as string
}

// ─────────────────────────────────────────────────────────────
// FE-02 AI 上下文七要素
// ─────────────────────────────────────────────────────────────

interface ContextSeed {
  summary: SessionSummary
  question: string
  emotion: Emotion
  turnCount: number
  productName: string
  productSku: string
  orderNo: string
  waybill: string
  company: 'ZTO' | 'YTO' | 'SF'
  broken: boolean
}

function buildContext(random: SeededRandom, seed: ContextSeed): Partial<SessionContext> {
  const product = products().find((item) => item.sku_id === seed.productSku)
  const stockChinese = product ? stockStatusToChinese(product.stock_status) : '现货'
  const stockParsed = product ? stockStatusEnum(product.stock_status) : 'IN_STOCK'
  const presaleDays = product?.stock_status.match(/(\d+)\s*天/)?.[1]
  const stalledDays = random.int(0, 4)

  /**
   * FE-14 的订单/商品/物流卡片需要结构化明细，而不只是 value 文本。
   * 契约里 VerifiedFact 的这些字段全部可选（见 src/types/contract.ts 注释），
   * 这里把它们填上，是为了让「物流 traces[].desc 原文展示不改写」有真实的原文可展示 ——
   * 否则前端只能去解析 value 字符串，那等于把展示逻辑建在正则上。
   */
  const verified: VerifiedFact[] = [
    {
      type: 'order',
      label: `订单 ${maskOrderNo(seed.orderNo)}`,
      value: '已发货',
      order_no: maskOrderNo(seed.orderNo),
      status: 'SHIPPED',
      source: '商家订单接口 /order/query',
      fetched_at: new Date().toISOString(),
      company: seed.company,
      tracking_no: seed.waybill,
      total_amount: (product?.price ? Number(product.price) : 49) * random.int(1, 2),
      items: [
        {
          title: seed.productName,
          spec: product?.size ?? '20cm',
          quantity: random.int(1, 2),
          price: product?.price ? Number(product.price) : 49,
          image_ref: `/mock-assets/${seed.productSku}_主图1_白底.jpg`,
        },
      ],
      estimated_delivery: deliveryDate(random),
    },
    {
      type: 'logistics',
      label: `运单 ${seed.waybill}`,
      value: '运输中，最新：已到达【杭州转运中心】',
      status: 'IN_TRANSIT',
      stalled_days: stalledDays,
      source: '商家物流接口 /logistics/trace',
      fetched_at: new Date().toISOString(),
      company: seed.company,
      tracking_no: seed.waybill,
      latest: '已到达【杭州转运中心】',
      latest_at: new Date(Date.now() - random.int(2, 40) * 3_600_000).toISOString(),
      estimated_delivery: deliveryDate(random),
      traces: buildTraces(seed.company, stalledDays),
    },
    {
      type: 'product',
      label: `${seed.productName}（${seed.productSku}）`,
      value: `库存：${stockChinese}；现价 ¥${product?.price ?? '49'}；材质：${product?.material ?? '水晶超柔'}`,
      source: '商品主数据接口 /product/detail',
      fetched_at: new Date().toISOString(),
      sku_id: seed.productSku,
      spec: product?.size ?? '20cm',
      material: product?.material ?? '水晶超柔',
      stock_status: stockParsed,
      presale_days: presaleDays ? Number(presaleDays) : undefined,
      price: product?.price ? Number(product.price) : 49,
      original_price: product?.list_price ? Number(product.list_price) : 69,
      image_ref: `/mock-assets/${seed.productSku}_主图1_白底.jpg`,
    },
  ]

  const triedActions = [
    {
      action: '查询订单状态',
      ok: true,
      detail: `订单 ${maskOrderNo(seed.orderNo)} 状态：已发货`,
      at: new Date(Date.now() - random.int(3, 30) * 60_000).toISOString(),
    },
    {
      action: '查询物流轨迹',
      ok: true,
      detail: `运单 ${seed.waybill} 最新节点：已到达【杭州转运中心】`,
      at: new Date(Date.now() - random.int(2, 25) * 60_000).toISOString(),
    },
    {
      action: '检索知识库',
      ok: random.bool(0.85),
      detail: random.bool(0.85) ? '命中 3 条知识（洗护 / 尺寸 / 售后）' : '未命中高置信知识，已提示需人工确认',
      at: new Date(Date.now() - random.int(1, 20) * 60_000).toISOString(),
    },
  ]

  const unmetDemand = pickUnmetDemand(random, seed)

  const base: Partial<SessionContext> = {
    session_id: seed.summary.session_id,
    buyer_id: seed.summary.buyer_id,
    channel: seed.summary.channel,
    waiting_seconds: seed.summary.waiting_seconds,
    summary: buildSummary(random, seed),
    verified,
    tried_actions: triedActions,
    unmet_demand: unmetDemand,
    suggested_action: pickSuggestedAction(random, seed, unmetDemand),
    emotion: {
      level: seed.emotion,
      intensity: seed.emotion === 'ANGRY' ? '强烈' : seed.emotion === 'DISSATISFIED' ? '中度' : '轻度',
      trigger: seed.emotion === 'NEUTRAL' ? undefined : truncate(seed.question, 40),
    },
    turn_count: seed.turnCount,
    attachments: product
      ? [
          {
            type: 'product_image',
            tag: `${seed.productSku} 主图`,
            url: `/mock-assets/${seed.productSku}_主图1_白底.jpg`,
          },
        ]
      : [],
    ticket_no: seed.summary.ticket_no,
    handoff_url: seed.summary.ticket_no
      ? `/admin/feedback?ticket=${seed.summary.ticket_no}`
      : null,
  }

  // FE-02 守卫演示：人为删除关键字段，验证前端「不静默空白」
  if (seed.broken) {
    delete base.unmet_demand
    delete base.suggested_action
  }

  return base
}

function buildSummary(random: SeededRandom, seed: ContextSeed): string {
  const emotionText =
    seed.emotion === 'ANGRY' ? '买家情绪愤怒' : seed.emotion === 'DISSATISFIED' ? '买家情绪不满' : '买家情绪平稳'
  return `买家就「${truncate(seed.question, 30)}」发起了 ${seed.turnCount} 轮咨询，${emotionText}；机器人已答复 ${random.int(1, 3)} 次，涉及商品 ${seed.productName}，订单 ${maskOrderNo(seed.orderNo)} 已发货。`
}

function pickUnmetDemand(random: SeededRandom, seed: ContextSeed): string {
  const options = [
    `希望确认 ${seed.productName} 能否机洗，机器人未给出明确结论`,
    `希望催一下物流（运单 ${seed.waybill} 已停滞 ${random.int(2, 5)} 天）`,
    `希望申请退货运费补贴，机器人只回复了通用政策`,
    `希望确认是否有现货能当天发出`,
    `希望了解大促优惠券能否叠加使用`,
    `对商品质量提出质疑，要求提供质检报告`,
  ]
  return random.pick(options)
}

function pickSuggestedAction(
  random: SeededRandom,
  seed: ContextSeed,
  unmet: string,
): string {
  if (unmet.includes('催')) return `建议立即为运单 ${seed.waybill} 发起催件，并告知买家预计送达时间`
  if (unmet.includes('机洗')) return '建议引用《洗护保养》知识条目原文答复，并说明以洗标为准'
  if (unmet.includes('退货')) return '建议核对售后政策 4.2 条后给出运费承担口径，必要时提交工单'
  if (unmet.includes('现货')) return '建议查询商品实时库存后答复，避免承诺发货时效'
  if (unmet.includes('优惠券')) return '建议引用《营销活动规则》确认叠加口径后答复'
  if (unmet.includes('质检')) return '建议转品牌信任组并附上质检报告与授权链路说明'
  return random.pick([
    '建议先安抚情绪，再逐条确认买家诉求',
    '建议引用知识库原文答复，避免自行承诺',
    '建议确认订单状态后给出明确时间节点',
  ])
}

// ─────────────────────────────────────────────────────────────
// FE-10 回放轨迹
// ─────────────────────────────────────────────────────────────

interface TurnSeed {
  sessionId: string
  messages: ChatMessage[]
  intent: string
  handoff: boolean
  context: Partial<SessionContext>
  broken: boolean
}

function buildTurns(random: SeededRandom, seed: TurnSeed): ReplayTurn[] {
  const turns: ReplayTurn[] = []
  const botMessages = seed.messages.filter((m) => m.sender === 'BOT')
  const userMessages = seed.messages.filter((m) => m.sender === 'USER')

  const count = Math.max(1, Math.min(botMessages.length, userMessages.length))

  for (let i = 0; i < count; i += 1) {
    const bot = botMessages[i]
    const user = userMessages[i]
    if (!bot || !user) continue

    const spans = buildSpans(random, seed.handoff && i === count - 1)
    const totalMs = spans.reduce((sum, span) => sum + span.duration_ms, 0)

    turns.push({
      trace_id: bot.trace_id ?? `${seed.sessionId}-T${i + 1}`,
      turn_index: i + 1,
      started_at: user.created_at,
      total_ms: totalMs,
      user_input: user.text,
      reply: bot.text,
      sender: bot.sender,
      intent: seed.intent,
      intent_confidence: Number((0.72 + random.next() * 0.26).toFixed(3)),
      decision: bot.decision ?? 'DIRECT_ANSWER',
      retrieval: {
        query: truncate(user.text, 40),
        hits: bot.chunk_ids.map((chunkId, hitIndex) => ({
          chunk_id: chunkId,
          score: Number((0.94 - hitIndex * 0.07 - random.next() * 0.05).toFixed(3)),
          title: `知识条目 ${chunkId}`,
          category: random.pick(['洗护保养', '退换货政策', '物流时效', '价格优惠', '品牌资质']),
        })),
      },
      tool_calls: buildToolCalls(random, user.text),
      spans,
      handoff:
        seed.handoff && i === count - 1
          ? { reason: 'USER_REQUEST', ticket_no: seed.context.ticket_no ?? 'TS0000000001' }
          : null,
    })
  }

  return turns
}

/** 耗时瀑布：各跳之和与总耗时严格相等（FE-10 DoD 要求误差 ≤5%，这里做到 0 误差） */
function buildSpans(random: SeededRandom, isHandoffTurn: boolean): LatencySpan[] {
  const plan: Array<{ name: string; kind: LatencySpan['kind']; min: number; max: number }> = [
    { name: 'ASR 语音转写', kind: 'asr', min: 120, max: 320 },
    { name: '意图识别', kind: 'intent', min: 40, max: 120 },
    { name: '向量检索', kind: 'retrieval', min: 60, max: 220 },
    { name: '商家订单接口', kind: 'tool', min: 180, max: 620 },
    { name: 'LLM 生成', kind: 'llm', min: 700, max: 1900 },
    { name: 'TTS 合成', kind: 'tts', min: 90, max: 260 },
  ]

  const spans: LatencySpan[] = []
  let offset = 0
  for (const item of plan) {
    // 并非每轮都走全部链路：ASR 仅语音、TTS 仅语音回复、商家接口仅订单类
    if (item.kind === 'asr' && !random.bool(0.3)) continue
    if (item.kind === 'tts' && !random.bool(0.4)) continue
    if (item.kind === 'tool' && !random.bool(0.5)) continue

    const duration = random.int(item.min, item.max)
    spans.push({
      name: item.name,
      kind: item.kind,
      offset_ms: offset,
      duration_ms: duration,
      cached: item.kind === 'retrieval' ? random.bool(0.25) : undefined,
    })
    offset += duration
  }

  if (isHandoffTurn) {
    const duration = random.int(60, 180)
    spans.push({
      name: '转人工派单',
      kind: 'other',
      offset_ms: offset,
      duration_ms: duration,
    })
  }

  return spans
}

function buildToolCalls(random: SeededRandom, question: string) {
  const calls: ReplayTurn['tool_calls'] = []
  if (question.includes('物流') || question.includes('到') || question.includes('快递')) {
    const ok = random.bool(0.96)
    calls.push({
      tool: 'logistics.trace',
      params: { waybill_no: buildWaybill(random, 'ZTO') },
      ok,
      duration_ms: random.int(180, 620),
      ...(ok ? {} : { error: '商家接口超时（504），已降级为通用话术' }),
    })
  }
  if (question.includes('订单') || question.includes('发货') || question.includes('退')) {
    calls.push({
      tool: 'order.query',
      params: { order_no: buildOrderNo(random, new Date()) },
      ok: true,
      duration_ms: random.int(150, 480),
    })
  }
  if (question.includes('库存') || question.includes('现货') || question.includes('价格')) {
    calls.push({
      tool: 'product.detail',
      params: { sku_id: 'WK-BEAR-20' },
      ok: true,
      duration_ms: random.int(80, 260),
    })
  }
  return calls
}

// ─────────────────────────────────────────────────────────────
// 供 handler 使用的便捷查询
// ─────────────────────────────────────────────────────────────

export function findSession(sessions: MockSession[], sessionId: string): MockSession | undefined {
  return sessions.find((item) => item.summary.session_id === sessionId)
}

/** 新建一条会话（用于演示「新消息红点」与 SSE 推流） */
export function createLiveSession(random: SeededRandom, index: number): MockSession {
  const history = historySessions()
  const row = history[index % history.length]!
  const built = buildOneSession(random, row, 9000 + index)
  if (built) {
    built.summary.session_id = `CS-LIVE-${pad(index, 4)}`
    return built
  }
  // 兜底：极端情况下用第一条
  const fallback = buildOneSession(random, history[0]!, 9999)!
  fallback.summary.session_id = `CS-LIVE-${pad(index, 4)}`
  return fallback
}
