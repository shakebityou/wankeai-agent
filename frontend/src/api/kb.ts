import { get, post, put, del, upload } from './client'
import type { Paged } from '@/types/bff'
import type {
  FeedbackCreateRequest,
  FeedbackItem,
  FeedbackListQuery,
  ImportCommitRequest,
  ImportPreviewResponse,
  ImportProgress,
  KbChunk,
  KbChunkListQuery,
  KbChunkPayload,
  KbHistoryResponse,
  KbReviewPayload,
  KbSearchRequest,
  KbSearchResponse,
  TicketDetail,
  TicketListQuery,
  TicketSummary,
} from '@/types/contract'

/**
 * 知识库相关 BFF 接口 —— FE-04 / FE-05 / FE-06 / FE-07 / FE-08。
 */

// ── FE-05 条目 CRUD 与审核流 ─────────────────────────────────

/** GET /bff/v1/kb/chunks —— 知识条目列表（筛选 category/sku_ids/review_status/channel） */
export function fetchKbChunks(
  query: KbChunkListQuery = {},
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<Paged<KbChunk>> {
  return get<Paged<KbChunk>>(
    '/kb/chunks',
    {
      category: query.category,
      sku_id: query.sku_id,
      review_status: query.review_status,
      channel: query.channel,
      kb_type: query.kb_type,
      keyword: query.keyword,
      page: query.page ?? 1,
      page_size: query.page_size ?? 20,
    },
    options,
  )
}

/** POST /bff/v1/kb/chunks —— 新增条目 */
export function createKbChunk(payload: KbChunkPayload): Promise<KbChunk> {
  return post<KbChunk>('/kb/chunks', payload)
}

/** PUT /bff/v1/kb/chunks/{chunk_id} —— 编辑条目 */
export function updateKbChunk(chunkId: string, payload: KbChunkPayload): Promise<KbChunk> {
  return put<KbChunk>(`/kb/chunks/${encodeURIComponent(chunkId)}`, payload)
}

/** DELETE /bff/v1/kb/chunks/{chunk_id} —— 下架（软删 DISABLED，保留版本历史） */
export function deleteKbChunk(chunkId: string): Promise<{ chunk_id: string; review_status: string }> {
  return del<{ chunk_id: string; review_status: string }>(`/kb/chunks/${encodeURIComponent(chunkId)}`)
}

/** POST /bff/v1/kb/chunks/{chunk_id}/review —— 审核（PENDING -> APPROVED / REJECTED） */
export function reviewKbChunk(chunkId: string, payload: KbReviewPayload): Promise<KbChunk> {
  return post<KbChunk>(`/kb/chunks/${encodeURIComponent(chunkId)}/review`, payload)
}

/** 批量审核：逐个调用，返回成功与失败明细（任务书 FE-05「批量审核可用」） */
export async function batchReviewKbChunks(
  chunkIds: string[],
  payload: KbReviewPayload,
): Promise<{ succeeded: string[]; failed: Array<{ chunk_id: string; message: string }> }> {
  const succeeded: string[] = []
  const failed: Array<{ chunk_id: string; message: string }> = []
  for (const chunkId of chunkIds) {
    try {
      await reviewKbChunk(chunkId, payload)
      succeeded.push(chunkId)
    } catch (error) {
      failed.push({
        chunk_id: chunkId,
        message: error instanceof Error ? error.message : String(error),
      })
    }
  }
  return { succeeded, failed }
}

// ── FE-04 坐席侧检索 ────────────────────────────────────────

/** POST /bff/v1/kb/search —— 坐席侧知识检索（只返回 APPROVED 条目） */
export function searchKb(
  payload: KbSearchRequest,
  options: { signal?: AbortSignal } = {},
): Promise<KbSearchResponse> {
  return post<KbSearchResponse>('/kb/search', payload, options)
}

// ── FE-06 批量导入 ──────────────────────────────────────────

/** POST /bff/v1/kb/import/preview —— 切分预览（导入前必看） */
export function previewKbImport(file: File): Promise<ImportPreviewResponse> {
  const form = new FormData()
  form.append('file', file)
  return upload<ImportPreviewResponse>('/kb/import/preview', form)
}

/** POST /bff/v1/kb/import —— 人工确认后真正导入 */
export function commitKbImport(payload: ImportCommitRequest): Promise<ImportProgress> {
  return post<ImportProgress>('/kb/import', payload)
}

/** GET /bff/v1/kb/import/{import_id}/progress —— 导入进度（1,000 条实时可见） */
export function fetchImportProgress(importId: string): Promise<ImportProgress> {
  return get<ImportProgress>(`/kb/import/${encodeURIComponent(importId)}/progress`)
}

// ── FE-07 版本与生效时间 ────────────────────────────────────

/** GET /bff/v1/kb/chunks/{chunk_id}/history —— 版本历史 */
export function fetchKbHistory(chunkId: string, options?: { signal?: AbortSignal }): Promise<KbHistoryResponse> {
  return get<KbHistoryResponse>(`/kb/chunks/${encodeURIComponent(chunkId)}/history`, undefined, options)
}

/** POST /bff/v1/kb/chunks/{chunk_id}/rollback —— 回滚到指定版本 */
export function rollbackKbChunk(
  chunkId: string,
  version: number,
): Promise<{ chunk_id: string; version: number; effective_at: string }> {
  return post<{ chunk_id: string; version: number; effective_at: string }>(
    `/kb/chunks/${encodeURIComponent(chunkId)}/rollback`,
    { version },
  )
}

// ── FE-08 纠错闭环 ──────────────────────────────────────────

/** POST /bff/v1/kb/feedback —— 「这条答错了」纠错上报 */
export function createFeedback(payload: FeedbackCreateRequest): Promise<FeedbackItem> {
  return post<FeedbackItem>('/kb/feedback', payload)
}

/** GET /bff/v1/kb/feedback —— 纠错待办列表 */
export function fetchFeedbackList(
  query: FeedbackListQuery = {},
): Promise<Paged<FeedbackItem>> {
  return get<Paged<FeedbackItem>>('/kb/feedback', {
    error_type: query.error_type,
    status: query.status,
    assignee: query.assignee,
    page: query.page ?? 1,
    page_size: query.page_size ?? 20,
  })
}

/** PUT /bff/v1/kb/feedback/{feedback_id} —— 指派 / 关闭 / 关联 chunk 修改 */
export function updateFeedback(
  feedbackId: string,
  payload: {
    status?: FeedbackItem['status']
    assignee?: string | null
    resolved_chunk_id?: string | null
    resolution?: string | null
  },
): Promise<FeedbackItem> {
  return put<FeedbackItem>(`/kb/feedback/${encodeURIComponent(feedbackId)}`, payload)
}

// ── 工单（FE-02）───────────────────────────────────────────

/** GET /bff/v1/tickets —— 工单列表（按 reason/priority/status 筛选） */
export function fetchTickets(
  query: TicketListQuery = {},
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<Paged<TicketSummary>> {
  return get<Paged<TicketSummary>>(
    '/tickets',
    {
      reason: query.reason,
      priority: query.priority,
      status: query.status,
      keyword: query.keyword,
      page: query.page ?? 1,
      // FE-02 DoD：工单列表分页 50 条/页
      page_size: query.page_size ?? 50,
    },
    options,
  )
}

/** GET /bff/v1/tickets/{ticket_no} —— 工单详情 */
export function fetchTicketDetail(
  ticketNo: string,
  options: { signal?: AbortSignal; silent?: boolean } = {},
): Promise<TicketDetail> {
  return get<TicketDetail>(`/tickets/${encodeURIComponent(ticketNo)}`, undefined, options)
}
