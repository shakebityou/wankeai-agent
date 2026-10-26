/**
 * 账号登录链路实测 —— 直接打真实 HTTP 接口，验证 Cookie 会话生效。
 *
 * 为什么不用 curl.exe：Windows 沙箱下 curl 走 schannel 会报
 * SEC_E_NO_CREDENTIALS，打不了本地 https；这里改用 Node 原生 fetch，
 * 并手动管理 Cookie（Node fetch 不像浏览器会自动带 Cookie）。
 */
const BASE = 'http://127.0.0.1:5273/bff/v1'

let cookie = ''
let passed = 0
let failed = 0

function check(name, cond, detail = '') {
  if (cond) {
    passed += 1
    console.log(`  ✓ ${name}`)
  } else {
    failed += 1
    console.log(`  ✗ ${name}${detail ? ` —— ${detail}` : ''}`)
  }
}

async function call(path, init = {}) {
  const headers = { 'Content-Type': 'application/json', ...(init.headers ?? {}) }
  if (cookie) headers.Cookie = cookie

  const res = await fetch(`${BASE}${path}`, { ...init, headers })

  // 手动收 Cookie：Set-Cookie 里只取 name=value 段
  const setCookie = res.headers.getSetCookie?.() ?? []
  for (const raw of setCookie) {
    const first = raw.split(';')[0]
    if (!first) continue
    const [name] = first.split('=')
    if (name === 'wk_session') cookie = first
  }

  const body = await res.json().catch(() => null)
  return { status: res.status, body, headers: res.headers }
}

const run = async () => {
  console.log('\n[1] 未登录访问受保护接口')
  const anon = await call('/auth/me')
  check('返回 401', anon.status === 401, String(anon.status))
  check('错误码 10002', anon.body?.code === 10002, String(anon.body?.code))

  console.log('\n[2] 登录校验')
  const noPwd = await call('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: 'wangfang', password: '' }),
  })
  check('空密码返回 10001', noPwd.body?.code === 10001, String(noPwd.body?.code))

  const wrong = await call('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: 'wangfang', password: 'wrong-pass-1' }),
  })
  check('密码错返回 401', wrong.status === 401, String(wrong.status))
  check('错误码 10002', wrong.body?.code === 10002)
  check('提示剩余次数', String(wrong.body?.message).includes('还可尝试'), wrong.body?.message)

  console.log('\n[3] 登录成功')
  const loginRes = await call('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: 'wangfang', password: 'Wk@2026demo' }),
  })
  check('返回成功', loginRes.body?.code === 0, JSON.stringify(loginRes.body?.message))
  check('已种下 Cookie', cookie.startsWith('wk_session='), cookie.slice(0, 20))
  check('响应不含 password_hash', !JSON.stringify(loginRes.body).includes('scrypt$'))
  check('角色为 AGENT', loginRes.body?.data?.user?.role === 'AGENT')
  check('分组为售后处理组', loginRes.body?.data?.user?.group_name === '售后处理组')

  console.log('\n[4] 带 Cookie 取当前用户')
  const me = await call('/auth/me')
  check('me 成功', me.body?.code === 0, JSON.stringify(me.body?.message))
  check('返回王芳', me.body?.data?.user?.display_name === '王芳')

  console.log('\n[5] 普通账号访问账号管理')
  const denied = await call('/users')
  check('坐席被拒 403', denied.status === 403, String(denied.status))
  check('错误码 10003', denied.body?.code === 10003, String(denied.body?.code))

  console.log('\n[6] 业务接口能取到当前操作人')
  const takeoverSession = await call('/sessions?page_size=1')
  const firstId = takeoverSession.body?.data?.items?.[0]?.session_id
  check('会话列表可访问', Boolean(firstId), String(takeoverSession.body?.code))
  if (firstId) {
    const tk = await call(`/sessions/${encodeURIComponent(firstId)}/takeover`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    check('接管成功', tk.body?.code === 0 || tk.body?.code === 10005, JSON.stringify(tk.body?.message))
    if (tk.body?.code === 0) {
      check('接管人来自登录态', tk.body?.data?.agent_name === '王芳', tk.body?.data?.agent_name)
      check('agent_id 为 U0001', tk.body?.data?.agent_id === 'U0001', tk.body?.data?.agent_id)
      await call(`/sessions/${encodeURIComponent(firstId)}/release`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
    }
  }

  console.log('\n[7] 登出')
  const out = await call('/auth/logout', { method: 'POST', body: '{}' })
  check('登出成功', out.body?.code === 0)
  const afterOut = await call('/auth/me')
  check('登出后 me 失效', afterOut.status === 401, String(afterOut.status))

  console.log('\n[8] 管理员登录')
  cookie = ''
  const admin = await call('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username: 'chenjing', password: 'Wk@2026demo' }),
  })
  check('管理员登录成功', admin.body?.code === 0, JSON.stringify(admin.body?.message))

  const users = await call('/users')
  check('账号列表可访问', users.body?.code === 0, JSON.stringify(users.body?.message))
  check('返回 7 个种子账号', users.body?.data?.total >= 7, String(users.body?.data?.total))
  check('列表不含哈希', !JSON.stringify(users.body).includes('scrypt$'))
  check('有按角色分布统计', typeof users.body?.data?.summary?.by_role?.ADMIN === 'number')

  const auditRes = await call('/audit?limit=10')
  check('审计日志可访问', auditRes.body?.code === 0, JSON.stringify(auditRes.body?.message))
  check('审计含登录记录', JSON.stringify(auditRes.body?.data).includes('LOGIN'))
  check('审计不含密码', !JSON.stringify(auditRes.body?.data).includes('Wk@2026'))

  console.log('\n[9] 建账号 → 强制改密 → 登录')
  // 每次跑用不同的账号名，脚本可重复执行（SQLite 是持久化的）
  const uniq = `e2e${Date.now().toString(36).slice(-6)}`
  const created = await call('/users', {
    method: 'POST',
    body: JSON.stringify({
      username: uniq,
      display_name: '联调测试',
      role: 'AGENT',
      group_id: 'G2',
    }),
  })
  check('建账号成功', created.body?.code === 0, JSON.stringify(created.body?.message))
  check('must_change 为 true', created.body?.data?.user?.must_change === true)

  const dupCreate = await call('/users', {
    method: 'POST',
    body: JSON.stringify({ username: uniq.toUpperCase(), display_name: '重名', role: 'AGENT' }),
  })
  check('重名被拒', dupCreate.body?.code === 10001, String(dupCreate.body?.code))

  console.log('\n[10] 自我保护')
  // 用「当前登录的管理员自己」的 user_id 去试，否则测的其实是别人的账号
  const myId = admin.body?.data?.user?.user_id ?? 'U0004'
  const self = await call(`/users/${myId}`, {
    method: 'PUT',
    body: JSON.stringify({ status: 'DISABLED' }),
  })
  check('不能禁用自己', self.body?.code === 10001, JSON.stringify(self.body?.message))
  check('提示说明原因', String(self.body?.message).includes('自己'), self.body?.message)

  const selfRole = await call(`/users/${myId}`, {
    method: 'PUT',
    body: JSON.stringify({ role: 'AGENT' }),
  })
  check('不能降级自己', selfRole.body?.code === 10001, JSON.stringify(selfRole.body?.message))

  console.log(`\n结果：${passed} 通过 / ${failed} 失败\n`)
  if (failed > 0) process.exit(1)
}

run().catch((error) => {
  console.error('测试异常：', error)
  process.exit(1)
})
