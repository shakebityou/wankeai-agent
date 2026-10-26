import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:5273'
const OUT = 'shots'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 }, locale: 'zh-CN' })

page.on('console', (m) => {
  if (m.type() === 'error' || m.type() === 'warning') console.log(`[${m.type()}]`, m.text().slice(0, 300))
})
page.on('pageerror', (e) => console.log('[pageerror]', e.message.slice(0, 400)))
page.on('requestfailed', (r) => console.log('[reqfail]', r.url().slice(0, 160), r.failure()?.errorText))

await page.goto(BASE)
await page.evaluate(() => window.localStorage.clear())
await page.goto(BASE)
await page.waitForURL(/#\/login/, { timeout: 15000 })
await page.locator('.role-card', { hasText: '王芳' }).first().click()
await page.waitForURL(/#\/(console|admin)/, { timeout: 15000 })
await page.waitForTimeout(2500)

// 检查图标元素的尺寸与 computed style
const info = await page.evaluate(() => {
  const svgs = Array.from(document.querySelectorAll('svg')).slice(0, 6)
  return svgs.map((s) => {
    const cs = getComputedStyle(s)
    const r = s.getBoundingClientRect()
    return {
      html: s.outerHTML.slice(0, 120),
      w: Math.round(r.width),
      h: Math.round(r.height),
      fontSize: cs.fontSize,
      cls: s.getAttribute('class'),
    }
  })
})
console.log('SVG 采样：', JSON.stringify(info, null, 2))

const cssOk = await page.evaluate(() => {
  const el = document.querySelector('.wk-page') || document.body
  return getComputedStyle(el).getPropertyValue('--wk-color-brand') || '(无)'
})
console.log('设计令牌 --wk-color-brand =', cssOk)

await page.screenshot({ path: `${OUT}/debug-workbench.png` })
await browser.close()
