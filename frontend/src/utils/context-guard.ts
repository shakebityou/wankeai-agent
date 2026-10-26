import type { SessionContext, ContextRequiredField } from '@/types/contract'
import { CONTEXT_REQUIRED_FIELDS } from '@/types/contract'

/**
 * AI 上下文七要素完整性守卫 —— FE-02 的核心 DoD。
 *
 * 任务书原文：
 *   「前端做字段完整性守卫，缺失时展示明确错误而非空白」
 *   「字段缺失展示『上下文生成异常，请联系技术支持』并上报埋点，不允许静默空白」
 *
 * 设计取舍：守卫放在这里而不是组件里，是为了让 ContextCard 与工单详情页
 * （context_snapshot）共用同一判定 —— 工单页展示的是转接那一刻的快照，
 * 同样可能缺字段，必须用同一口径报警。
 */

export const CONTEXT_ERROR_MESSAGE = '上下文生成异常，请联系技术支持'

export interface ContextGuardResult {
  ok: boolean
  /** 缺失或非法的要素字段名 */
  missing: ContextRequiredField[]
  /** 面向坐席的中文缺失说明 */
  missingLabels: string[]
  /** 守卫通过时返回强类型上下文，否则为 null */
  context: SessionContext | null
}

const FIELD_LABELS: Record<ContextRequiredField, string> = {
  summary: '对话摘要',
  verified: '已验证事实',
  tried_actions: '已尝试动作',
  unmet_demand: '未满足诉求',
  suggested_action: '建议动作',
  emotion: '情绪',
  turn_count: '轮次数',
}

/**
 * 判定单个要素是否「有值」。
 *
 * 口径说明：
 *   - 字符串：空串与纯空白视为缺失；
 *   - 数组：`verified` 必须非空（无事实可展示说明上下文没生成成功），
 *     但 `tried_actions` 允许为空数组 —— 一个动作都没尝试是合法的业务状态
 *     （例如买家第一句就要人工），不应误报为异常；
 *   - 对象：`emotion` 必须含 level 与 intensity；
 *   - 数字：`turn_count` 允许 0（首轮），只拒绝 null / undefined。
 */
function hasValue(field: ContextRequiredField, value: unknown): boolean {
  if (value === null || value === undefined) return false

  switch (field) {
    case 'summary':
    case 'unmet_demand':
    case 'suggested_action':
      return typeof value === 'string' && value.trim().length > 0

    case 'verified':
      return Array.isArray(value) && value.length > 0

    case 'tried_actions':
      return Array.isArray(value)

    case 'emotion': {
      if (typeof value !== 'object' || value === null) return false
      const emotion = value as { level?: unknown; intensity?: unknown }
      return Boolean(emotion.level && emotion.intensity)
    }

    case 'turn_count':
      return typeof value === 'number' && Number.isFinite(value)

    default:
      return true
  }
}

export function guardContext(context: Partial<SessionContext> | null | undefined): ContextGuardResult {
  if (!context) {
    return {
      ok: false,
      missing: [...CONTEXT_REQUIRED_FIELDS],
      missingLabels: CONTEXT_REQUIRED_FIELDS.map((field) => FIELD_LABELS[field]),
      context: null,
    }
  }

  const missing = CONTEXT_REQUIRED_FIELDS.filter((field) => !hasValue(field, context[field]))

  if (missing.length > 0) {
    return {
      ok: false,
      missing: [...missing],
      missingLabels: missing.map((field) => FIELD_LABELS[field]),
      context: null,
    }
  }

  // 守卫通过：此处已逐字段校验过，可安全收窄
  return { ok: true, missing: [], missingLabels: [], context: context as SessionContext }
}

/**
 * 上报埋点。
 *
 * 任务书要求「上报埋点」，但没有给出埋点通道（BFF 也无此接口）。
 * 因此这里只做本地留痕 + 控制台告警，并在注释中标注对接点：
 * 真实接入时应改为调用统一埋点 SDK（如 window.__TRACK__ 或 BFF 的 /bff/v1/telemetry）。
 */
export function reportContextAnomaly(sessionId: string, missing: ContextRequiredField[]): void {
  const payload = {
    event: 'context_incomplete',
    session_id: sessionId,
    missing_fields: missing,
    at: new Date().toISOString(),
  }

  // 本地留痕，便于联调期排查；生产环境替换为统一埋点上报
  try {
    const key = 'wankeai.telemetry.context'
    const buffer = JSON.parse(window.sessionStorage.getItem(key) ?? '[]') as unknown[]
    buffer.push(payload)
    // 只保留最近 50 条，避免无限增长
    window.sessionStorage.setItem(key, JSON.stringify(buffer.slice(-50)))
  } catch {
    // sessionStorage 不可用（隐私模式）时静默降级
  }

  console.warn('[埋点] 上下文要素缺失', payload)
}
