/**
 * 会话 Cookie 工具 —— 签发与解析。
 *
 * 为什么用 HttpOnly Cookie 而不是 localStorage 存 token：
 *   localStorage 能被任意 JS 读取，一次 XSS 就等于 token 泄露；
 *   HttpOnly 让前端脚本根本拿不到值，能挡住这一类攻击。
 *   代价是前端无法判断「我有没有登录」，所以另配一个 /auth/me 接口。
 *
 * SameSite=Lax：本站是前后端同源部署（Nginx 反代 /bff），
 * 不需要跨站携带，Lax 能同时防 CSRF 又保留从外部链接点进来的场景。
 */
import type { ServerResponse } from 'node:http'

export const SESSION_COOKIE = 'wk_session'

interface CookieOptions {
  maxAgeSec?: number
  clear?: boolean
}

export function setSessionCookie(res: ServerResponse, token: string, opts: CookieOptions = {}): void {
  appendCookie(res, buildCookie(token, opts))
}

export function clearSessionCookie(res: ServerResponse): void {
  appendCookie(res, buildCookie('', { clear: true }))
}

function buildCookie(token: string, opts: CookieOptions): string {
  const parts = [`${SESSION_COOKIE}=${opts.clear ? '' : encodeURIComponent(token)}`, 'Path=/', 'HttpOnly', 'SameSite=Lax']
  if (opts.clear) {
    parts.push('Max-Age=0')
  } else if (opts.maxAgeSec) {
    parts.push(`Max-Age=${opts.maxAgeSec}`)
  }
  // 生产环境走 HTTPS 时由 Nginx 加 Secure；开发环境是 http，加了浏览器会直接丢弃
  if (process.env.NODE_ENV === 'production') parts.push('Secure')
  return parts.join('; ')
}

function appendCookie(res: ServerResponse, cookie: string): void {
  const existing = res.getHeader('Set-Cookie')
  const next = existing ? ([] as string[]).concat(existing as string | string[], [cookie]) : cookie
  res.setHeader('Set-Cookie', next)
}

/** 从请求头解析指定 Cookie 的值；不存在返回空串。 */
export function readCookie(
  headers: Record<string, string | string[] | undefined>,
  name: string,
): string {
  const raw = headers.cookie
  const line = Array.isArray(raw) ? raw.join('; ') : (raw ?? '')
  if (!line) return ''

  for (const part of line.split(';')) {
    const idx = part.indexOf('=')
    if (idx < 0) continue
    if (part.slice(0, idx).trim() !== name) continue
    try {
      return decodeURIComponent(part.slice(idx + 1).trim())
    } catch {
      return part.slice(idx + 1).trim()
    }
  }
  return ''
}
