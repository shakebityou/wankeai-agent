import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5273'
const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 }, locale: 'zh-CN' })

await page.goto(BASE)
await page.evaluate(() => window.localStorage.clear())
await page.goto(BASE)
await page.waitForURL(/#\/login/, { timeout: 15000 })
await page.locator('.role-card', { hasText: '陈静' }).first().click()
await page.waitForURL(/#\/(console|admin)/, { timeout: 15000 })

await page.goto(`${BASE}/#/console/sessions`)
await page.waitForTimeout(1800)
const sid = await page.evaluate(() => {
  const first = document.querySelector('.session-item')
  if (!first) return ''
  const onclick = first.outerHTML
  return onclick
})
console.log('首条会话 HTML 片段:', sid.slice(0, 400))

// 点进第一个会话再读 URL
await page.locator('.session-item').first().click()
await page.waitForURL(/#\/console\/sessions\/[^/]+$/, { timeout: 15000 })
const url = page.url()
console.log('会话详情 URL:', url)
const id = url.split('/').pop()
console.log('sessionId =', id)

await page.goto(`${BASE}/#/console/replay/${id}`)
await page.waitForTimeout(2500)
await page.screenshot({ path: 'shots/18-replay.png' })
console.log('shot 18-replay')

await browser.close()
