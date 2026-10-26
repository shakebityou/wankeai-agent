import { get } from './client'
import type {
  MetricsDistributionResponse,
  MetricsOverviewResponse,
  MetricsTrendResponse,
} from '@/types/contract'

/**
 * 指标接口 —— FE-09 指标看板（12 项验收指标）。
 * 注意 HC-2：这里只取聚合值，绝不请求任何含个人信息的明细。
 */

/** GET /bff/v1/metrics/overview —— 12 项概览（当前值/目标值/达标状态） */
export function fetchMetricsOverview(
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<MetricsOverviewResponse> {
  return get<MetricsOverviewResponse>('/metrics/overview', undefined, options)
}

/** GET /bff/v1/metrics/trend —— 趋势（按日/周/渠道/意图切换） */
export function fetchMetricsTrend(
  params: { granularity?: 'DAY' | 'WEEK'; metric_ids?: string; days?: number } = {},
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<MetricsTrendResponse> {
  return get<MetricsTrendResponse>(
    '/metrics/trend',
    {
      granularity: params.granularity ?? 'DAY',
      metric_ids: params.metric_ids,
      days: params.days ?? 30,
    },
    options,
  )
}

/** GET /bff/v1/metrics/distribution —— 渠道占比 / 意图分布 / 转人工漏斗 */
export function fetchMetricsDistribution(
  params: { days?: number } = {},
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<MetricsDistributionResponse> {
  return get<MetricsDistributionResponse>(
    '/metrics/distribution',
    { days: params.days ?? 30 },
    options,
  )
}
