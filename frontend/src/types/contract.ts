/**
 * 前后端接口契约 —— 严格对齐任务书第 2 章「接口契约设计」。
 *
 * 本文件是前端唯一的数据形状来源：
 *   - 2.6 数据字典与枚举：全部以字面量联合类型落地，禁止在业务代码里写裸字符串；
 *   - 2.5 BFF 接口总览：每个接口的请求/响应形状在这里定义；
 *   - 2.5.2 context 七要素：FE-02 的字段完整性守卫依赖这里的类型。
 *
 * 约定：
 *   1. 所有枚举的英文值原样保留（与后端、向量库、日志同源），
 *      中文展示一律走 label 映射表，不做反向翻译；
 *   2. 可空字段显式标注 `| null`，前端不假设后端一定给值；
 *   3. 时间一律 ISO 8601 字符串（UTC），展示层负责转本地时区。
 */
import type { Paged } from './bff'

// ─────────────────────────────────────────────────────────────
// 2.6 数据字典与枚举
// ─────────────────────────────────────────────────────────────

/** 渠道 */
export const CHANNELS = ['TAOBAO', 'TMALL', 'PDD', 'DOUYIN', 'WECHAT'] as const
export type Channel = (typeof CHANNELS)[number]

export const CHANNEL_LABELS: Record<Channel, string> = {
  TAOBAO: '淘宝',
  TMALL: '天猫',
  PDD: '拼多多',
  DOUYIN: '抖音小店',
  WECHAT: '微信客服',
}

/** 消息类型（收）：TEXT / IMAGE，VOICE 为内部映射 */
export const MSG_TYPES = ['TEXT', 'IMAGE'] as const
export type MsgType = (typeof MSG_TYPES)[number]

/**
 * 消息类型（展示口径）—— 任务书 FE-01 DoD 要求「五种 msg_type 渲染正确」：
 * TEXT / IMAGE / VOICE（语音转文字）/ OCR 结果 / 转人工卡片。
 * 后端只回 TEXT|IMAGE，其余三种由 text_source 与 handoff 标记推导，
 * 推导逻辑集中在 src/utils/message-kind.ts，避免各组件各写一套。
 */
export type MessageKind = 'TEXT' | 'IMAGE' | 'VOICE' | 'OCR' | 'HANDOFF_CARD'

/** 发送方 */
export const SENDERS = ['USER', 'BOT', 'AGENT'] as const
export type Sender = (typeof SENDERS)[number]

export const SENDER_LABELS: Record<Sender, string> = {
  USER: '买家',
  BOT: '智能客服',
  AGENT: '坐席',
}

/** 订单状态 */
export const ORDER_STATUSES = ['CREATED', 'PAID', 'SHIPPED', 'DELIVERED', 'CLOSED'] as const
export type OrderStatus = (typeof ORDER_STATUSES)[number]

export const ORDER_STATUS_LABELS: Record<OrderStatus, string> = {
  CREATED: '待付款',
  PAID: '待发货',
  SHIPPED: '已发货',
  DELIVERED: '已签收',
  CLOSED: '已关闭',
}

/** 物流状态 */
export const LOGISTICS_STATUSES = [
  'PENDING',
  'COLLECTED',
  'IN_TRANSIT',
  'OUT_FOR_DELIVERY',
  'DELIVERED',
  'EXCEPTION',
  'RETURNED',
] as const
export type LogisticsStatus = (typeof LOGISTICS_STATUSES)[number]

export const LOGISTICS_STATUS_LABELS: Record<LogisticsStatus, string> = {
  PENDING: '待揽收',
  COLLECTED: '已揽收',
  IN_TRANSIT: '运输中',
  OUT_FOR_DELIVERY: '派送中',
  DELIVERED: '已签收',
  EXCEPTION: '物流异常',
  RETURNED: '已退回',
}

/** 库存状态 —— FE-14 要求映射中文口径，不显示英文枚举 */
export const STOCK_STATUSES = ['IN_STOCK', 'PRESALE', 'OUT_OF_STOCK'] as const
export type StockStatus = (typeof STOCK_STATUSES)[number]

export const STOCK_STATUS_LABELS: Record<StockStatus, string> = {
  IN_STOCK: '现货',
  PRESALE: '预售',
  OUT_OF_STOCK: '缺货',
}

