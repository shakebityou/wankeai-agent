/**
 * 账号认证服务 —— 登录 / 登出 / 会话校验 / 改密 / 用户管理。
 *
 * 这一层不碰 HTTP，只做业务判断；handlers 负责把结果翻译成 BFF 响应。
 * 分开的好处：单测可以直接调这里，不用起服务。
 *
 * 安全口径（与任务书 4.5 及 HC-2 对齐）：
 *   - 密码只存 scrypt 哈希，任何接口都不返回哈希；
 *   - 连续失败 5 次锁定 10 分钟，返回 20001（限流码），前端按倒计时禁用按钮；
 *   - 登录态 token 随机 32 字节，库里只存 sha256，可随时吊销；
 *   - 所有账号写操作落 audit_log。
 */
import { randomBytes } from 'node:crypto'
import { getDb, writeAudit, type UserRow } from '../db/index.ts'
import { hashPassword, hashToken, verifyPassword } from '../db/password.ts'
import { DEMO_PASSWORD, ensureSeeded } from '../seed/auth-db.ts'
import type { UserRole } from '../../src/permission/matrix.ts'

/** 连续失败上限：与 4.5.1 的 20001 语义一致，前端按此做倒计时禁用。 */
export const MAX_FAILED_ATTEMPTS = 5
/** 锁定时长（分钟） */
export const LOCK_MINUTES = 10
/** 登录态有效期（小时）。坐席一个班次 8 小时，留 4 小时余量。 */
export const SESSION_TTL_HOURS = 12
/** 密码最小长度 */
export const MIN_PASSWORD_LENGTH = 8

export interface AuthUser {
  user_id: string
  username: string
  display_name: string
  role: UserRole
  group_id: string | null
  group_name: string | null
  /** 该坐席可见的渠道（来自分组），主管/管理员为全渠道 */
  channels: string[]
  must_change: boolean
  last_login_at: string | null
}

export type AuthFailure =
  | { kind: 'BAD_CREDENTIAL'; message: string }
  | { kind: 'LOCKED'; message: string; retry_after_sec: number }
  | { kind: 'DISABLED'; message: string }

export type LoginResult =
  | { ok: true; token: string; user: AuthUser; expires_at: string }
  | { ok: false; failure: AuthFailure }

const ALL_CHANNELS = ['TAOBAO', 'TMALL', 'PDD', 'DOUYIN', 'WECHAT'] as const

// ─────────────────────────────────────────────────────────────
// 登录
// ─────────────────────────────────────────────────────────────

export async function login(
  username: string,
  password: string,
  meta: { ip?: string; user_agent?: string } = {},
): Promise<LoginResult> {
  await ensureSeeded()
  const db = getDb()

  const row = db
    .prepare('SELECT * FROM users WHERE username = ? COLLATE NOCASE')
    .get(username.trim()) as UserRow | undefined

  // 账号不存在时也走一次哈希校验，让响应时间与「密码错」一致，
  // 否则攻击者能靠计时区分「账号存在但密码错」和「账号不存在」。
  if (!row) {
    await verifyPassword(password, DECOY_HASH)
    return { ok: false, failure: { kind: 'BAD_CREDENTIAL', message: '账号或密码不正确' } }
  }

  if (row.status === 'DISABLED') {
    return { ok: false, failure: { kind: 'DISABLED', message: '该账号已停用，请联系管理员' } }
  }

  if (row.status === 'LOCKED') {
    const remainMs = row.locked_until ? Date.parse(row.locked_until) - Date.now() : 0
    if (remainMs > 0) {
      return {
        ok: false,
        failure: {
          kind: 'LOCKED',
          message: `登录失败次数过多，请 ${Math.ceil(remainMs / 60000)} 分钟后再试`,
          retry_after_sec: Math.ceil(remainMs / 1000),
        },
      }
    }
    // 锁定已到期：自动解锁，允许再试
    db.prepare("UPDATE users SET status = 'ACTIVE', failed_count = 0, locked_until = NULL WHERE user_id = ?").run(
      row.user_id,
    )
    row.status = 'ACTIVE'
    row.failed_count = 0
  }

  const matched = await verifyPassword(password, row.password_hash)

  if (!matched) {
    const failed = row.failed_count + 1
    const now = new Date()
    if (failed >= MAX_FAILED_ATTEMPTS) {
      const until = new Date(now.getTime() + LOCK_MINUTES * 60_000)
      db.prepare(
        "UPDATE users SET failed_count = ?, status = 'LOCKED', locked_until = ?, updated_at = ? WHERE user_id = ?",
      ).run(failed, until.toISOString(), now.toISOString(), row.user_id)
      writeAudit({
        actor_id: row.user_id,
        actor_name: row.display_name,
        action: 'LOGIN_LOCKED',
        target: row.username,
        detail: { failed_attempts: failed },
        ip: meta.ip,
      })
      return {
        ok: false,
        failure: {
          kind: 'LOCKED',
          message: `连续 ${MAX_FAILED_ATTEMPTS} 次密码错误，账号已锁定 ${LOCK_MINUTES} 分钟`,
          retry_after_sec: LOCK_MINUTES * 60,
        },
      }
    }
    db.prepare('UPDATE users SET failed_count = ?, updated_at = ? WHERE user_id = ?').run(
      failed,
      now.toISOString(),
      row.user_id,
    )
    return {
      ok: false,
      failure: {
        kind: 'BAD_CREDENTIAL',
        message: `账号或密码不正确，还可尝试 ${MAX_FAILED_ATTEMPTS - failed} 次`,
      },
    }
  }

  // 登录成功
  const now = new Date()
  const expiresAt = new Date(now.getTime() + SESSION_TTL_HOURS * 3600_000)
  const token = randomBytes(32).toString('base64url')

  db.prepare(
    'UPDATE users SET failed_count = 0, status = ?, locked_until = NULL, last_login_at = ?, last_login_ip = ?, updated_at = ? WHERE user_id = ?',
  ).run('ACTIVE', now.toISOString(), meta.ip ?? null, now.toISOString(), row.user_id)

  db.prepare(
    'INSERT INTO auth_sessions (token_hash, user_id, issued_at, expires_at, revoked_at, user_agent) VALUES (?, ?, ?, ?, NULL, ?)',
  ).run(hashToken(token), row.user_id, now.toISOString(), expiresAt.toISOString(), meta.user_agent ?? null)

  writeAudit({
    actor_id: row.user_id,
    actor_name: row.display_name,
    action: 'LOGIN',
    target: row.username,
    ip: meta.ip,
  })

  return {
    ok: true,
    token,
    user: toAuthUser(row),
    expires_at: expiresAt.toISOString(),
  }
}

