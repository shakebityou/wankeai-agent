import { expect, test } from '@playwright/test'
import { attemptLogin, loginAs, readToast } from './fixtures'

/**
 * 账号登录体系 E2E。
 *
 * 覆盖四件事：
 *   1. 未登录访问受保护页面会被挡到登录页（服务端鉴权，不是前端假拦）；
 *   2. 密码错有明确提示，且不会把人放进系统；
 *   3. 四个角色的落点不同（坐席进工作台，后台角色进对应后台）；
 *   4. 账号管理页只有管理员能进，且能新建/停用账号。
 */
test.describe('账号登录体系', () => {
  test('未登录访问受保护页面会被挡到登录页', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()
    await page.goto('/#/console/sessions')

    await page.waitForURL(/#\/login/, { timeout: 10_000 })
    await expect(page.getByPlaceholder('请输入登录账号')).toBeVisible()
    await expect(page.getByRole('button', { name: '登 录' })).toBeVisible()
  })

  test('密码错误时给出明确提示且不放行', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()

    await attemptLogin(page, 'wangfang', 'totally-wrong-1')

    const alert = page.locator('.login__error')
    await expect(alert).toBeVisible({ timeout: 10_000 })
    await expect(alert).toContainText('账号或密码不正确')
    // 还在登录页，没有进入系统
    await expect(page).toHaveURL(/#\/login/)
  })

  test('账号不存在时提示与密码错一致（防账号枚举）', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()

    await attemptLogin(page, 'no_such_user_here', 'whatever123')

    const alert = page.locator('.login__error')
    await expect(alert).toBeVisible({ timeout: 10_000 })
    await expect(alert).toContainText('账号或密码不正确')
    await expect(page).toHaveURL(/#\/login/)
  })

  test('四个角色登录后落到各自的首页', async ({ page }) => {
    const cases: Array<{ name: '王芳' | '李敏' | '张涛' | '陈静'; expect: RegExp }> = [
      { name: '王芳', expect: /#\/console\/sessions/ },
      { name: '李敏', expect: /#\/console\/sessions/ },
      { name: '张涛', expect: /#\/admin\/(metrics|kb)/ },
      { name: '陈静', expect: /#\/admin\/(metrics|kb)/ },
    ]

    for (const item of cases) {
      await loginAs(page, item.name)
      await expect(page).toHaveURL(item.expect)
      // 顶栏显示的是登录进去的那个人，不是硬编码的演示身份
      await expect(page.locator('.console-topbar__name, .admin-topbar__name')).toContainText(item.name)
    }
  })

  test('演示账号展开后点击可自动填入表单', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()
    await page.goto('/#/login')

    await page.getByRole('button', { name: /展开演示账号|收起演示账号/ }).click()
    const row = page.locator('.demo-row', { hasText: '陈静' })
    await expect(row).toBeVisible()
    await row.click()

    await expect(page.getByPlaceholder('请输入登录账号')).toHaveValue('chenjing')
    await expect(page.getByPlaceholder('请输入密码')).not.toHaveValue('')
  })

  test('退出登录后回到登录页且不能再访问工作台', async ({ page }) => {
    await loginAs(page, '王芳')
    await expect(page).toHaveURL(/#\/console\/sessions/)

    await page.getByRole('button', { name: '退出' }).click()
    await page.getByRole('button', { name: '确认退出' }).click()

    await page.waitForURL(/#\/login/, { timeout: 10_000 })

    // 直接输地址也进不去 —— Cookie 已被服务端吊销
    await page.goto('/#/console/sessions')
    await page.waitForURL(/#\/login/, { timeout: 10_000 })
  })

  test('坐席访问账号管理被挡到无权限页', async ({ page }) => {
    await loginAs(page, '王芳')
    await page.goto('/#/admin/users')

    await page.waitForURL(/#\/(forbidden|console)/, { timeout: 10_000 })
    const body = await page.locator('body').textContent()
    expect(body ?? '').toMatch(/无权限|不可访问|403/)
  })

  test('管理员能在账号管理页看到全部账号并按角色统计', async ({ page }) => {
    await loginAs(page, '陈静')
    await page.goto('/#/admin/users')

    await expect(page.locator('.users__title')).toHaveText('账号管理', { timeout: 10_000 })
    // 账号数随建号用例增长，这里只断言「至少 7 个种子账号」，不比精确值 ——
    // 否则用例之间互相污染（前一条用例建了号，后一条就挂）
    const rowCount = await page.locator('.users__table .el-table__row').count()
    expect(rowCount).toBeGreaterThanOrEqual(7)
    await expect(page.locator('.users__total')).toContainText('共')
    await expect(page.locator('.users__table')).toContainText('wangfang')

    // 按角色分布：四类角色都要有数字
    const roleStat = page.locator('.stat__roles')
    await expect(roleStat).toContainText('坐席')
    await expect(roleStat).toContainText('系统管理员')

    // 列表里不该出现任何密码哈希
    const tableText = (await page.locator('.users__table').textContent()) ?? ''
    expect(tableText).not.toContain('scrypt$')

    // 自己的停用按钮应当是禁用的
    const selfRow = page.locator('.users__table .el-table__row', { hasText: '陈静' })
    await expect(selfRow).toContainText('本人')
    await expect(selfRow.getByRole('button', { name: '停用' })).toBeDisabled()
  })

  test('管理员新建账号后列表里能看到', async ({ page }) => {
    await loginAs(page, '陈静')
    await page.goto('/#/admin/users')
    await expect(page.locator('.users__title')).toBeVisible({ timeout: 10_000 })

    await page.getByRole('button', { name: '新建账号' }).first().click()

    const dialog = page.getByRole('dialog', { name: '新建账号' })
    await expect(dialog).toBeVisible()

    const seq = Date.now().toString(36).slice(-5)
    await dialog.getByPlaceholder('3–32 位字母数字，用于登录').fill(`ui${seq}`)
    await dialog.getByPlaceholder('真实姓名，会显示在会话与工单记录里').fill('界面测试')
    await dialog.getByRole('button', { name: '保存' }).click()

    const row = page.locator('.users__table tr', { hasText: `ui${seq}` })
    await expect(row).toBeVisible({ timeout: 10_000 })

    // 操作留痕
    await expect(page.locator('.audit')).toContainText('新建账号', { timeout: 10_000 })

    // 用完即停用：SQLite 是长期文件库，不清理会让账号表被测试数据撑爆。
    // 账号不做物理删除（audit_log 要能追溯），停用后统计卡里的「已停用」会 +1。
    await row.getByRole('button', { name: '停用' }).click()
    // 停用是危险操作，有二次确认弹窗
    await page.getByRole('button', { name: '确认停用' }).click()
    await expect(row).toContainText('已停用', { timeout: 10_000 })
  })

  test('管理员新建账号时账号格式非法会被拒', async ({ page }) => {
    await loginAs(page, '陈静')
    await page.goto('/#/admin/users')
    await expect(page.locator('.users__title')).toBeVisible({ timeout: 10_000 })

    await page.getByRole('button', { name: '新建账号' }).first().click()
    const dialog = page.getByRole('dialog', { name: '新建账号' })
    await dialog.getByPlaceholder('3–32 位字母数字，用于登录').fill('ab')
    await dialog.getByPlaceholder('真实姓名，会显示在会话与工单记录里').fill('短账号')
    await dialog.getByRole('button', { name: '保存' }).click()

    await expect(dialog).toContainText('3–32 位')
    // 弹窗没关，说明确实拦住了
    await expect(dialog).toBeVisible()
  })

  test('顶栏「改密」进入修改密码页，密码校验生效', async ({ page }) => {
    await loginAs(page, '王芳')
    await page.getByRole('button', { name: '改密' }).click()
    await page.waitForURL(/#\/account\/password/, { timeout: 10_000 })

    await expect(page.locator('.pwd__title')).toHaveText('修改密码')

    // 两次输入不一致要拦下来
    await page.getByPlaceholder('请输入当前密码').fill('whatever')
    await page.getByPlaceholder('至少 8 位，需含字母和数字').fill('NewPass1234')
    await page.getByPlaceholder('再输入一次新密码').fill('NewPass9999')
    await page.getByRole('button', { name: '确认修改' }).click()

    await expect(page.locator('.pwd__card')).toContainText('两次输入的密码不一致')
    await expect(page).toHaveURL(/#\/account\/password/)
  })

  test('登录页不出现外链（HC-1）', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()
    await page.goto('/#/login')

    const hrefs = await page.locator('a[href]').evaluateAll((els) =>
      els.map((el) => (el as HTMLAnchorElement).getAttribute('href') ?? ''),
    )
    const external = hrefs.filter((href) => !href.startsWith('#') && href !== '')
    expect(external).toEqual([])

    const text = (await page.locator('body').textContent()) ?? ''
    expect(text).not.toContain('http://')
    expect(text).not.toContain('https://')
  })

  test('登录成功后有欢迎提示', async ({ page }) => {
    await page.goto('/')
    await page.context().clearCookies()
    await attemptLogin(page, 'zhangtao', 'Wk@2026demo')
    await page.waitForURL(/#\/(console|admin|account)\/.+/, { timeout: 15_000 })
    const toast = await readToast(page)
    expect(toast).toContain('张涛')
  })
})