/** 快递公司 */
export const COMPANY_CODES = ['ZTO', 'YTO', 'SF'] as const
export type CompanyCode = (typeof COMPANY_CODES)[number]

export const COMPANY_LABELS: Record<CompanyCode, string> = {
  ZTO: '中通快递',
  YTO: '圆通速递',
  SF: '顺丰速运',
}

/** 转人工原因 */
export const HANDOFF_REASONS = [
  'USER_REQUEST',
  'EMOTION',
  'UNRESOLVED',
  'HIGH_RISK',
  'SYSTEM_ERROR',
] as const
export type HandoffReason = (typeof HANDOFF_REASONS)[number]

export const HANDOFF_REASON_LABELS: Record<HandoffReason, string> = {
  USER_REQUEST: '用户显式请求',
  EMOTION: '情绪升级',
  UNRESOLVED: '多轮未解决',
  HIGH_RISK: '高风险意图',
  SYSTEM_ERROR: '系统异常',
}

/** 工单状态 */
export const TICKET_STATUSES = ['OPEN', 'PROCESSING', 'CLOSED'] as const
export type TicketStatus = (typeof TICKET_STATUSES)[number]

export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  OPEN: '待处理',
  PROCESSING: '处理中',
  CLOSED: '已关闭',
}

/** 优先级 */
export const PRIORITIES = ['LOW', 'NORMAL', 'HIGH', 'URGENT'] as const
export type Priority = (typeof PRIORITIES)[number]

export const PRIORITY_LABELS: Record<Priority, string> = {
  LOW: '低',
  NORMAL: '普通',
  HIGH: '高',
  URGENT: '紧急',
}

/** 文本来源 */
export const TEXT_SOURCES = ['ORIGINAL', 'ASR', 'OCR', 'VLM', 'CLASSIFIER'] as const
export type TextSource = (typeof TEXT_SOURCES)[number]

export const TEXT_SOURCE_LABELS: Record<TextSource, string> = {
  ORIGINAL: '原文',
  ASR: '语音转写',
  OCR: '图片识别',
  VLM: '图像理解',
  CLASSIFIER: '分类器',
}

/** 无依据决策 */
export const DECISIONS = ['DIRECT_ANSWER', 'WEAK', 'NO_EVIDENCE'] as const
export type Decision = (typeof DECISIONS)[number]

export const DECISION_LABELS: Record<Decision, string> = {
  DIRECT_ANSWER: '有依据直答',
  WEAK: '弱依据回复',
  NO_EVIDENCE: '无依据拒答',
}

/** 审核状态 */
export const REVIEW_STATUSES = ['PENDING', 'APPROVED', 'REJECTED', 'DISABLED'] as const
export type ReviewStatus = (typeof REVIEW_STATUSES)[number]

export const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  PENDING: '待审核',
  APPROVED: '已通过',
  REJECTED: '已驳回',
  DISABLED: '已下架',
}

/** 情绪 */
export const EMOTIONS = ['NEUTRAL', 'DISSATISFIED', 'ANGRY'] as const
export type Emotion = (typeof EMOTIONS)[number]

export const EMOTION_LABELS: Record<Emotion, string> = {
  NEUTRAL: '中性',
  DISSATISFIED: '不满',
  ANGRY: '愤怒',
}

/** 会话归属方 */
export type SessionOwner = 'BOT' | 'AGENT'

/** 情绪强度（2.5.2 context.emotion.intensity 为中文） */
export type EmotionIntensity = '轻度' | '中度' | '强烈'

// ─────────────────────────────────────────────────────────────
// 会话与消息（FE-01 / FE-02 / FE-03 / FE-10 / FE-14）
// ─────────────────────────────────────────────────────────────

/** 会话列表项 —— GET /bff/v1/sessions */
export interface SessionSummary {
  session_id: string
  /** 脱敏后的买家标识（4.5.2：姓名 张**、手机号 138****8888） */
  buyer_display: string
  buyer_id: string
  channel: Channel
  owner: SessionOwner
  /** 接管坐席 ID，owner=BOT 时为 null */
  agent_id: string | null
  agent_name: string | null
  /** 是否已被人工接管 */
  taken_over: boolean
  /** 机器人是否已静音（接管后为 true，FE-03） */
  bot_muted: boolean
  group_id: string
  group_name: string
  /** 最后一条消息摘要 */
  last_message_preview: string
  last_message_at: string
  /** 最后一条消息发送方，用于列表气泡着色 */
  last_sender: Sender
  /** 等待坐席回复时长（秒），用于超时告警 */
  waiting_seconds: number
  unread_count: number
  turn_count: number
  /** 关联工单号，无则为 null */
  ticket_no: string | null
  /** 命中过的意图标签（用于列表快速判断） */
  intents: string[]
}

