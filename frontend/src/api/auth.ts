/**
 * 账号相关接口 —— 登录 / 登出 / 当前用户 / 改密 / 用户管理。
 *
 * 与其它 api 模块的区别：这里用 `silent: true` 调底层请求。
 * 原因：登录失败（密码错、被锁定）是正常业务分支，
 * 不该弹全局红色提示 —— 登录页自己会在表单里给出精确文案与倒计时。
 */
import { get, post, put } from '@/api/client'

export interface AccountUser {
  user_id: string
  username: string
  display_name: string
  role: 'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN'
  group_id: string | null
  group_name: string | null
  channels: string[]
  status: 'ACTIVE' | 'DISABLED' | 'LOCKED'
  must_change: boolean
  last_login_at: string | null
  created_at?: string
  updated_at?: string
  created_by?: string | null
}

export interface UserSummary {
  total: number
  active: number
  disabled: number
  locked: number
  by_role: Record<'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN', number>
}

export interface UserListResponse {
  total: number
  page: number
  page_size: number
  items: AccountUser[]
  summary: UserSummary
}

export interface GroupOption {
  group_id: string
  group_name: string
  leader: string | null
  headcount: number
  channels: string[]
}

export interface CreateUserPayload {
  username: string
  display_name: string
  role: AccountUser['role']
  group_id?: string | null
  password?: string
}

export interface UpdateUserPayload {
  display_name?: string
  role?: AccountUser['role']
  group_id?: string | null
  status?: 'ACTIVE' | 'DISABLED'
}

const SILENT = { silent: true }

export function bffPost<T>(url: string, body?: unknown): Promise<T> {
  return post<T>(url, body, SILENT)
}

export function bffGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return get<T>(url, params, { silent: true })
}

// ── 登录态 ────────────────────────────────────────────────────

export function fetchGroups(): Promise<{ items: GroupOption[] }> {
  return get<{ items: GroupOption[] }>('/meta/groups')
}

export function loginApi(username: string, password: string): Promise<{ user: AccountUser; expires_at: string }> {
  return bffPost('/auth/login', { username, password })
}

export function logoutApi(): Promise<{ ok: boolean }> {
  return bffPost('/auth/logout', {})
}

export function fetchMe(): Promise<{ user: AccountUser }> {
  return bffGet('/auth/me')
}

export function changePasswordApi(oldPassword: string, newPassword: string): Promise<{ ok: boolean }> {
  return bffPost('/auth/password', { old_password: oldPassword, new_password: newPassword })
}

// ── 用户管理（仅管理员）──────────────────────────────────────

export function fetchUsers(params: {
  keyword?: string
  role?: string
  status?: string
  group_id?: string
  page?: number
  page_size?: number
}): Promise<UserListResponse> {
  return get<UserListResponse>('/users', params)
}

export function createUserApi(payload: CreateUserPayload): Promise<{ user: AccountUser }> {
  return post('/users', payload)
}

export function updateUserApi(userId: string, payload: UpdateUserPayload): Promise<{ user: AccountUser }> {
  return put(`/users/${encodeURIComponent(userId)}`, payload)
}

export function resetPasswordApi(userId: string, newPassword: string): Promise<{ username: string }> {
  return post(`/users/${encodeURIComponent(userId)}/password`, { new_password: newPassword })
}

export interface AuditEntry {
  id: number
  at: string
  actor_name: string | null
  action: string
  target: string | null
  detail: unknown
}

export function fetchAudit(limit = 100): Promise<{ items: AuditEntry[] }> {
  return get('/audit', { limit })
}
