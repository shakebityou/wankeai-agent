import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5273'
const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 } })

await page.goto(BASE)
await page.evaluate(() => window.localStorage.clear())
await page.goto(BASE)
await page.waitForURL(/#\/login/, { timeout: 15000 })
await page.locator('.role-card', { hasText: '王芳' }).first().click()
await page.waitForURL(/#\/(console|admin)/, { timeout: 15000 })
await page.waitForTimeout(2500)

const probe = await page.evaluate(() => {
  const out = {}
  // 1. meta CSP 实际内容
  const meta = document.querySelector('meta[http-equiv="Content-Security-Policy"]')
  out.csp = meta ? meta.getAttribute('content') : '(无 meta)'
  // 2. 样式表是否加载
  out.sheets = Array.from(document.styleSheets).map((s) => {
    try {
      return { href: (s.href || '(inline)').slice(-60), rules: s.cssRules.length }
    } catch {
      return { href: (s.href || '(inline)').slice(-60), rules: 'CORS-blocked' }
    }
  })
  // 3. 关键选择器是否存在
  out.keyClasses = ['.el-select', '.el-select__wrapper', '.session-item', '.session-filter', '.workbench__side']
    .map((sel) => ({ sel, count: document.querySelectorAll(sel).length }))
  // 4. .el-select__caret 图标宽度 与 .el-icon 样式
  const icon = document.querySelector('.el-icon svg')
  if (icon) {
    const cs = getComputedStyle(icon)
    out.iconSvgStyle = { width: cs.width, height: cs.height, fontSize: cs.fontSize }
    const p = icon.closest('.el-icon')
    if (p) {
      const pcs = getComputedStyle(p)
      out.iconParent = { cls: p.className, w: pcs.width, h: pcs.height, fs: pcs.fontSize }
    }
  }
  // 5. 搜索是否有 svg 尺寸规则
  let found = null
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      for (const rule of Array.from(sheet.cssRules)) {
        if (rule.cssText && rule.cssText.includes('.el-icon svg')) { found = rule.cssText.slice(0, 200); break }
      }
    } catch {}
    if (found) break
  }
  out.elIconSvgRule = found || '(未找到 .el-icon svg 规则)'
  return out
})
console.log(JSON.stringify(probe, null, 2))
await browser.close()
