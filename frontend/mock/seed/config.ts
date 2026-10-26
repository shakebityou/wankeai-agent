/**
 * 渠道授权与转人工规则 Mock —— FE-11 / FE-13。
 *
 * 合规红线（4.5.2）：渠道页**不展示任何 AppSecret / Token 明文**，
 * 因此这里只提供 callback_url_masked（域名 + 路径，密钥段以 *** 遮蔽）
 * 与布尔型的配置状态，绝不生成真实凭证字段。
 */
import type { AuthStatus, Channel, ChannelAuth, HandoffRule } from '../../src/types/contract'
import type { SeededRandom } from './random'

export interface MockChannel extends ChannelAuth {}

export interface MockRule extends HandoffRule {}

export interface ChannelChangeLogEntry {
  at: string
  actor: string
  detail: string
}

const DAY_MS = 86_400_000

/** 生成相对今天偏移 n 天的 YYYY-MM-DD */
function dateFromNow(days: number): string {
  return new Date(Date.now() + days * DAY_MS).toISOString().slice(0, 10)
}

function daysUntil(date: string | null): number | null {
  if (!date) return null
  return Math.ceil((Date.parse(`${date}T23:59:59Z`) - Date.now()) / DAY_MS)
}

/**
 * 5 个渠道的授权状态刻意覆盖 4 种状态，便于演示 FE-11 的
 * 到期提醒（15 天阈值）与 AuthStatusTag 的四种配色。
 */
const CHANNEL_SEEDS: Array<{
  channel: Channel
  auth_status: AuthStatus
  permission_packages: string[]
  expires_in_days: number | null
  callback_configured: boolean
  callback_host: string
  ip_whitelist_configured: boolean
  ip_whitelist_count: number
  service_provider_certified: boolean | null
  service_provider_note: string | null
  updated_by: string
}> = [
  {
    channel: 'TAOBAO',
    auth_status: 'AUTHORIZED',
    permission_packages: ['订单查询', '发货', '退款', '物流查询', '商品查询'],
    expires_in_days: 268,
    callback_configured: true,
    callback_host: 'kf.wankeai.example.com',
    ip_whitelist_configured: true,
    ip_whitelist_count: 3,
    service_provider_certified: null,
    service_provider_note: null,
    updated_by: '系统管理员·周航',
  },
  {
    channel: 'TMALL',
    auth_status: 'AUTHORIZED',
    permission_packages: ['订单查询', '发货', '退款', '物流查询', '商品查询', '评价查询'],
    expires_in_days: 191,
    callback_configured: true,
    callback_host: 'kf.wankeai.example.com',
    ip_whitelist_configured: true,
    ip_whitelist_count: 3,
    service_provider_certified: null,
    service_provider_note: null,
    updated_by: '系统管理员·周航',
  },
  {
    channel: 'PDD',
    auth_status: 'EXPIRING',
    permission_packages: ['订单查询', '发货', '物流查询'],
    // 落在 15 天提醒窗口内，用于验证 ExpiryAlert
    expires_in_days: 9,
    callback_configured: true,
    callback_host: 'kf.wankeai.example.com',
    ip_whitelist_configured: true,
    ip_whitelist_count: 2,
    service_provider_certified: null,
    service_provider_note: null,
    updated_by: '系统管理员·周航',
  },
  {
    channel: 'DOUYIN',
    auth_status: 'PENDING',
    permission_packages: ['订单查询', '商品查询'],
    expires_in_days: null,
    callback_configured: false,
    callback_host: 'kf.wankeai.example.com',
    ip_whitelist_configured: false,
    ip_whitelist_count: 0,
    // 关键路径：抖音小店服务商认证 10 个工作日
    service_provider_certified: false,
    service_provider_note: '服务商认证材料已提交，平台审核中，预计 10 个工作日完成',
    updated_by: '系统管理员·周航',
  },
  {
    channel: 'WECHAT',
    auth_status: 'EXPIRED',
    permission_packages: ['消息收发'],
    expires_in_days: -12,
    callback_configured: true,
    callback_host: 'kf.wankeai.example.com',
    ip_whitelist_configured: true,
    ip_whitelist_count: 1,
    service_provider_certified: null,
    service_provider_note: null,
    updated_by: '系统管理员·周航',
  },
]

export function buildChannels(_random: SeededRandom): MockChannel[] {
  void _random
  return CHANNEL_SEEDS.map((seed) => {
    const expiresAt = seed.expires_in_days === null ? null : dateFromNow(seed.expires_in_days)
    return {
      channel: seed.channel,
      auth_status: seed.auth_status,
      permission_packages: seed.permission_packages,
      expires_at: expiresAt,
      days_to_expiry: daysUntil(expiresAt),
      callback_configured: seed.callback_configured,
      // 只暴露回调域名与路径，密钥段一律遮蔽（HC-2 / 4.5.2）
      callback_url_masked: seed.callback_configured
        ? `https://${seed.callback_host}/bff/v1/channels/${seed.channel.toLowerCase()}/callback?token=***`
        : '未配置',
      ip_whitelist_configured: seed.ip_whitelist_configured,
      ip_whitelist_count: seed.ip_whitelist_count,
      service_provider_certified: seed.service_provider_certified,
      service_provider_note: seed.service_provider_note,
      updated_at: new Date(Date.now() - 2 * DAY_MS).toISOString(),
      updated_by: seed.updated_by,
    }
  })
}

