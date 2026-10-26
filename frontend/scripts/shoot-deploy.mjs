import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = path.resolve('shots/deploy');
fs.mkdirSync(OUT, { recursive: true });

const shots = [];
async function shot(page, name, note) {
  await page.waitForTimeout(900);
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  shots.push({ name, note, file });
  console.log('  saved', name);
}

// 系统已装 Chrome，直接复用（不下载 playwright 自带浏览器）
const browser = await chromium.launch({ channel: 'chrome' });
const ctx = await browser.newContext({
  viewport: { width: 1600, height: 950 },
  deviceScaleFactor: 1,
  locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai',
});
const page = await ctx.newPage();
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

// 1. 登录页
await page.goto('http://127.0.0.1:8080/', { waitUntil: 'networkidle' });
await shot(page, '01-login', '登录页');

// 2. 用管理员登录（陈静 / ADMIN，能看到最多菜单）
await page.fill('input[type="text"], input[placeholder*="账号"], input#username', 'chenjing');
await page.fill('input[type="password"], input#password', 'Wk@2026demo');
await page.click('button[type="submit"], button:has-text("登录")');
await page.waitForTimeout(2500);
await shot(page, '02-after-login', '登录后落地页');

// 3. 会话工作台
await page.goto('http://127.0.0.1:8080/#/console/sessions', { waitUntil: 'networkidle' });
await shot(page, '03-sessions', '会话工作台');

// 4. 知识库
await page.goto('http://127.0.0.1:8080/#/admin/kb', { waitUntil: 'networkidle' });
await shot(page, '04-kb', '知识库管理');

// 5. 指标
await page.goto('http://127.0.0.1:8080/#/admin/metrics', { waitUntil: 'networkidle' });
await shot(page, '05-metrics', '指标看板');

// 6. 配置 - 转人工规则
await page.goto('http://127.0.0.1:8080/#/admin/channels', { waitUntil: 'networkidle' });
await shot(page, '06-config', '配置页');

// 7. 工单
await page.goto('http://127.0.0.1:8080/#/console/tickets', { waitUntil: 'networkidle' });
await shot(page, '07-tickets', '工单列表');

console.log('\n=== console errors ===');
console.log(errors.length ? errors.slice(0, 12).join('\n') : '(none)');
fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify({ shots, errors }, null, 2));
await browser.close();
console.log('\nDONE', shots.length);
