/**
 * 账号相关路由 —— 登录 / 登出 / 当前用户 / 改密 / 用户管理。
 *
 * 权限口径：
 *   /v1/auth/*       公开（login）或需登录（logout/me/password）
 *   /v1/users*       仅 ADMIN
 *   /v1/meta/groups  需登录（前端下拉用）
 *   /v1/audit        仅 ADMIN
 *
 * 为什么单独一个文件：handlers.ts 已经 1300 多行，
 * 账号这块自带 SQL 依赖，混进去会让两边都难读。
 */
import type { MockRequest, MockResult } from './http'
import { ok, fail, paginate, readQueryInt } from './http'
import {
  changePassword,
  createUser,
  getUser,
  listAudit,
  listGroups,
  listUsers,
  login,
  logout,
  resetPassword,
  updateUser,
  type UserListItem,
} from './services/auth-service'
import { SESSION_COOKIE, clearSessionCookie, readCookie, setSessionCookie } from './services/cookie'
import type { UserRole } from '@/permission/matrix'

const ADMIN_ONLY: UserRole[] = ['ADMIN']

/** 取客户端 IP：Mock 环境只有本地回环，真实环境取 X-Forwarded-For。 */
function clientIp(req: MockRequest): string {
  const forwarded = req.headers['x-forwarded-for']
  const raw = Array.isArray(forwarded) ? forwarded[0] : forwarded
  return (raw ?? '').split(',')[0]?.trim() || '127.0.0.1'
}

function clientUa(req: MockRequest): string {
  const ua = req.headers['user-agent']
  return String(Array.isArray(ua) ? ua[0] : (ua ?? '')).slice(0, 200)
}

/** 账号视图：**绝不包含 password_hash**（HC-2 明文/哈希都不出后端）。 */
function toView(user: UserListItem) {
  return {
    user_id: user.user_id,
    username: user.username,
    display_name: user.display_name,
    role: user.role,
    group_id: user.group_id,
    group_name: user.group_name,
    channels: user.channels,
    status: user.status,
    must_change: user.must_change,
    last_login_at: user.last_login_at,
    created_at: user.created_at,
    updated_at: user.updated_at,
    created_by: user.created_by,
  }
}

export type UserView = ReturnType<typeof toView>