/**
 * 6 条转人工规则（FE-13）。
 * priority 数字越小越优先；「显式请求」与「高风险意图」必须排在最前，
 * 且 no_retention_locked = true —— UI 侧锁定为只读，不允许降级到挽留分支。
 */
export function buildHandoffRules(): MockRule[] {
  const now = new Date(Date.now() - DAY_MS).toISOString()
  return [
    {
      rule_id: 'HR-01',
      code: 'EXPLICIT_REQUEST',
      name: '买家显式要求人工',
      description: '识别到「转人工」「找客服」「真人」「人工客服」等明确诉求时，直接转人工，不做任何挽留。',
      enabled: true,
      priority: 1,
      params: [
        {
          key: 'keywords',
          label: '触发关键词',
          value: '转人工,找人工,人工客服,真人,转客服',
          type: 'select',
          options: [
            { label: '默认词库（5 个）', value: '转人工,找人工,人工客服,真人,转客服' },
            { label: '扩展词库（9 个）', value: '转人工,找人工,人工客服,真人,转客服,叫个人,人工,活人,找真人' },
          ],
        },
      ],
      no_retention_locked: true,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
    {
      rule_id: 'HR-02',
      code: 'HIGH_RISK',
      name: '高风险意图',
      description: '涉及投诉、工商/平台举报、法律纠纷、媒体曝光、人身安全等高风险意图，直接转人工并标记紧急。',
      enabled: true,
      priority: 2,
      params: [
        {
          key: 'risk_levels',
          label: '命中等级',
          value: 'HIGH',
          type: 'select',
          options: [
            { label: '高风险及以上', value: 'HIGH' },
            { label: '中风险及以上', value: 'MEDIUM' },
          ],
        },
        {
          key: 'auto_urgent',
          label: '自动标记紧急',
          value: true,
          type: 'boolean',
        },
      ],
      no_retention_locked: true,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
    {
      rule_id: 'HR-03',
      code: 'EMOTION',
      name: '连续负面情绪',
      description: '买家连续多轮出现负面情绪且强度递增时转人工，避免情绪升级。',
      enabled: true,
      priority: 3,
      params: [
        {
          key: 'consecutive_turns',
          label: '连续负面轮数 N',
          value: 3,
          type: 'number',
          min: 1,
          max: 10,
          unit: '轮',
        },
        {
          key: 'emotion_level',
          label: '触发情绪等级',
          value: 'DISSATISFIED',
          type: 'select',
          options: [
            { label: '不满及以上', value: 'DISSATISFIED' },
            { label: '仅愤怒', value: 'ANGRY' },
          ],
        },
      ],
      no_retention_locked: false,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
    {
      rule_id: 'HR-04',
      code: 'UNRESOLVED',
      name: '多轮未解决',
      description: '同一诉求连续多轮未解决、或买家重复追问同一问题时转人工。',
      enabled: true,
      priority: 4,
      params: [
        {
          key: 'max_turns',
          label: '最大轮次上限',
          value: 6,
          type: 'number',
          min: 2,
          max: 20,
          unit: '轮',
        },
        {
          key: 'repeat_detection',
          label: '重复追问检测',
          value: true,
          type: 'boolean',
        },
      ],
      no_retention_locked: false,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
    {
      rule_id: 'HR-05',
      code: 'RETRIEVAL_FAILED',
      name: '知识检索失败',
      description: '连续检索无命中或最高命中分低于阈值时转人工，避免无依据作答。',
      enabled: true,
      priority: 5,
      params: [
        {
          key: 'min_top_score',
          label: '最低命中分阈值',
          value: 0.62,
          type: 'number',
          min: 0.3,
          max: 0.95,
          unit: '',
        },
        {
          key: 'consecutive_failures',
          label: '连续失败次数',
          value: 2,
          type: 'number',
          min: 1,
          max: 5,
          unit: '次',
        },
      ],
      no_retention_locked: false,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
    {
      rule_id: 'HR-06',
      code: 'SYSTEM_ERROR',
      name: '系统异常兜底',
      description: '下游接口或模型连续不可用时转人工，并展示兜底话术。',
      enabled: true,
      priority: 6,
      params: [
        {
          key: 'error_threshold',
          label: '连续异常次数',
          value: 2,
          type: 'number',
          min: 1,
          max: 5,
          unit: '次',
        },
        {
          key: 'fallback_message',
          label: '兜底话术',
          value: '抱歉，系统暂时有点忙，正在为您转接人工客服，请稍等片刻～',
          type: 'select',
          options: [
            { label: '标准兜底话术', value: '抱歉，系统暂时有点忙，正在为您转接人工客服，请稍等片刻～' },
            { label: '简洁兜底话术', value: '正在为您转接人工客服，请稍等。' },
          ],
        },
      ],
      no_retention_locked: false,
      updated_at: now,
      updated_by: '系统管理员·周航',
    },
  ]
}
