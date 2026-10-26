import { chromium } from "playwright";
const b = await chromium.launch({ channel: "chrome" });
const p = await b.newPage({ viewport: { width: 1600, height: 950 } });
await p.goto("http://127.0.0.1:8080/", { waitUntil: "networkidle" });
await p.fill('input[type="text"], input#username', "chenjing");
await p.fill('input[type="password"], input#password', "Wk@2026demo");
await p.click('button[type="submit"], button:has-text("登录")');
await p.waitForTimeout(2500);
for (const u of ["/admin/kb", "/admin/metrics", "/admin/config", "/console/tickets", "/console/sessions"]) {
  await p.goto("http://127.0.0.1:8080" + u, { waitUntil: "networkidle" });
  await p.waitForTimeout(1200);
  console.log(u, "->", p.url(), "|", (await p.title()));
}
await b.close();