/** 会话列表查询参数 —— GET /bff/v1/sessions */
export interface SessionListQuery {
  channel?: Channel | ''
  status?: 'ALL' | 'BOT' | 'AGENT' | 'WAITING'
  taken_over?: boolean | null
  keyword?: string
  group_id?: string
  start_time?: string
  end_time?: string
  page?: number
  page_size?: number
}

/** 单条消息 */
export interface ChatMessage {
  message_id: string
  session_id: string
  sender: Sender
  msg_type: MsgType
  /** 文本内容；IMAGE 类型时为图片说明或空 */
  text: string
  /** 文本来源，用于区分 ASR / OCR（FE-01 五种 msg_type） */
  text_source: TextSource
  /** 图片消息的本地资源标识；HC-1 禁止外链，故为同源相对路径或 null */
  image_ref: string | null
  image_alt: string | null
  created_at: string
  /** 该消息所属轮次的 trace_id，用于与回放页对齐（FE-10） */
  trace_id: string | null
  /** 该消息命中的知识条目 ID（BOT 消息才有） */
  chunk_ids: string[]
  /** 无依据决策口径 */
  decision: Decision | null
  /** 若本条是转人工卡片，记录原因 */
  handoff_reason: HandoffReason | null
  /** 本条消息的端到端耗时（毫秒） */
  latency_ms: number | null
  /** 是否已被坐席标记为「这条答错了」（FE-08） */
  feedback_marked: boolean
}

/** 会话详情 —— GET /bff/v1/sessions/{session_id} */
export interface SessionDetail {
  session: SessionSummary
  messages: ChatMessage[]
  /** 买家侧上下文（不含任何敏感字段，HC-2） */
  buyer: {
    buyer_id: string
    buyer_display: string
    channel: Channel
    /** 脱敏地址：仅省市（4.5.2） */
    region: string
    member_level: string
    first_seen_at: string
  }
}

// ── FE-02：AI 上下文卡片七要素 ────────────────────────────────

/** 已验证事实（订单 / 物流 / 商品） */
export interface VerifiedFact {
  type: 'order' | 'logistics' | 'product' | 'policy'
  /** 展示标题，如订单号 / 运单号 */
  label: string
  /** 事实值 */
  value: string
  /** 订单号等需要脱敏的字段由后端已脱敏；前端不再二次处理 */
  order_no?: string
  status?: OrderStatus | LogisticsStatus | string
  /** 物流停滞天数（2.5.2 示例字段） */
  stalled_days?: number
  /** 数据来源接口，便于坐席判断时效（HC-3：会变的数字走接口） */
  source?: string
  /** 该事实的取数时间 */
  fetched_at?: string
  // ── 以下为 FE-14 卡片渲染所需的**可选**结构化明细 ──
  // 任务书 2.5.2 的 context 示例里，verified 元素本身就是
  // 「type + 若干该类型的字段」（order 带 order_no/status，logistics 带 latest/stalled_days），
  // 即契约对每个 type 携带的字段是开放的。FE-14 要求渲染订单卡片、
  // 商品卡片与物流时间轴，且明确「物流 traces[].desc 原文展示不改写」——
  // 既然要原样展示 traces，就必须让它随事实一起下发，而不是让前端去解析 value 文本。
  // 全部可选：后端只给 value 时，卡片退化为纯文本展示（见 OrderTextCard 的纯文本通道）。
  /** 快递公司编码（type=logistics） */
  company?: CompanyCode
  /** 运单号（type=logistics） */
  tracking_no?: string
  /** 最新物流描述（2.5.2 示例字段名，type=logistics） */
  latest?: string
  /** 最新物流时间（type=logistics） */
  latest_at?: string
  /** 物流轨迹，按后端顺序下发，前端展示时倒序（type=logistics） */
  traces?: Array<{ time: string; desc: string; status?: LogisticsStatus; location?: string }>
  /** 预计送达（type=logistics，YYYY-MM-DD） */
  estimated_delivery?: string
  /** 订单商品行（type=order） */
  items?: Array<{ title: string; spec?: string; quantity: number; price?: number; image_ref?: string | null }>
  /** 订单金额（type=order） */
  total_amount?: number
  /** 商品 SKU（type=product） */
  sku_id?: string
  /** 商品规格（type=product） */
  spec?: string
  /** 商品材质（type=product） */
  material?: string
  /** 库存状态（type=product，HC-3：会变的数字走接口） */
  stock_status?: StockStatus
  /** 预售天数（type=product，stock_status=PRESALE 时有值） */
  presale_days?: number
  /** 现价（type=product） */
  price?: number
  /** 吊牌价（type=product） */
  original_price?: number
  /** 商品图（type=product，同源相对路径） */
  image_ref?: string | null
}

