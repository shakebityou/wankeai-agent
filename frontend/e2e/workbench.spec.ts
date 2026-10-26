import { expect, test } from '@playwright/test'
import {
  expectNoAnchor,
  loginAs,
  messageCount,
  openBotSession,
  openWorkbench,
  readToast,
} from './fixtures'

/**
 * 关键路径 E2E —— 任务书 4.6 DoD 第③条。
 *
 *   「关键路径 E2E 通过（接管 → 上下文卡片 → 知识检索 → 回复 → 释放）」
 *
 * 拆成五条独立用例而不是一条长用例，理由：
 *   一条 300 行的大用例挂在哪一步都只报「超时」，定位成本极高；
 *   五条里哪条红了，问题边界立刻清楚。代价是每条都要重新登录，
 *   但 Mock 数据是持久在内存里的单例，前一步的写操作会留给后一步。
 *   因此**用例之间不共享状态假设** —— 每条自己把会话摆到需要的状态。
 */

test.describe('坐席工作台关键路径', () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, '李敏') // 主管：全部会话可见 + 有接管权限
    await openWorkbench(page)
  })

  test('① 会话列表可用，点击进入详情并能看到七要素上下文卡片', async ({ page }) => {
    const sessionId = await openBotSession(page)

    // 中栏：买家、渠道、归属徽标
    await expect(page.locator('.main-head__buyer')).not.toBeEmpty()
    await expect(page.locator('.main-head__channel')).not.toBeEmpty()
    await expect(page.locator('.owner-badge')).toBeVisible()

    // 右栏：AI 上下文卡片必须是**完整七要素**，不能是守卫错误态
    // （守卫错误态在 E2E 里算失败：Mock 的种子数据本来就该是齐全的）
    await expect(page.locator('.context-card')).toBeVisible()
    await expect(page.locator('[data-testid="context-guard-error"]')).toHaveCount(0)
    await expect(page.locator('.context-card__error')).toHaveCount(0)
    await expect(page.locator('.context-card__summary')).not.toBeEmpty()

    // 七要素的可见证据：摘要 / 已核实 / 已尝试 / 未满足诉求 / 建议动作 / 情绪 / 轮次
    const headings = page.locator('.context-card__heading')
    await expect(headings.filter({ hasText: '对话摘要' })).toHaveCount(1)
    await expect(headings.filter({ hasText: '已验证事实' })).toHaveCount(1)
    await expect(headings.filter({ hasText: '已尝试动作' })).toHaveCount(1)
    await expect(page.locator('.overview-item', { hasText: '咨询轮次' })).toBeVisible()
    await expect(page.locator('.context-card .emotion')).toBeVisible()

    // 有等待时长时必须给坐席可读口径，而不是裸秒数
    const waiting = page.locator('.main-head__waiting')
    if (await waiting.count()) {
      await expect(waiting).toContainText(/秒|分/)
    }

    expect(sessionId).toMatch(/\S/)
    await expectNoAnchor(page)
  })

  test('② 接管 → 机器人回复模式切换为坐席模式', async ({ page }) => {
    await openBotSession(page)

    // 接管前：发送框提示这是「机器人接待中」
    await expect(page.locator('.composer__mode')).toContainText('机器人接待中')

    await page.getByRole('button', { name: '接管会话' }).click()
    const dialog = page.getByRole('dialog', { name: '确认接管该会话？' })
    await expect(dialog).toBeVisible()
    // 二次确认必须列出后果，不能只剩一个「确定」
    await expect(dialog.locator('.confirm__list li')).toHaveCount(3)

    await dialog.getByRole('button', { name: '确认接管' }).click()
    expect(await readToast(page)).toContain('接管成功')

    // 按钮翻面 + 归属徽标变为坐席 + 回复框切到坐席模式
    await expect(page.getByRole('button', { name: '释放回机器人' })).toBeVisible()
    await expect(page.locator('.owner-badge')).toContainText('坐席')
    await expect(page.locator('.composer__mode')).toContainText('坐席回复模式')
    await expect(page.getByPlaceholder(/将以「坐席」身份发送给买家/)).toBeVisible()

    // 接管后机器人已静音 —— 这一点由徽标上的 muted 标记给出可见证据
    await expect(page.locator('.owner-badge__muted')).toBeVisible()
  })

  test('③ 知识检索只出已审核条目，可一键填入回复框且不改原文', async ({ page }) => {
    await openBotSession(page)

    const panel = page.locator('.kb-search')
    const searchInput = page.getByPlaceholder('检索知识库（商品 / 政策 / FAQ）')
    await searchInput.fill('奶油小熊')

    // 防抖 300ms 后自动检索；检索耗时是 DoD 可见指标
    await expect(panel.locator('.kb-search__latency')).toBeVisible({ timeout: 10_000 })
    await expect(panel.locator('.hit').first()).toBeVisible()
    await expect(panel.locator('.kb-search__latency')).toContainText('命中')

    // 每条命中都要有可信度分数与溯源依据（没有依据的知识不该被展示）
    const firstHit = panel.locator('.hit').first()
    await expect(firstHit.locator('.hit__score')).toHaveText(/^\d\.\d{2}$/)
    await expect(firstHit.locator('.hit__authority')).toContainText('依据：')

    const snippet = (await firstHit.locator('.hit__snippet').textContent())?.trim() ?? ''
    expect(snippet.length).toBeGreaterThan(0)

    // 一键填入：回复框拿到内容，且内容未经改写（去掉展示用的省略号后比对）
    await firstHit.getByRole('button', { name: '填入回复框' }).click()
    const textarea = page.locator('.composer textarea')
    const draft = await textarea.inputValue()
    expect(draft.length).toBeGreaterThan(0)
    const core = snippet.replace(/^…|…$/g, '')
    expect(draft.includes(core.slice(0, 12)) || core.includes(draft.slice(0, 12))).toBe(true)

    await expectNoAnchor(page)
  })

  test('④ 坐席发送消息进入消息流，且不产生任何外链', async ({ page }) => {
    await openBotSession(page)

    // 先接管，才允许以坐席身份发送
    await page.getByRole('button', { name: '接管会话' }).click()
    await page.getByRole('dialog').getByRole('button', { name: '确认接管' }).click()
    await expect(page.getByRole('button', { name: '释放回机器人' })).toBeVisible()

    const before = await messageCount(page).count()
    const text = '您好，这款奶油小熊 20cm 现货，今天下单 48 小时内发出。'

    const textarea = page.locator('.composer textarea')
    await textarea.fill(text)
    await page.locator('.composer__send').click()

    // 发送后消息条数增加，且内容原样出现在消息流里
    await expect(messageCount(page)).toHaveCount(before + 1, { timeout: 10_000 })
    await expect(page.locator('.stream')).toContainText(text)

    // HC-1：话术里不得出现可点击链接
    await expectNoAnchor(page)
    await expect(page.locator('.stream a')).toHaveCount(0)
  })

  test('⑤ 释放回机器人，归属与回复模式复原', async ({ page }) => {
    await openBotSession(page)

    // 先把会话摆到「我接管」的状态
    const takeoverBtn = page.getByRole('button', { name: '接管会话' })
    if (await takeoverBtn.count()) {
      await takeoverBtn.click()
      await page.getByRole('dialog').getByRole('button', { name: '确认接管' }).click()
      await expect(page.getByRole('button', { name: '释放回机器人' })).toBeVisible()
    }

    await page.getByRole('button', { name: '释放回机器人' }).click()
    const dialog = page.getByRole('dialog', { name: '确认释放回机器人？' })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '确认释放' }).click()

    expect(await readToast(page)).toContain('已释放回机器人')
    await expect(page.getByRole('button', { name: '接管会话' })).toBeVisible()
    await expect(page.locator('.owner-badge')).toContainText('机器人')
    await expect(page.locator('.composer__mode')).toContainText('机器人接待中')
  })
})