export async function handleAuthRoutes(req: MockRequest): Promise<MockResult | null> {
  const { method, path } = req
  const auth = req.auth ?? null
  const ip = clientIp(req)

  // ── 登录 ────────────────────────────────────────────────────
  if (method === 'POST' && path === '/v1/auth/login') {
    const body = (req.body ?? {}) as { username?: string; password?: string }
    const username = String(body.username ?? '').trim()
    const password = String(body.password ?? '')

    if (!username || !password) {
      return fail(10001, '请输入账号和密码', 400)
    }

    const result = await login(username, password, { ip, user_agent: clientUa(req) })
    if (!result.ok) {
      const { failure } = result
      if (failure.kind === 'LOCKED') {
        // 20001 = 请求过于频繁，前端据此禁用按钮并倒计时（4.5.1）
        return fail(20001, failure.message, 429)
      }
      return fail(10002, failure.message, 401)
    }

    setSessionCookie(req.res, result.token, { maxAgeSec: 12 * 3600 })
    return ok({
      token: result.token,
      expires_at: result.expires_at,
      user: toView({ ...result.user, status: 'ACTIVE', created_at: '', updated_at: '', created_by: null }),
    }, '登录成功')
  }

  // ── 登出 ────────────────────────────────────────────────────
  if (method === 'POST' && path === '/v1/auth/logout') {
    const token = readCookie(req.headers, SESSION_COOKIE)
    logout(token, { ip })
    clearSessionCookie(req.res)
    return ok({ ok: true }, '已退出登录')
  }

  // ── 当前登录用户 ────────────────────────────────────────────
  if (method === 'GET' && path === '/v1/auth/me') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    const user = getUser(auth.user_id)
    if (!user) return fail(10002, '账号不存在', 401)
    return ok({ user: toView(user) })
  }

  // ── 修改自己的密码 ──────────────────────────────────────────
  if (method === 'POST' && path === '/v1/auth/password') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    const body = (req.body ?? {}) as { old_password?: string; new_password?: string }
    const oldPassword = String(body.old_password ?? '')
    const newPassword = String(body.new_password ?? '')
    if (!oldPassword || !newPassword) return fail(10001, '原密码与新密码都不能为空', 400)

    const result = await changePassword(auth.user_id, oldPassword, newPassword, { ip })
    if (!result.ok) return fail(10001, result.message, 400)
    // 改密会吊销全部会话，当前这个也失效 —— 前端应跳回登录页
    clearSessionCookie(req.res)
    return ok({ ok: true }, '密码已修改，请用新密码重新登录')
  }

  // ── 分组字典（登录页之外的下拉用）────────────────────────────
  if (method === 'GET' && path === '/v1/meta/groups') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    return ok({ items: listGroups() })
  }

  // ── 用户列表 ────────────────────────────────────────────────
  if (method === 'GET' && path === '/v1/users') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    if (!ADMIN_ONLY.includes(auth.role)) return fail(10003, '只有系统管理员可以查看账号列表', 403)

    const keyword = (req.query.get('keyword') ?? '').trim().toLowerCase()
    const role = req.query.get('role') ?? ''
    const status = req.query.get('status') ?? ''
    const groupId = req.query.get('group_id') ?? ''

    let items = listUsers()
    if (role) items = items.filter((item) => item.role === role)
    if (status) items = items.filter((item) => item.status === status)
    if (groupId) items = items.filter((item) => item.group_id === groupId)
    if (keyword) {
      items = items.filter(
        (item) =>
          item.username.toLowerCase().includes(keyword) ||
          item.display_name.toLowerCase().includes(keyword),
      )
    }

    const paged = paginate(items.map(toView), req.query, 20)
    // 顺带返回统计，避免前端为了画「按角色分布」再发一次请求
    const all = listUsers()
    return ok({
      ...paged,
      summary: {
        total: all.length,
        active: all.filter((item) => item.status === 'ACTIVE').length,
        disabled: all.filter((item) => item.status === 'DISABLED').length,
        locked: all.filter((item) => item.status === 'LOCKED').length,
        by_role: {
          AGENT: all.filter((item) => item.role === 'AGENT').length,
          SUPERVISOR: all.filter((item) => item.role === 'SUPERVISOR').length,
          KB_OPERATOR: all.filter((item) => item.role === 'KB_OPERATOR').length,
          ADMIN: all.filter((item) => item.role === 'ADMIN').length,
        },
      },
    })
  }

  // ── 新建账号 ────────────────────────────────────────────────
  if (method === 'POST' && path === '/v1/users') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    if (!ADMIN_ONLY.includes(auth.role)) return fail(10003, '只有系统管理员可以新建账号', 403)

    const body = (req.body ?? {}) as {
      username?: string
      display_name?: string
      role?: UserRole
      group_id?: string | null
      password?: string
    }
    const result = await createUser(
      {
        username: String(body.username ?? ''),
        display_name: String(body.display_name ?? ''),
        role: body.role ?? 'AGENT',
        group_id: body.group_id ?? null,
        password: body.password,
      },
      { user_id: auth.user_id, display_name: auth.display_name, ip },
    )
    if (!result.ok) return fail(10001, result.message, 400)
    return ok({ user: toView(result.data.user) }, '账号已创建')
  }

  // ── 编辑账号 ────────────────────────────────────────────────
  const userMatch = /^\/v1\/users\/([^/]+)$/.exec(path)
  if (method === 'PUT' && userMatch) {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    if (!ADMIN_ONLY.includes(auth.role)) return fail(10003, '只有系统管理员可以修改账号', 403)

    const userId = decodeURIComponent(userMatch[1]!)
    const body = (req.body ?? {}) as {
      display_name?: string
      role?: UserRole
      group_id?: string | null
      status?: 'ACTIVE' | 'DISABLED'
    }
    const result = updateUser(userId, body, {
      user_id: auth.user_id,
      display_name: auth.display_name,
      ip,
    })
    if (!result.ok) return fail(10001, result.message, 400)
    return ok({ user: toView(result.data.user) }, '账号已更新')
  }

  // ── 重置密码 ────────────────────────────────────────────────
  const resetMatch = /^\/v1\/users\/([^/]+)\/password$/.exec(path)
  if (method === 'POST' && resetMatch) {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    if (!ADMIN_ONLY.includes(auth.role)) return fail(10003, '只有系统管理员可以重置密码', 403)

    const userId = decodeURIComponent(resetMatch[1]!)
    const body = (req.body ?? {}) as { new_password?: string }
    const newPassword = String(body.new_password ?? '')
    if (!newPassword) return fail(10001, '请输入新密码', 400)

    const result = await resetPassword(userId, newPassword, {
      user_id: auth.user_id,
      display_name: auth.display_name,
      ip,
    })
    if (!result.ok) return fail(10001, result.message, 400)
    return ok({ username: result.data.username }, '密码已重置，该账号下次登录需修改密码')
  }

  // ── 操作日志（账号变更是审计重点，管理人后台要看）──────────
  if (method === 'GET' && path === '/v1/audit') {
    if (!auth) return fail(10002, '未登录或登录已过期', 401)
    if (!ADMIN_ONLY.includes(auth.role)) return fail(10003, '只有系统管理员可以查看操作日志', 403)
    const limit = Math.min(500, readQueryInt(req.query, 'limit', 100))
    const items = listAudit(limit).map((row) => ({
      ...row,
      detail: row.detail ? safeParseJson(row.detail) : null,
    }))
    return ok({ items })
  }

  return null
}

function safeParseJson(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}