/** 已尝试动作 */
export interface TriedAction {
  action: string
  ok: boolean
  /** 失败原因 / 结果说明 */
  detail?: string
  at?: string
}

/** AI 上下文卡片 —— GET /bff/v1/sessions/{session_id}/context */
export interface SessionContext {
  session_id: string
  buyer_id: string
  channel: Channel
  waiting_seconds: number
  /** 要素 1：对话摘要 */
  summary: string
  /** 要素 2：已验证事实 */
  verified: VerifiedFact[]
  /** 要素 3：已尝试动作 */
  tried_actions: TriedAction[]
  /** 要素 4：未满足诉求（警示色高亮） */
  unmet_demand: string
  /** 要素 5：建议动作 */
  suggested_action: string
  /** 要素 6：情绪 */
  emotion: {
    level: Emotion
    intensity: EmotionIntensity
    /** 触发情绪的原文片段，便于坐席快速定位 */
    trigger?: string
  }
  /** 要素 7：轮次数 */
  turn_count: number
  attachments: Array<{ type: string; tag: string; url: string | null }>
  ticket_no: string | null
  /** 内部工单页地址 —— HC-1 要求不渲染为可点击外链，仅作展示标识 */
  handoff_url: string | null
}

/** 七要素字段名 —— FE-02 完整性守卫的检查清单 */
export const CONTEXT_REQUIRED_FIELDS = [
  'summary',
  'verified',
  'tried_actions',
  'unmet_demand',
  'suggested_action',
  'emotion',
  'turn_count',
] as const
export type ContextRequiredField = (typeof CONTEXT_REQUIRED_FIELDS)[number]

// ── FE-03：接管与释放 ────────────────────────────────────────

export interface TakeoverRequest {
  agent_id: string
  ticket_no?: string
}

export interface TakeoverResponse {
  session_id: string
  owner: SessionOwner
  agent_id: string
  agent_name: string
  taken_over_at: string
  bot_muted: boolean
}

export interface ReleaseResponse {
  session_id: string
  owner: SessionOwner
  released_at: string
  bot_muted: boolean
}

// ── FE-10：会话回放 ─────────────────────────────────────────

/** 单跳耗时（瀑布图一段） */
export interface LatencySpan {
  /** 跳名称，如「意图识别」「向量检索」「LLM 生成」「商家订单接口」 */
  name: string
  /** 相对该轮起点的偏移毫秒 */
  offset_ms: number
  duration_ms: number
  /** 该跳的类别，用于着色 */
  kind: 'asr' | 'intent' | 'retrieval' | 'tool' | 'llm' | 'tts' | 'other'
  /** 是否命中缓存 */
  cached?: boolean
}

/** 单轮回放详情 */
export interface ReplayTurn {
  trace_id: string
  turn_index: number
  started_at: string
  total_ms: number
  /** 买家输入 */
  user_input: string
  /** 机器人/坐席输出 */
  reply: string
  sender: Sender
  intent: string
  intent_confidence: number
  decision: Decision
  /** 检索命中 */
  retrieval: {
    query: string
    hits: Array<{ chunk_id: string; score: number; title: string; category: string }>
  }
  /** 工具调用 */
  tool_calls: Array<{
    tool: string
    params: Record<string, string | number>
    ok: boolean
    duration_ms: number
    error?: string
  }>
  /** 耗时瀑布 */
  spans: LatencySpan[]
  /** 该轮是否触发转人工 */
  handoff: { reason: HandoffReason; ticket_no: string } | null
}

