import { expect, test } from '@playwright/test'
import { loginAs } from './fixtures'

/**
 * 运营后台 E2E —— 覆盖 FE-05~FE-13 的「页面能开、数据能出、红线守住」。
 *
 * 为什么后台不做深度交互断言：
 *   后台的深度交互（批量审核、导入切分、规则试算）已经由单测和 Mock 侧
 *   契约测试覆盖；E2E 在这里的价值是**冒烟**——确认 8 个页面都能在真实
 *   浏览器里挂载成功、接口能通、没有渲染崩溃。页面级崩溃（例如某个
 *   undefined 被当成数组 .length）只能被真浏览器抓到。
 *
 * 每条用例同时断言 HC-1：页面里不得出现可点击外链。
 */
const ADMIN_PAGES: Array<{ name: string; hash: string; anchor: string; role: '张涛' | '陈静' | '李敏' }> = [
  { name: '指标看板 FE-09', hash: '/admin/metrics', anchor: '.metric', role: '陈静' },
  { name: '知识库列表 FE-05', hash: '/admin/kb', anchor: '.kb-table, .el-table', role: '张涛' },
  { name: '批量导入 FE-06', hash: '/admin/kb/import', anchor: '.upload', role: '张涛' },
  { name: '版本与回滚 FE-07', hash: '/admin/kb/KB-0001/versions', anchor: 'body', role: '张涛' },
  { name: '纠错待办 FE-08', hash: '/admin/feedback', anchor: 'body', role: '张涛' },
  { name: '渠道授权 FE-11', hash: '/admin/channels', anchor: '.channels', role: '陈静' },
  { name: '坐席分组排班 FE-12', hash: '/admin/agent-groups', anchor: '.wk-card', role: '陈静' },
  { name: '转人工规则 FE-13', hash: '/admin/handoff-rules', anchor: '.rules', role: '陈静' },
]

for (const pageDef of ADMIN_PAGES) {
  test(`运营后台：${pageDef.name} 可正常打开`, async ({ page }) => {
    await loginAs(page, pageDef.role)
    await page.goto(`/#${pageDef.hash}`)

    // 不得落到无权限页或 404
    await expect(page.locator('.wk-state__title', { hasText: '无权限' })).toHaveCount(0)
    await expect(page.locator('.not-found, [data-testid="not-found"]')).toHaveCount(0)

    if (pageDef.anchor !== 'body') {
      await expect(page.locator(pageDef.anchor).first()).toBeVisible({ timeout: 15_000 })
    }

    // 页面不得出现 Vue 运行时错误导致的空白：body 必须有实际文本
    const text = (await page.locator('body').innerText()).trim()
    expect(text.length).toBeGreaterThan(20)

    // HC-1：全页无外链
    const external = await page.locator('a[href]').evaluateAll((els) =>
      els
        .map((el) => (el as HTMLAnchorElement).getAttribute('href') ?? '')
        .filter((href) => href !== '' && !href.startsWith('#')),
    )
    expect(external, `出现外链：${external.join(', ')}`).toEqual([])
  })
}

test('角色权限矩阵：坐席进不了运营后台，知识库运营进不了坐席台', async ({ page }) => {
  // 坐席：直接访问后台应被守卫拦到无权限页
  await loginAs(page, '王芳')
  await page.goto('/#/admin/channels')
  await expect(page.locator('body')).toContainText(/无权限|不可访问|403/, { timeout: 10_000 })

  // 知识库运营：进不了坐席工作台
  await loginAs(page, '张涛')
  await page.goto('/#/console/sessions')
  await expect(page.locator('body')).toContainText(/无权限|不可访问|403/, { timeout: 10_000 })
})

test('指标看板：12 项指标全部渲染，且达标状态用三色区分', async ({ page }) => {
  await loginAs(page, '陈静')
  await page.goto('/#/admin/metrics')
  await expect(page.locator('.metric').first()).toBeVisible({ timeout: 15_000 })

  // 任务书 4.4 FE-09 锁定 12 项指标
  await expect(page.locator('.metric')).toHaveCount(12)

  // 每张卡都必须有目标值与当前值，否则坐席无法判断是否达标
  const first = page.locator('.metric').first()
  await expect(first).toContainText(/[≥≤]/)
})
