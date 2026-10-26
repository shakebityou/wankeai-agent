import { expect, type Page } from '@playwright/test'

/**
 * E2E 公共动作。
 *
 * 集中在这里而不是散在用例里，是因为「登录 → 打开某个会话」这串动作
 * 几乎每条用例都要做，一旦登录页/列表结构微调，只改一处。
 */

/** 演示账号密码（与 mock/seed/auth-db.ts 的 DEMO_PASSWORD 一致） */
export const DEMO_PASSWORD = 'Wk@2026demo'

const ACCOUNTS: Record<string, string> = {
  王芳: 'wangfang',
  李敏: 'limin',
  张涛: 'zhangtao',
  陈静: 'chenjing',
  刘洋: 'liuyang',
  赵磊: 'zhaolei',
  孙悦: 'sunyue',
}

/**
 * 账号密码登录。
 *
 * 为什么先清 Cookie 再进登录页：
 * 登录态现在由 HttpOnly Cookie 承载（JS 读不到，也不该读）。
 * Playwright 的每个 test 虽然默认独立上下文，但同一用例里切换角色要显式清，
 * 否则会带着上一个人的会话直接进系统，登录页根本不会出现。
 */
export async function loginAs(
  page: Page,
  name: '王芳' | '李敏' | '张涛' | '陈静' | '刘洋' | '赵磊' | '孙悦',
  password = DEMO_PASSWORD,
): Promise<void> {
  await page.goto('/')
  await page.context().clearCookies()
  await page.evaluate(() => window.localStorage.clear())
  // 用 full reload 回到登录页：单靠 goto('/#/login') 在 hash 路由下
  // 可能只改 hash 不重新加载，SPA 里残留的旧身份会把人直接送进系统。
  await page.goto('/#/login')
  await page.reload()
  await page.waitForURL(/#\/login/)
  await expect(page.getByPlaceholder('请输入登录账号')).toBeVisible({ timeout: 15_000 })

  const username = ACCOUNTS[name] ?? ''
  expect(username, `未知演示账号 ${name}`).not.toBe('')

  await page.getByPlaceholder('请输入登录账号').fill(username)
  await page.getByPlaceholder('请输入密码').fill(password)
  await page.getByRole('button', { name: '登 录' }).click()

  // 等「已经进到某个系统里」而不是等 URL 变化：
  // 同一用例里连续登录多个角色时，上一个人可能也落在同一个路径上，
  // 只盯 URL 会一直等不到变化。这里以布局容器出现为准。
  await expect(page.locator('.console-layout, .admin-layout, .pwd')).toBeVisible({
    timeout: 15_000,
  })
}

/** 只填表单点登录，不等待跳转 —— 用于断言失败分支 */
export async function attemptLogin(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/#/login')
  await page.waitForURL(/#\/login/)
  await page.getByPlaceholder('请输入登录账号').fill(username)
  await page.getByPlaceholder('请输入密码').fill(password)
  await page.getByRole('button', { name: '登 录' }).click()
}

/** 进入坐席工作台并等待会话列表出数据 */
export async function openWorkbench(page: Page): Promise<void> {
  await page.goto('/#/console/sessions')
  await expect(page.locator('.session-list__items .session-item').first()).toBeVisible({
    timeout: 15_000,
  })
}

/**
 * 点开第一个「机器人接待中」的会话。
 *
 * 为什么挑机器人接待的而不是随便点第一个：
 * 接管用例需要一个**尚未被接管**的会话，否则按钮文案是「释放回机器人」，
 * 整条关键路径的前提就不成立。
 */
export async function openBotSession(page: Page): Promise<string> {
  const item = page
    .locator('.session-item')
    .filter({ has: page.locator('.wk-pill--mint', { hasText: '机器人' }) })
    .first()
  await expect(item).toBeVisible({ timeout: 15_000 })
  await item.click()

  // 会话打开后 URL 会带上 sessionId，把它取出来给用例断言用
  await page.waitForURL(/#\/console\/sessions\/.+/)
  const sessionId = page.url().split('/').pop() ?? ''
  expect(sessionId).not.toBe('')

  // 中栏出消息流 = 详情接口已返回
  await expect(page.locator('.stream')).toBeVisible({ timeout: 15_000 })
  return sessionId
}

/** 消息流里消息条数 */
export function messageCount(page: Page) {
  return page.locator('.stream .bubble-row')
}

/**
 * 全局断言：页面上不存在任何可点击的外链（HC-1）。
 *
 * 放在 E2E 里而不是只靠静态扫描，是因为静态扫描看不到
 * 「后端下发的内容被 v-html 渲染成链接」这类运行时才出现的外链。
 */
export async function expectNoAnchor(page: Page): Promise<void> {
  const anchors = page.locator('a[href]')
  const count = await anchors.count()
  if (count > 0) {
    const hrefs = await anchors.evaluateAll((els) =>
      els.map((el) => (el as HTMLAnchorElement).getAttribute('href') ?? ''),
    )
    // 允许纯锚点（#xxx）用于页内跳转，其余一律不允许
    const external = hrefs.filter((href) => !href.startsWith('#') && href !== '')
    expect(external, `页面出现外链：${external.join(', ')}`).toEqual([])
  }
}

/** 等待 Element Plus 的 message 提示出现并返回文案 */
export async function readToast(page: Page): Promise<string> {
  const toast = page.locator('.el-message').last()
  await expect(toast).toBeVisible()
  return (await toast.textContent()) ?? ''
}