export interface ReplayResponse {
  session_id: string
  channel: Channel
  buyer_display: string
  started_at: string
  ended_at: string
  turns: ReplayTurn[]
}

// ─────────────────────────────────────────────────────────────
// 工单（FE-02）
// ─────────────────────────────────────────────────────────────

export interface TicketSummary {
  ticket_no: string
  session_id: string
  buyer_display: string
  channel: Channel
  reason: HandoffReason
  priority: Priority
  status: TicketStatus
  /** 工单摘要，通常取 AI 上下文的 unmet_demand */
  subject: string
  assignee: string | null
  group_name: string
  created_at: string
  updated_at: string
  /** 已等待时长（秒），用于超时高亮 */
  waiting_seconds: number
  sla_breached: boolean
}

export interface TicketDetail extends TicketSummary {
  /** 触发转人工的上下文快照（转接那一刻的七要素） */
  context_snapshot: SessionContext | null
  timeline: Array<{
    at: string
    actor: string
    action: string
    detail: string
  }>
  /** 关联知识条目（坐席处理时引用的） */
  related_chunk_ids: string[]
}

export interface TicketListQuery {
  reason?: HandoffReason | ''
  priority?: Priority | ''
  status?: TicketStatus | ''
  keyword?: string
  page?: number
  page_size?: number
}

// ─────────────────────────────────────────────────────────────
// 知识库（FE-04 / FE-05 / FE-06 / FE-07 / FE-08）
// ─────────────────────────────────────────────────────────────

/** 知识类型 —— 决定切分长度区间（FE-06） */
export const KB_TYPES = ['PRODUCT', 'POLICY', 'FAQ', 'LONG_DOC'] as const
export type KbType = (typeof KB_TYPES)[number]

export const KB_TYPE_LABELS: Record<KbType, string> = {
  PRODUCT: '商品知识',
  POLICY: '政策规则',
  FAQ: 'FAQ',
  LONG_DOC: '长文档',
}

/**
 * 切分长度区间（任务书 FE-06 明确给出）：
 * 商品 200–400 / 政策 100–300 / FAQ 50–200 / 长文档 300–500，重叠 15%。
 */
export const CHUNK_LENGTH_RANGE: Record<KbType, { min: number; max: number }> = {
  PRODUCT: { min: 200, max: 400 },
  POLICY: { min: 100, max: 300 },
  FAQ: { min: 50, max: 200 },
  LONG_DOC: { min: 300, max: 500 },
}

export const CHUNK_OVERLAP_RATIO = 0.15

export interface KbChunk {
  chunk_id: string
  title: string
  /** 纯 Markdown 正文 */
  content: string
  kb_type: KbType
  category: string
  /** 关联 SKU，用于商品知识追溯 */
  sku_ids: string[]
  /** 权威来源，如「商品主数据表·洗护方式字段」 */
  authority: string
  review_status: ReviewStatus
  reviewer: string | null
  review_comment: string | null
  /** 生效区间 */
  effective_from: string | null
  effective_to: string | null
  channel: Channel | 'ALL'
  version: number
  created_by: string
  updated_by: string
  created_at: string
  updated_at: string
  /** 字数，用于 FE-06 预览与超长/过短标记 */
  char_count: number
}

export interface KbChunkListQuery {
  category?: string
  sku_id?: string
  review_status?: ReviewStatus | ''
  channel?: Channel | 'ALL' | ''
  kb_type?: KbType | ''
  keyword?: string
  page?: number
  page_size?: number
}

export type KbChunkPayload = Pick<
  KbChunk,
  | 'title'
  | 'content'
  | 'kb_type'
  | 'category'
  | 'sku_ids'
  | 'authority'
  | 'effective_from'
  | 'effective_to'
  | 'channel'
>

export interface KbReviewPayload {
  decision: Extract<ReviewStatus, 'APPROVED' | 'REJECTED'>
  comment: string
  reviewer: string
}

