import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { UserRole } from '@/permission/matrix'
import { canDo, type ActionPermission } from '@/permission/matrix'
import { fetchMe, logoutApi, loginApi, type AccountUser } from '@/api/auth'

/**
 * 当前登录用户。
 *
 * 鉴权真源在服务端（SQLite + HttpOnly Cookie）：
 *   - 密码与角色存 `mock/db/wankeai.db`，前端拿不到也改不了；
 *   - 会话 token 在 HttpOnly Cookie 里，JS 读不到，XSS 偷不走；
 *   - 刷新页面后靠 `restore()` 调 /auth/me 重新问服务端「我是谁」。
 *
 * 这里保存的身份只用于三件事：
 *   1. 路由守卫按 4.5.4 权限矩阵控制页面可见性（防误点，不防越权）；
 *   2. 页面内按角色隐藏无权操作；
 *   3. 展示「谁在操作」。
 * 真正的越权拦截在 BFF：每个写接口都会自己校验角色与数据归属。
 */
export type CurrentUser = AccountUser

/** 登录失败的结构化结果，供登录页区分「密码错」与「被锁定」 */
export type LoginFailure =
  | { kind: 'BAD_CREDENTIAL'; message: string }
  | { kind: 'LOCKED'; message: string; retryAfterSec: number }
  | { kind: 'DISABLED'; message: string }
  | { kind: 'NETWORK'; message: string }

export type LoginOutcome = { ok: true; user: CurrentUser } | { ok: false; failure: LoginFailure }

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  /** 是否已向服务端问过「我是谁」。未确认前路由守卫不能判定为未登录。 */
  const ready = ref(false)

  const isAuthenticated = computed(() => user.value !== null)
  const role = computed<UserRole>(() => user.value?.role ?? 'AGENT')
  const displayName = computed(() => user.value?.display_name ?? '未登录')

  /**
   * 账号密码登录。
   *
   * 不在这里抛异常：登录失败是正常业务分支（密码错、被锁定），
   * 抛异常会让调用方必须包 try/catch，容易漏掉。
   */
  async function login(username: string, password: string): Promise<LoginOutcome> {
    try {
      const data = await loginApi(username, password)
      user.value = data.user
      ready.value = true
      return { ok: true, user: data.user }
    } catch (error) {
      return { ok: false, failure: toFailure(error) }
    }
  }

  /**
   * 刷新页面后恢复登录态。
   *
   * Cookie 是 HttpOnly，前端无法自查，只能问服务端。
   * 失败（未登录/过期）不视为异常，静默置未登录。
   *
   * `force` 用于「服务端会话可能已变」的场景（拦截器收到 10002、
   * 用户主动退出再登录等）：此时不能信 ready 缓存，必须重新问一次。
   */
  async function restore(force = false): Promise<void> {
    if (ready.value && !force) return
    try {
      const data = await fetchMe()
      user.value = data.user
    } catch {
      user.value = null
    } finally {
      // 无论成功失败都要置 true，否则守卫会一直等下去
      ready.value = true
    }
  }

  async function logout(): Promise<void> {
    try {
      await logoutApi()
    } catch {
      // 服务端不可达也要清掉本地身份：宁可多登一次，不可留一个失效身份
    }
    user.value = null
    // 关键：把 ready 复位。否则下次进 /login 时守卫会以为「已经问过了」，
    // 拿着一个空身份直接放行，用户会看到一个登录过的系统却又什么都点不动。
    ready.value = false
  }

  /** 细粒度动作权限，页面内使用；与路由级 canAccess 互为补充。 */
  function allow(action: ActionPermission): boolean {
    return canDo(role.value, action)
  }

  return {
    user,
    ready,
    isAuthenticated,
    role,
    displayName,
    login,
    restore,
    logout,
    allow,
  }
})

/** 把拦截器抛出的 BffError 归一成登录页能直接展示的结构 */
function toFailure(error: unknown): LoginFailure {
  const err = error as { code?: number; message?: string }
  const message = err?.message || '登录失败，请稍后重试'
  if (err?.code === 20001) {
    // 20001 = 请求过于频繁。登录场景下是「连续失败被锁定」，按倒计时禁用按钮。
    const match = /(\d+)\s*分钟/.exec(message)
    const minutes = match ? Number(match[1]) : 10
    return { kind: 'LOCKED', message, retryAfterSec: minutes * 60 }
  }
  if (err?.code === 10002) {
    if (message.includes('停用')) return { kind: 'DISABLED', message }
    return { kind: 'BAD_CREDENTIAL', message }
  }
  if (err?.code === 30001 || err?.code === undefined) {
    return { kind: 'NETWORK', message: '服务暂时不可用，请稍后重试' }
  }
  return { kind: 'BAD_CREDENTIAL', message }
}