/**
 * 伪造哈希：账号不存在时也做一次同等代价的校验。
 * 值本身无意义，只要格式合法即可。
 */
const DECOY_HASH =
  'scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='

// ─────────────────────────────────────────────────────────────
// 会话校验 / 登出
// ─────────────────────────────────────────────────────────────

export function resolveToken(token: string): AuthUser | null {
  if (!token) return null
  const db = getDb()
  const session = db
    .prepare('SELECT * FROM auth_sessions WHERE token_hash = ?')
    .get(hashToken(token)) as { user_id: string; expires_at: string; revoked_at: string | null } | undefined

  if (!session) return null
  if (session.revoked_at) return null
  if (Date.parse(session.expires_at) <= Date.now()) return null

  const row = db.prepare('SELECT * FROM users WHERE user_id = ?').get(session.user_id) as UserRow | undefined
  if (!row || row.status !== 'ACTIVE') return null
  return toAuthUser(row)
}

export function logout(token: string, meta: { ip?: string } = {}): void {
  if (!token) return
  const db = getDb()
  const tokenHash = hashToken(token)
  const session = db
    .prepare('SELECT user_id FROM auth_sessions WHERE token_hash = ?')
    .get(tokenHash) as { user_id: string } | undefined

  db.prepare('UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ?').run(
    new Date().toISOString(),
    tokenHash,
  )

  if (session) {
    const row = db.prepare('SELECT display_name FROM users WHERE user_id = ?').get(session.user_id) as
      | { display_name: string }
      | undefined
    writeAudit({
      actor_id: session.user_id,
      actor_name: row?.display_name,
      action: 'LOGOUT',
      ip: meta.ip,
    })
  }
}

/** 吊销某人的全部会话：禁用账号、改密、重置密码时调用。 */
function revokeAllSessions(userId: string): void {
  getDb()
    .prepare('UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL')
    .run(new Date().toISOString(), userId)
}

// ─────────────────────────────────────────────────────────────
// 修改自己的密码
// ─────────────────────────────────────────────────────────────

export type ChangePasswordResult = { ok: true } | { ok: false; message: string }

export async function changePassword(
  userId: string,
  oldPassword: string,
  newPassword: string,
  meta: { ip?: string } = {},
): Promise<ChangePasswordResult> {
  const db = getDb()
  const row = db.prepare('SELECT * FROM users WHERE user_id = ?').get(userId) as UserRow | undefined
  if (!row) return { ok: false, message: '账号不存在' }

  if (!(await verifyPassword(oldPassword, row.password_hash))) {
    return { ok: false, message: '原密码不正确' }
  }

  const check = validatePasswordStrength(newPassword)
  if (!check.ok) return check as ChangePasswordResult

  if (await verifyPassword(newPassword, row.password_hash)) {
    return { ok: false, message: '新密码不能与原密码相同' }
  }

  db.prepare('UPDATE users SET password_hash = ?, must_change = 0, updated_at = ? WHERE user_id = ?').run(
    await hashPassword(newPassword),
    new Date().toISOString(),
    userId,
  )
  // 改密后旧会话全部失效，避免密码泄露后被持续利用
  revokeAllSessions(userId)

  writeAudit({
    actor_id: row.user_id,
    actor_name: row.display_name,
    action: 'CHANGE_PASSWORD',
    target: row.username,
    ip: meta.ip,
  })
  return { ok: true }
}