/** FE-04 坐席侧检索 —— POST /bff/v1/kb/search */
export interface KbSearchHit {
  chunk_id: string
  title: string
  /** 命中片段，含高亮标记 */
  snippet: string
  /** 完整原文，供「一键填入」使用（不改原文） */
  content: string
  score: number
  category: string
  authority: string
  kb_type: KbType
  review_status: ReviewStatus
  /** 召回该条的路由，用于坐席判断可信度 */
  matched_by: 'vector' | 'keyword' | 'sku' | 'faq'
}

export interface KbSearchRequest {
  query: string
  category?: string
  top_k?: number
  /**
   * 发起检索的会话 ID（可选）。
   * 用途：检索失败时后端可关联到具体会话，用于「检索失败」转人工规则的判定与埋点，
   * 不参与召回计算，因此可选。
   */
  session_id?: string
}

export interface KbSearchResponse {
  query: string
  hits: KbSearchHit[]
  /** 检索耗时，用于 P95 ≤500ms 的可观测性 */
  latency_ms: number
  /** 无依据时后端明确返回，前端据此提示拒答（HC-3） */
  no_evidence: boolean
}

// ── FE-06 批量导入 ──────────────────────────────────────────

export interface ImportPreviewChunk {
  index: number
  title: string
  content: string
  char_count: number
  /** 是否超出该知识类型的目标长度区间 */
  length_flag: 'OK' | 'TOO_SHORT' | 'TOO_LONG'
  /** 切分来源行号/段落号，便于回溯 */
  source_ref: string
}

export interface ImportPreviewResponse {
  import_id: string
  file_name: string
  detected_type: KbType
  total_chunks: number
  chunks: ImportPreviewChunk[]
  /** 重叠比例，后端按 15% 处理 */
  overlap_ratio: number
  /** 自动识别到的知识类型候选与置信度 */
  type_candidates: Array<{ kb_type: KbType; confidence: number }>
  warnings: string[]
}

export interface ImportCommitRequest {
  import_id: string
  /** 人工确认后的知识类型（可覆盖自动识别结果） */
  kb_type: KbType
  category: string
  authority: string
  channel: Channel | 'ALL'
  /** 人工在预览里剔除的切分单元下标 */
  excluded_indexes: number[]
}

export interface ImportFailure {
  row: number
  reason: string
  raw: string
}

export interface ImportProgress {
  import_id: string
  status: 'RUNNING' | 'DONE' | 'PARTIAL_FAILED' | 'FAILED'
  total: number
  succeeded: number
  failed: number
  failures: ImportFailure[]
  started_at: string
  finished_at: string | null
}

// ── FE-07 版本与生效时间 ─────────────────────────────────────

export interface KbVersion {
  version: number
  chunk_id: string
  title: string
  content: string
  review_status: ReviewStatus
  effective_from: string | null
  effective_to: string | null
  updated_by: string
  updated_at: string
  change_note: string
}

export interface KbHistoryResponse {
  chunk_id: string
  current_version: number
  versions: KbVersion[]
}

// ── FE-08 纠错闭环 ──────────────────────────────────────────

export const FEEDBACK_ERROR_TYPES = [
  'FACT_ERROR',
  'OUTDATED',
  'POLICY_MISMATCH',
  'IRRELEVANT',
] as const
export type FeedbackErrorType = (typeof FEEDBACK_ERROR_TYPES)[number]

export const FEEDBACK_ERROR_TYPE_LABELS: Record<FeedbackErrorType, string> = {
  FACT_ERROR: '事实错误',
  OUTDATED: '话术过时',
  POLICY_MISMATCH: '口径错误',
  IRRELEVANT: '答非所问',
}

/** 商家历史错误类型分布（任务书 FE-08 给出，用于对比） */
export const MERCHANT_HISTORICAL_DISTRIBUTION: Record<FeedbackErrorType, number> = {
  FACT_ERROR: 40.5,
  OUTDATED: 24.8,
  IRRELEVANT: 20.3,
  POLICY_MISMATCH: 14.4,
}

export interface FeedbackItem {
  feedback_id: string
  message_id: string
  session_id: string
  chunk_ids: string[]
  error_type: FeedbackErrorType
  comment: string
  submitted_by: string
  submitted_at: string
  status: 'OPEN' | 'ASSIGNED' | 'RESOLVED' | 'CLOSED'
  assignee: string | null
  /** 处理时关联并修改的知识条目 */
  resolved_chunk_id: string | null
  resolution: string | null
}

