import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = path.resolve('shots/deploy');
fs.mkdirSync(OUT, { recursive: true });
const report = [];
const browser = await chromium.launch({ channel: 'chrome' });
const ctx = await browser.newContext({
  viewport: { width: 1600, height: 950 },
  locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
});
const page = await ctx.newPage();

async function grab(name, note) {
  await page.waitForTimeout(1100);
  const txt = (await page.locator('body').innerText()).replace(/\n{3,}/g, '\n\n').trim();
  const rows = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('table tr').forEach((tr) => {
      const cells = [...tr.querySelectorAll('th,td')].map((c) => c.innerText.trim().replace(/\s+/g, ' '));
      if (cells.some(Boolean)) out.push(cells);
    });
    return out.slice(0, 30);
  });
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
  report.push({ name, note, url: page.url(), text: txt.slice(0, 3000), rows });
  console.log(`\n########## ${name} — ${note} ##########`);
  console.log(txt.slice(0, 1300));
  if (rows.length) { console.log('--- TABLE ---'); rows.slice(0, 14).forEach((r) => console.log('  | ' + r.join(' | '))); }
}

const B = 'http://127.0.0.1:8080/#';

await page.goto(B + '/login', { waitUntil: 'networkidle' });
await grab('01-login', '登录页');

await page.fill('input[type="text"], input#username', 'chenjing');
await page.fill('input[type="password"], input#password', 'Wk@2026demo');
await page.click('button[type="submit"], button:has-text("登录")');
await page.waitForTimeout(2800);

await page.goto(B + '/console/sessions', { waitUntil: 'networkidle' });
await grab('02-sessions', '会话工作台');

// 点第一个会话，看消息 + AI 上下文
try {
  const first = page.locator('.session-item, .el-scrollbar .item, li').filter({ hasText: '淘宝' }).first();
  await first.click({ timeout: 5000 });
  await page.waitForTimeout(2200);
  await grab('03-session-detail', '会话详情（消息 + AI 上下文 + 检索）');
} catch (e) { console.log('!! 点会话失败：', e.message.split('\n')[0]); }

await page.goto(B + '/console/tickets', { waitUntil: 'networkidle' });
await grab('04-tickets', '工单列表');

await page.goto(B + '/admin/kb', { waitUntil: 'networkidle' });
await grab('05-kb', '知识库管理');

await page.goto(B + '/admin/metrics', { waitUntil: 'networkidle' });
await grab('06-metrics', '指标看板');

await page.goto(B + '/admin/handoff-rules', { waitUntil: 'networkidle' });
await grab('07-handoff-rules', '转人工规则');

await page.goto(B + '/admin/channels', { waitUntil: 'networkidle' });
await grab('08-channels', '渠道授权');

await page.goto(B + '/admin/users', { waitUntil: 'networkidle' });
await grab('09-users', '账号管理');

fs.writeFileSync(path.join(OUT, 'render-report.json'), JSON.stringify(report, null, 2), 'utf-8');
await browser.close();
console.log('\nDONE');