export function validatePasswordStrength(password: string): { ok: boolean; message: string } {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return { ok: false, message: `密码至少 ${MIN_PASSWORD_LENGTH} 位` }
  }
  if (!/[a-zA-Z]/.test(password) || !/\d/.test(password)) {
    return { ok: false, message: '密码需同时包含字母和数字' }
  }
  return { ok: true, message: '' }
}

// ─────────────────────────────────────────────────────────────
// 用户管理（仅 ADMIN）
// ─────────────────────────────────────────────────────────────

export interface UserListItem extends AuthUser {
  status: 'ACTIVE' | 'DISABLED' | 'LOCKED'
  created_at: string
  updated_at: string
  created_by: string | null
}

export function listUsers(): UserListItem[] {
  const rows = getDb().prepare('SELECT * FROM users ORDER BY user_id').all() as unknown as UserRow[]
  return rows.map((row) => ({ ...toAuthUser(row), status: row.status, created_at: row.created_at, updated_at: row.updated_at, created_by: row.created_by }))
}

export function getUser(userId: string): UserListItem | null {
  const row = getDb().prepare('SELECT * FROM users WHERE user_id = ?').get(userId) as UserRow | undefined
  if (!row) return null
  return { ...toAuthUser(row), status: row.status, created_at: row.created_at, updated_at: row.updated_at, created_by: row.created_by }
}

export type MutationResult<T = { ok: true }> = { ok: true; data: T } | { ok: false; message: string }

export async function createUser(
  input: {
    username: string
    display_name: string
    role: UserRole
    group_id?: string | null
    password?: string
  },
  actor: { user_id: string; display_name: string; ip?: string },
): Promise<MutationResult<{ user: UserListItem }>> {
  const db = getDb()
  const username = input.username.trim()
  const displayName = input.display_name.trim()

  if (!username) return { ok: false, message: '登录账号不能为空' }
  if (!/^[a-zA-Z0-9._-]{3,32}$/.test(username)) {
    return { ok: false, message: '账号需 3–32 位，只允许字母、数字、点、下划线、短横线' }
  }
  if (!displayName) return { ok: false, message: '姓名不能为空' }

  const dup = db.prepare('SELECT user_id FROM users WHERE username = ? COLLATE NOCASE').get(username)
  if (dup) return { ok: false, message: `账号「${username}」已存在` }

  const password = input.password?.trim() || DEMO_PASSWORD
  const strength = validatePasswordStrength(password)
  if (!strength.ok) return { ok: false, message: strength.message }

  const role = db.prepare('SELECT role_code FROM roles WHERE role_code = ?').get(input.role)
  if (!role) return { ok: false, message: `未知角色 ${input.role}` }

  if (input.group_id) {
    const group = db.prepare('SELECT group_id FROM groups WHERE group_id = ?').get(input.group_id)
    if (!group) return { ok: false, message: `未知分组 ${input.group_id}` }
  }

  const seq = (db.prepare('SELECT COUNT(*) AS n FROM users').get() as { n: number }).n + 1
  const userId = `U${String(seq).padStart(4, '0')}`
  const now = new Date().toISOString()

  db.prepare(
    `INSERT INTO users (
       user_id, username, display_name, role_code, group_id, password_hash,
       status, failed_count, locked_until, must_change,
       last_login_at, last_login_ip, created_at, updated_at, created_by
     ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', 0, NULL, 1, NULL, NULL, ?, ?, ?)`,
  ).run(
    userId,
    username,
    displayName,
    input.role,
    input.group_id ?? null,
    await hashPassword(password),
    now,
    now,
    actor.user_id,
  )

  writeAudit({
    actor_id: actor.user_id,
    actor_name: actor.display_name,
    action: 'USER_CREATE',
    target: username,
    detail: { role: input.role, group_id: input.group_id ?? null },
    ip: actor.ip,
  })

  const created = getUser(userId)
  if (!created) return { ok: false, message: '创建后读取失败' }
  return { ok: true, data: { user: created } }
}

