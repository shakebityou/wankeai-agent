import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:5273'
const OUT = 'shots'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 }, locale: 'zh-CN' })

async function shot(name) {
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
  console.log('shot', name)
}

// 1. 登录页
await page.goto(BASE)
await page.evaluate(() => window.localStorage.clear())
await page.goto(BASE)
await page.waitForURL(/#\/login/, { timeout: 15000 })
await shot('01-login')

// 2. 工作台：点坐席王芳
await page.locator('.role-card', { hasText: '王芳' }).first().click()
await page.waitForURL(/#\/(console|admin)/, { timeout: 15000 })
await page.waitForTimeout(1200)
await shot('02-workbench')

// 3. 选一个含机器人标签的会话
const bot = page.locator('.session-item', { hasText: '机器人' }).first()
const cnt = await bot.count()
if (cnt > 0) {
  await bot.click()
} else {
  await page.locator('.session-item').first().click()
}
await page.waitForURL(/#\/console\/sessions\/[^/]+$/, { timeout: 15000 })
await page.waitForTimeout(1500)
await shot('03-session-detail')

// 4. 知识检索
const kb = page.getByPlaceholder('检索知识库（商品 / 政策 / FAQ）')
if (await kb.count()) {
  await kb.fill('奶油小熊')
  await page.waitForTimeout(1200)
  await shot('04-kb-search')
}

// 5. 接管（弹窗）
const takeover = page.getByRole('button', { name: '接管会话' })
if (await takeover.count()) {
  await takeover.first().click()
  await page.waitForTimeout(600)
  await shot('05-takeover-confirm')
  const ok = page.getByRole('button', { name: '确认接管' })
  if (await ok.count()) {
    await ok.first().click()
    await page.waitForTimeout(1200)
    await shot('06-taken-over')
  }
}

// 6. 运营后台指标看板
await page.goto(`${BASE}/#/admin/metrics`)
await page.waitForTimeout(1800)
await shot('07-metrics')

// 7. 知识库
await page.goto(`${BASE}/#/admin/kb`)
await page.waitForTimeout(1500)
await shot('08-kb-list')

// 8. 排班
await page.goto(`${BASE}/#/admin/agent-groups`)
await page.waitForTimeout(1500)
await shot('09-agent-groups')

// 9. 转人工规则
await page.goto(`${BASE}/#/admin/handoff-rules`)
await page.waitForTimeout(1500)
await shot('10-handoff-rules')

await browser.close()
console.log('done')
