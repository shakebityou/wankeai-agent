/**
 * 账号体系截图 —— 登录页 / 登录失败态 / 账号管理页 / 改密页。
 *
 * 用法：node scripts/screenshot-auth.mjs
 * 前置：dev 服务跑在 5273，且本机装了 Chrome（用 channel: 'chrome'）。
 */
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'shots')
mkdirSync(outDir, { recursive: true })

const BASE = 'http://127.0.0.1:5273'
const PASSWORD = 'Wk@2026demo'

const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1600, height: 960 }, locale: 'zh-CN' })

async function shot(name) {
  await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(outDir, `${name}.png`) })
  console.log('✓', name)
}

async function login(username) {
  await page.goto(`${BASE}/`)
  await page.context().clearCookies()
  await page.evaluate(() => window.localStorage.clear())
  await page.goto(`${BASE}/#/login`)
  await page.reload()
  await page.waitForSelector('input[placeholder="请输入登录账号"]')
  await page.fill('input[placeholder="请输入登录账号"]', username)
  await page.fill('input[placeholder="请输入密码"]', PASSWORD)
  await page.click('button:has-text("登 录")')
  await page.waitForTimeout(2500)
}

// 1. 登录页（带演示账号展开）
await page.goto(`${BASE}/`)
await page.context().clearCookies()
await page.evaluate(() => window.localStorage.clear())
await page.goto(`${BASE}/#/login`)
await page.reload()
await page.waitForSelector('input[placeholder="请输入登录账号"]')
await shot('auth-01-登录页')

// 2. 展开演示账号
await page.click('button:has-text("展开演示账号")')
await shot('auth-02-演示账号展开')

// 3. 密码错误态
await page.fill('input[placeholder="请输入登录账号"]', 'wangfang')
await page.fill('input[placeholder="请输入密码"]', 'wrong-password')
await page.click('button:has-text("登 录")')
await page.waitForTimeout(1200)
await shot('auth-03-密码错误')

// 4. 账号管理页（管理员）
await login('chenjing')
await page.goto(`${BASE}/#/admin/users`)
await page.waitForTimeout(2500)
await shot('auth-04-账号管理')

// 5. 新建账号弹窗
const createBtn = page.locator('button:has-text("新建账号")').first()
if (await createBtn.count()) {
  await createBtn.click()
  await page.waitForTimeout(800)
  await shot('auth-05-新建账号弹窗')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
}

// 6. 改密页
await page.goto(`${BASE}/#/account/password`)
await page.waitForTimeout(1500)
await shot('auth-06-修改密码')

await browser.close()
console.log(`\n截图输出：${outDir}`)
