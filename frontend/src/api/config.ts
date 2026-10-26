import { get, put } from './client'
import type {
  AgentGroupsResponse,
  ChannelAuth,
  HandoffRuleTestRequest,
  HandoffRuleTestResult,
  HandoffRulesResponse,
} from '@/types/contract'

/**
 * 配置接口 —— FE-11 渠道授权 / FE-12 分组排班 / FE-13 转人工规则。
 *
 * HC-2：渠道接口不返回任何 AppSecret / Token 明文（任务书 FE-11 DoD 明确要求）。
 */

/** GET /bff/v1/config/channels —— 渠道与授权状态（含到期提醒） */
export function fetchChannels(
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<{ channels: ChannelAuth[] }> {
  return get<{ channels: ChannelAuth[] }>('/config/channels', undefined, options)
}

/** PUT /bff/v1/config/channels/{channel} —— 手动更新认证状态（抖音小店关键路径） */
export function updateChannelAuth(
  channel: string,
  payload: { service_provider_certified?: boolean; service_provider_note?: string },
): Promise<ChannelAuth> {
  return put<ChannelAuth>(`/config/channels/${encodeURIComponent(channel)}`, payload)
}

/** GET /bff/v1/config/agent-groups —— 坐席分组与排班 */
export function fetchAgentGroups(
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<AgentGroupsResponse> {
  return get<AgentGroupsResponse>('/config/agent-groups', undefined, options)
}

/** PUT /bff/v1/config/agent-groups —— 保存在线时段（BE-15 在线判定 5 分钟内同步） */
export function saveAgentGroups(payload: {
  groups: Array<{ group_id: string; schedule: boolean[][] }>
}): Promise<AgentGroupsResponse> {
  return put<AgentGroupsResponse>('/config/agent-groups', payload)
}

/** GET /bff/v1/config/handoff-rules —— 6 条转人工触发规则 */
export function fetchHandoffRules(
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<HandoffRulesResponse> {
  return get<HandoffRulesResponse>('/config/handoff-rules', undefined, options)
}

/** PUT /bff/v1/config/handoff-rules —— 保存规则（变更 5 分钟内生效，留日志） */
export function saveHandoffRules(payload: {
  rules: Array<{
    rule_id: string
    enabled: boolean
    priority: number
    params: Array<{ key: string; value: number | string | boolean }>
  }>
}): Promise<HandoffRulesResponse> {
  return put<HandoffRulesResponse>('/config/handoff-rules', payload)
}

/** POST /bff/v1/config/handoff-rules/test —— 规则试算（抽样 50 条） */
export function testHandoffRules(
  payload: HandoffRuleTestRequest,
): Promise<HandoffRuleTestResult> {
  return put<HandoffRuleTestResult>('/config/handoff-rules/test', payload)
}
