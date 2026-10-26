import { get, post } from './client'
import type { Paged } from '@/types/bff'
import type {
  ReleaseResponse,
  ReplayResponse,
  SessionContext,
  SessionDetail,
  SessionListQuery,
  SessionSummary,
  TakeoverRequest,
  TakeoverResponse,
} from '@/types/contract'

/**
 * 会话相关 BFF 接口 —— 每接口一个函数，页面不直接写 URL（任务书 4.5.1）。
 * 对应任务书 2.5 BFF 接口总览「会话」与「对话」两组。
 */

/** GET /bff/v1/sessions —— 会话列表（筛选渠道/状态/是否已接管/时间范围） */
export function fetchSessions(
  query: SessionListQuery = {},
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<Paged<SessionSummary>> {
  return get<Paged<SessionSummary>>(
    '/sessions',
    {
      channel: query.channel,
      status: query.status,
      taken_over: query.taken_over === null ? undefined : query.taken_over,
      keyword: query.keyword,
      group_id: query.group_id,
      start_time: query.start_time,
      end_time: query.end_time,
      page: query.page ?? 1,
      page_size: query.page_size ?? 20,
    },
    options,
  )
}

/** GET /bff/v1/sessions/{session_id} —— 会话详情 + 消息历史 */
export function fetchSessionDetail(
  sessionId: string,
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<SessionDetail> {
  return get<SessionDetail>(`/sessions/${encodeURIComponent(sessionId)}`, undefined, options)
}

/** GET /bff/v1/sessions/{session_id}/context —— AI 上下文卡片数据（FE-02 七要素） */
export function fetchSessionContext(
  sessionId: string,
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<SessionContext> {
  return get<SessionContext>(
    `/sessions/${encodeURIComponent(sessionId)}/context`,
    undefined,
    options,
  )
}

/** POST /bff/v1/sessions/{session_id}/takeover —— 一键接管（FE-03） */
export function takeoverSession(
  sessionId: string,
  payload: TakeoverRequest,
): Promise<TakeoverResponse> {
  return post<TakeoverResponse>(`/sessions/${encodeURIComponent(sessionId)}/takeover`, payload)
}

/** POST /bff/v1/sessions/{session_id}/release —— 释放回机器人（FE-03） */
export function releaseSession(sessionId: string): Promise<ReleaseResponse> {
  return post<ReleaseResponse>(`/sessions/${encodeURIComponent(sessionId)}/release`, {})
}

/** GET /bff/v1/sessions/{session_id}/replay —— 会话回放与全链路日志（FE-10） */
export function fetchReplay(
  sessionId: string,
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<ReplayResponse> {
  return get<ReplayResponse>(`/sessions/${encodeURIComponent(sessionId)}/replay`, undefined, options)
}