export function updateUser(
  userId: string,
  patch: {
    display_name?: string
    role?: UserRole
    group_id?: string | null
    status?: 'ACTIVE' | 'DISABLED'
  },
  actor: { user_id: string; display_name: string; ip?: string },
): MutationResult<{ user: UserListItem }> {
  const db = getDb()
  const row = db.prepare('SELECT * FROM users WHERE user_id = ?').get(userId) as UserRow | undefined
  if (!row) return { ok: false, message: '账号不存在' }

  const changes: string[] = []
  const params: Array<string | null> = []

  if (patch.display_name !== undefined) {
    const name = patch.display_name.trim()
    if (!name) return { ok: false, message: '姓名不能为空' }
    changes.push('display_name = ?')
    params.push(name)
  }
  if (patch.role !== undefined) {
    if (userId === actor.user_id && patch.role !== 'ADMIN') {
      return { ok: false, message: '不能修改自己的角色，请让另一位管理员操作' }
    }
    changes.push('role_code = ?')
    params.push(patch.role)
  }
  if (patch.group_id !== undefined) {
    changes.push('group_id = ?')
    params.push(patch.group_id || null)
  }
  if (patch.status !== undefined) {
    if (userId === actor.user_id && patch.status === 'DISABLED') {
      return { ok: false, message: '不能禁用自己' }
    }
    changes.push('status = ?')
    params.push(patch.status)
    // 禁用时清掉失败计数与锁定，避免解禁后还要等锁定到期
    if (patch.status === 'ACTIVE') {
      changes.push('failed_count = 0', 'locked_until = NULL')
    }
  }

  if (changes.length === 0) return { ok: false, message: '没有需要修改的字段' }

  changes.push('updated_at = ?')
  params.push(new Date().toISOString())
  params.push(userId)

  db.prepare(`UPDATE users SET ${changes.join(', ')} WHERE user_id = ?`).run(...params)

  // 禁用账号立刻踢下线：不等 token 过期
  if (patch.status === 'DISABLED') revokeAllSessions(userId)

  writeAudit({
    actor_id: actor.user_id,
    actor_name: actor.display_name,
    action: 'USER_UPDATE',
    target: row.username,
    detail: patch,
    ip: actor.ip,
  })

  const updated = getUser(userId)
  if (!updated) return { ok: false, message: '更新后读取失败' }
  return { ok: true, data: { user: updated } }
}

export async function resetPassword(
  userId: string,
  newPassword: string,
  actor: { user_id: string; display_name: string; ip?: string },
): Promise<MutationResult<{ username: string }>> {
  const db = getDb()
  const row = db.prepare('SELECT * FROM users WHERE user_id = ?').get(userId) as UserRow | undefined
  if (!row) return { ok: false, message: '账号不存在' }

  const strength = validatePasswordStrength(newPassword)
  if (!strength.ok) return { ok: false, message: strength.message }

  db.prepare(
    'UPDATE users SET password_hash = ?, must_change = 1, status = ?, failed_count = 0, locked_until = NULL, updated_at = ? WHERE user_id = ?',
  ).run(await hashPassword(newPassword), 'ACTIVE', new Date().toISOString(), userId)

  revokeAllSessions(userId)

  writeAudit({
    actor_id: actor.user_id,
    actor_name: actor.display_name,
    action: 'USER_RESET_PASSWORD',
    target: row.username,
    ip: actor.ip,
  })

  return { ok: true, data: { username: row.username } }
}

export function listGroups(): Array<{ group_id: string; group_name: string; leader: string | null; headcount: number; channels: string[] }> {
  const rows = getDb().prepare('SELECT * FROM groups ORDER BY group_id').all() as unknown as Array<{
    group_id: string
    group_name: string
    leader: string | null
    headcount: number
    channels: string
  }>
  return rows.map((row) => ({ ...row, channels: row.channels ? row.channels.split(',') : [] }))
}

export function listAudit(limit = 100): Array<{
  id: number
  at: string
  actor_name: string | null
  action: string
  target: string | null
  detail: string | null
}> {
  return getDb()
    .prepare('SELECT id, at, actor_name, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?')
    .all(limit) as Array<{
    id: number
    at: string
    actor_name: string | null
    action: string
    target: string | null
    detail: string | null
  }>
}

// ─────────────────────────────────────────────────────────────
// 内部映射
// ─────────────────────────────────────────────────────────────

function toAuthUser(row: UserRow): AuthUser {
  const group = row.group_id
    ? (getDb().prepare('SELECT group_name, channels FROM groups WHERE group_id = ?').get(row.group_id) as
        | { group_name: string; channels: string }
        | undefined)
    : undefined

  // 主管与管理员看全渠道；坐席只看本组渠道（4.5.4「坐席仅见本组会话」）
  const role = row.role_code as UserRole
  const channels =
    role === 'AGENT' && group?.channels
      ? group.channels.split(',').filter(Boolean)
      : [...ALL_CHANNELS]

  return {
    user_id: row.user_id,
    username: row.username,
    display_name: row.display_name,
    role,
    group_id: row.group_id,
    group_name: group?.group_name ?? null,
    channels,
    must_change: row.must_change === 1,
    last_login_at: row.last_login_at,
  }
}
