import type { Connect, Plugin } from 'vite'
import type { ServerResponse } from 'node:http'
import { handleBffRequest } from './handlers'
import type { MockAuth } from './http'
import { SESSION_COOKIE, readCookie } from './services/cookie'
import { resolveToken } from './services/auth-service'

/**
 * 本地 BFF Mock 中间件。
 *
 * 为什么不用 mockjs / vite-plugin-mock：
 * - 任务书要求「接口 Mock 配置对齐 X-09（联调与数据准备计划）」，需要真实还原
 *   2.5 节的统一响应包装、分页结构、6 个 BFF 错误码与 SSE 流式事件表；
 * - SSE（/bff/v1/chat/stream）必须逐帧推送才能验证打字机与断线重连，
 *   通用 mock 库不提供该能力。
 * - 账号登录要读写 SQLite 并维护 Cookie 会话，通用 mock 库同样做不到。
 *
 * 因此这里直接挂一个 Connect 中间件，把 /bff/v1/* 全部交给 handlers 处理。
 */
export function bffMockPlugin(): Plugin {
  return {
    name: 'wankeai-bff-mock',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use('/bff', createMiddleware())
    },
    configurePreviewServer(server) {
      server.middlewares.use('/bff', createMiddleware())
    },
  }
}

function createMiddleware(): Connect.NextHandleFunction {
  return (req, res, next) => {
    // 预检：Mock 层同样遵守「同源」原则，不做跨域放行。
    if (req.method === 'OPTIONS') {
      res.statusCode = 204
      res.end()
      return
    }

    const chunks: Buffer[] = []
    req.on('data', (chunk: Buffer) => chunks.push(chunk))
    req.on('end', () => {
      // 保留原始字节：FE-06 的 XLSX 上传是二进制，UTF-8 字符串会破坏它
      const rawBuffer = Buffer.concat(chunks)
      void dispatch(req, res, rawBuffer, next)
    })
    req.on('error', () => {
      next()
    })
  }
}

async function dispatch(
  req: Connect.IncomingMessage,
  res: ServerResponse,
  rawBuffer: Buffer,
  next: Connect.NextFunction,
): Promise<void> {
  try {
    const host = req.headers.host ?? '127.0.0.1'
    const url = new URL(req.url ?? '/', `http://${host}`)
    const contentType = String(req.headers['content-type'] ?? '')
    // multipart 是二进制（FE-06 的 XLSX），不能当文本解析
    const isBinary = contentType.includes('multipart/form-data')
    const rawBody = isBinary ? '' : rawBuffer.toString('utf-8')
    const headers = req.headers as Record<string, string | string[] | undefined>
    const handled = await handleBffRequest({
      method: (req.method ?? 'GET').toUpperCase(),
      path: url.pathname,
      query: url.searchParams,
      body: rawBody ? safeParse(rawBody) : undefined,
      rawBody,
      rawBuffer,
      headers,
      res,
      auth: resolveAuth(headers),
    })
    if (!handled) next()
  } catch (error) {
    // Mock 层自身异常也要按 2.5.5 的失败包装返回，便于前端验证兜底分支。
    const message = error instanceof Error ? error.message : String(error)
    res.statusCode = 500
    res.setHeader('Content-Type', 'application/json; charset=utf-8')
    res.end(
      JSON.stringify({
        code: 30001,
        message: `Mock 处理异常：${message}`,
        request_id: `mock-err-${Date.now()}`,
        data: null,
      }),
    )
  }
}

/**
 * 解析登录态。
 *
 * 这里不区分「未登录」与「token 失效」—— 两者对下游都是 10002，
 * 前端统一跳登录页，没必要暴露更多信息。
 */
function resolveAuth(headers: Record<string, string | string[] | undefined>): MockAuth | null {
  const token = readCookie(headers, SESSION_COOKIE)
  if (!token) return null
  const user = resolveToken(token)
  if (!user) return null
  return {
    token,
    user_id: user.user_id,
    username: user.username,
    display_name: user.display_name,
    role: user.role,
    group_id: user.group_id,
    group_name: user.group_name,
    channels: user.channels,
  }
}

function safeParse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}