export interface FeedbackCreateRequest {
  message_id: string
  session_id: string
  chunk_ids: string[]
  error_type: FeedbackErrorType
  comment: string
}

export interface FeedbackListQuery {
  error_type?: FeedbackErrorType | ''
  status?: FeedbackItem['status'] | ''
  assignee?: string
  page?: number
  page_size?: number
}

// ─────────────────────────────────────────────────────────────
// 指标（FE-09）
// ─────────────────────────────────────────────────────────────

/** 12 项验收指标 ID —— 与任务书 FE-09 逐条对应 */
export const METRIC_IDS = [
  'INTENT_ACCURACY',
  'RAG_FACT_ACCURACY',
  'NO_EVIDENCE_REFUSAL',
  'ASR_ACCURACY',
  'PRODUCT_IMAGE_TOPK3',
  'ORDER_QUERY_SUCCESS',
  'FIRST_RESPONSE_P95',
  'E2E_RESOLUTION',
  'HANDOFF_RATE',
  'HANDOFF_SUCCESS',
  'CONTEXT_COMPLETENESS',
  'AVAILABILITY',
] as const
export type MetricId = (typeof METRIC_IDS)[number]

export interface MetricDefinition {
  metric_id: MetricId
  name: string
  /** 单位：% / s / ms */
  unit: string
  /** 目标值 */
  target: number
  /** 比较方向：GTE 表示越大越好，LTE 表示越小越好 */
  direction: 'GTE' | 'LTE'
  /** 当前值 */
  current: number
  /** 商家历史基线（如有） */
  baseline: number | null
  /** 达标状态 —— 等于目标值算达标（FE-09 DoD） */
  status: 'OK' | 'WARNING' | 'FAIL'
  /** 口径说明，坐席主管需要知道这个数怎么算出来的 */
  definition: string
  /** 取数时间 */
  measured_at: string
}

export interface MetricsOverviewResponse {
  metrics: MetricDefinition[]
  /** 统计区间 */
  range: { start: string; end: string }
  /** 数据是否完整（部分指标可能因样本不足缺失） */
  data_complete: boolean
  /** 样本量，避免小样本误判 */
  sample_size: number
}

export interface MetricsTrendPoint {
  /** 日期（YYYY-MM-DD）或周标识（YYYY-Www） */
  bucket: string
  values: Partial<Record<MetricId, number>>
}

export interface MetricsTrendResponse {
  granularity: 'DAY' | 'WEEK'
  metric_ids: MetricId[]
  points: MetricsTrendPoint[]
}

export interface MetricsChannelDistribution {
  channel: Channel
  sessions: number
  handoff: number
  handoff_rate: number
}

export interface MetricsIntentDistribution {
  intent: string
  count: number
  accuracy: number
}

/** 转人工漏斗（FE-09 HandoffFunnel） */
export interface HandoffFunnelStage {
  stage: string
  count: number
  /** 相对上一阶段的转化率（%） */
  conversion: number
}

export interface MetricsDistributionResponse {
  channels: MetricsChannelDistribution[]
  intents: MetricsIntentDistribution[]
  funnel: HandoffFunnelStage[]
}

// ─────────────────────────────────────────────────────────────
// 配置（FE-11 / FE-12 / FE-13）
// ─────────────────────────────────────────────────────────────

/** FE-11 渠道授权 */
export type AuthStatus = 'AUTHORIZED' | 'EXPIRING' | 'EXPIRED' | 'PENDING'

export const AUTH_STATUS_LABELS: Record<AuthStatus, string> = {
  AUTHORIZED: '已授权',
  EXPIRING: '即将到期',
  EXPIRED: '已过期',
  PENDING: '待授权',
}

export interface ChannelAuth {
  channel: Channel
  auth_status: AuthStatus
  /** 权限包列表，如「订单查询」「发货」「退款」 */
  permission_packages: string[]
  /** 授权到期日（YYYY-MM-DD） */
  expires_at: string | null
  /** 距到期天数，负数表示已过期 */
  days_to_expiry: number | null
  /** 回调地址配置状态（不展示完整 URL 中的密钥部分） */
  callback_configured: boolean
  callback_url_masked: string
  /** 出口 IP 白名单配置状态 */
  ip_whitelist_configured: boolean
  ip_whitelist_count: number
  /** 抖音小店服务商认证状态（关键路径 10 工作日） */
  service_provider_certified: boolean | null
  service_provider_note: string | null
  /** 最近一次状态更新时间 */
  updated_at: string
  updated_by: string
}

