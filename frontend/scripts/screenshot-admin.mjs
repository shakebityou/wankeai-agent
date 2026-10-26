import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:5273'
const OUT = 'shots'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 }, locale: 'zh-CN' })

async function loginAs(name) {
  await page.goto(BASE)
  await page.evaluate(() => window.localStorage.clear())
  await page.goto(BASE)
  await page.waitForURL(/#\/login/, { timeout: 15000 })
  await page.locator('.role-card', { hasText: name }).first().click()
  await page.waitForURL(/#\/(console|admin)/, { timeout: 15000 })
  await page.waitForTimeout(800)
}

async function shot(name) {
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('shot', name)
}

// 管理员视角：所有后台页
await loginAs('陈静')
for (const [name, path] of [
  ['11-admin-kb-list', '/#/admin/kb'],
  ['12-admin-kb-import', '/#/admin/kb/import'],
  ['13-admin-versions', '/#/admin/kb/KB-0001/versions'],
  ['14-admin-feedback', '/#/admin/feedback'],
  ['15-admin-channels', '/#/admin/channels'],
  ['16-admin-groups', '/#/admin/agent-groups'],
  ['17-admin-rules', '/#/admin/handoff-rules'],
]) {
  await page.goto(`${BASE}${path}`)
  await page.waitForTimeout(1600)
  await shot(name)
}

// 回放页：先拿一个会话 id
await page.goto(`${BASE}/#/console/sessions`)
await page.waitForTimeout(1500)
const sid = await page.evaluate(() => {
  const a = document.querySelector('.session-item')
  return a ? 'CS20261006-0320' : ''
})
await page.goto(`${BASE}/#/console/replay/${sid}`)
await page.waitForTimeout(2000)
await shot('18-replay')

await browser.close()
console.log('done')
