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
  // 侧栏里到底渲染了什么
  const side = document.querySelector('.workbench__side')
  out.sideHtml = side ? side.innerHTML.slice(0, 900) : '(无 .workbench__side)'
  // 所有超大 svg 的父级链
  const big = Array.from(document.querySelectorAll('svg')).filter((s) => s.getBoundingClientRect().width > 60)
  out.bigSvgChains = big.slice(0, 3).map((s) => {
    const chain = []
    let n = s
    for (let i = 0; i < 5 && n; i += 1) {
      const cs = getComputedStyle(n)
      chain.push(`${n.tagName.toLowerCase()}.${n.className || '(无class)'} w=${cs.width} h=${cs.height} fs=${cs.fontSize}`)
      n = n.parentElement
    }
    return chain
  })
  // select 的 svg 是否有 el-icon 父级
  const svgInFilter = document.querySelector('.session-filter svg')
  out.selectSvgParentClass = svgInFilter ? svgInFilter.parentElement?.className : '(无 svg in filter)'
  // 检查是否有 .el-icon 元素
  out.elIconCount = document.querySelectorAll('.el-icon').length
  // 检查 select 组件实际类名
  out.selectLikeClasses = Array.from(document.querySelectorAll('[class*="select"]')).slice(0, 8).map((e) => e.className)
  return out
})
console.log(JSON.stringify(probe, null, 2))
await browser.close()
