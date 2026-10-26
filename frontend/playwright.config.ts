import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E 配置 —— 对应任务书 4.6 DoD 第③条。
 *
 *   「关键路径 E2E 通过（接管 → 上下文卡片 → 知识检索 → 回复 → 释放）」
 *
 * 几个刻意的决定：
 *
 * 1. 走 dev server（端口 5273）+ 内置 BFF Mock，而不是 preview(dist)。
 *    因为 Mock 中间件只挂在 Vite dev server 上；E2E 要验证的就是
 *    「前端 → BFF → 渲染」这条真实链路，而不是静态页面长得对不对。
 *    CI 里接真实 BFF 时，把 VITE_BFF_TARGET 指过去即可，用例不用改。
 *
 * 2. retries=0、workers=1、fullyParallel=false。
 *    Mock 的 store 是进程内单例（会话状态、接管归属都是共享的），
 *    并发跑会互相改状态导致偶发失败。E2E 只有关键路径几条，
 *    串行 30 秒内能跑完，稳定性优先于速度。
 *
 * 3. 只在 chromium 上跑。
 *    任务书 4.1 明确「坐席台是 PC 端，本期不做移动端适配」，
 *    跨浏览器矩阵属于后续版本的事。
 */
export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  forbidOnly: !!process.env.CI,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: process.env.CI
    ? [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]]
    : [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:5273',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    viewport: { width: 1600, height: 960 },
    // 生产环境 CSP 禁止任何外链，E2E 同样不加载任何外部资源
    serviceWorkers: 'block',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1600, height: 960 },
        // 复用本机已安装的 Chrome，而不是下载 Playwright 自带内核。
        // 原因：受限环境里 `playwright install` 要访问 CDN，常被网络策略挡住；
        // 而本机 Chrome 版本足够新，跑这套断言没有任何差异。
        // CI 里没有系统 Chrome 时，删掉这一行并跑一次 `npx playwright install chromium` 即可。
        channel: 'chrome',
      },
    },
  ],
  webServer: {
    // 端口写死并 strictPort 由 vite 配置保证；复用已在跑的 dev server，CI 里自动拉起
    command: 'npx vite --port 5273 --strictPort',
    url: 'http://127.0.0.1:5273',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