/** FE-12 坐席分组与排班 */
export interface AgentGroup {
  group_id: string
  group_name: string
  service_scope: string
  agent_count: number
  supervisor: string
  /** 7×24 时段表：index 0 = 周一，slot 为 24 小时布尔数组 */
  schedule: boolean[][]
  channels: Channel[]
  daily_volume: number
  peak_volume: number
  note: string
}

export interface AgentGroupsResponse {
  groups: AgentGroup[]
  /** 时段表头，便于前端与后端对齐口径 */
  slot_labels: string[]
  updated_at: string
}

/** FE-13 转人工规则 */
export interface HandoffRule {
  rule_id: string
  /** 6 条规则：显式请求 / 情绪 / 未解决 / 高风险 / 检索失败 / 系统异常 */
  code:
    | 'EXPLICIT_REQUEST'
    | 'EMOTION'
    | 'UNRESOLVED'
    | 'HIGH_RISK'
    | 'RETRIEVAL_FAILED'
    | 'SYSTEM_ERROR'
  name: string
  description: string
  enabled: boolean
  priority: number
  /** 规则参数，如「连续 N 轮负面」的 N */
  params: Array<{
    key: string
    label: string
    value: number | string | boolean
    type: 'number' | 'select' | 'boolean'
    /** number 类型的取值范围 */
    min?: number
    max?: number
    unit?: string
    options?: Array<{ label: string; value: string | number }>
  }>
  /**
   * 是否锁定「不得挽留」：任务书 FE-13 要求「显式请求」与「高风险意图」
   * 不允许降级到挽留分支，UI 必须锁定为只读。
   */
  no_retention_locked: boolean
  updated_at: string
  updated_by: string
}

export interface HandoffRulesResponse {
  rules: HandoffRule[]
  /** 试算样本量（抽样 50 条与后端一致） */
  test_sample_size: number
}

export interface HandoffRuleTestRequest {
  /** 被测规则集（未保存的草稿也可试算） */
  rules: HandoffRule[]
  sample_size: number
}

export interface HandoffRuleTestResult {
  sample_size: number
  matched: number
  /** 每条规则命中数 */
  per_rule: Array<{ rule_id: string; name: string; hits: number }>;
  /** 与后端当前线上配置的差异条数 */
  diff_count: number
  details: Array<{
    session_id: string
    matched_rule: string | null
    reason: string
  }>
}

// ─────────────────────────────────────────────────────────────
// 对话（FE-01 坐席台内发送）
// ─────────────────────────────────────────────────────────────

export interface ChatTurnRequest {
  session_id: string
  text: string
  msg_type: MsgType
  /** 坐席发送时固定为 AGENT */
  sender: Sender
  image_ref?: string | null
}

export interface ChatTurnResponse {
  message_id: string
  session_id: string
  created_at: string
  trace_id: string
}

// ─────────────────────────────────────────────────────────────
// SSE 事件（2.5.4）—— 前端唯一流式通道
// ─────────────────────────────────────────────────────────────

export interface SseTurnStart {
  trace_id: string
  session_id: string
}

export interface SseIntent {
  intent: string
  confidence: number
}

export interface SseRetrieval {
  hits: number
  top_score: number
}

export interface SseDelta {
  text: string
}

export interface SseAction {
  action: string
  status: string
}

export interface SseTurnEnd {
  reply: string
  actions: string[]
  latency_ms: number
}

export interface SseHandoff {
  reason: HandoffReason
  ticket_no: string
  queue_position: number
}

export interface SseError {
  code: number
  message: string
}

/** SSE 事件联合类型，判别字段为 event 名 */
export type SseEvent =
  | { type: 'turn.start'; data: SseTurnStart }
  | { type: 'intent'; data: SseIntent }
  | { type: 'retrieval'; data: SseRetrieval }
  | { type: 'delta'; data: SseDelta }
  | { type: 'action'; data: SseAction }
  | { type: 'turn.end'; data: SseTurnEnd }
  | { type: 'handoff'; data: SseHandoff }
  | { type: 'error'; data: SseError }

export type { Paged }
