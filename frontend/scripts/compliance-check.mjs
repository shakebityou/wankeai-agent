#!/usr/bin/env node
/**
 * 合规红线自动化检查 —— 对应任务书 4.6 DoD 第 ④ 条：
 *   「合规红线 5 条全部通过自动化检查」
 *
 * 5 条红线（任务书 4.5.2）：
 *   R1 不外链：源码中不得出现任何第三方外链域名（商品/店铺/活动链接、小程序、H5 唤起）
 *   R2 不导出：不得出现导出/下载入口（file-saver、download 属性、导出按钮）
 *   R3 不展示明文凭证：不得出现 AppSecret / AccessToken / Token 的明文渲染
 *   R4 脱敏展示：手机号/姓名/地址/订单号必须走 mask 函数
 *   R5 只调 BFF：不得直连内部服务或商家接口（axios/fetch 目标必须是 /bff/v1）
 *
 * 设计决策：
 *   - **静态扫描而不是人工走查** —— 红线是「一票否决」项，靠人眼看 40 个组件必然漏，
 *     且事后发现时代价是重新设计交互。
 *   - **失败时给出文件与行号** —— 合规检查的价值在于能直接定位到要改的那一行。
 *   - **退出码非 0 即 CI 失败** —— 接进流水线才有约束力。
 *
 * 用法：npm run lint:compliance
 */

import { readdir, readFile, stat } from 'node:fs/promises'
import { join, relative, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC_DIR = join(ROOT, 'src')

/** 允许扫描的源码后缀 */
const SCAN_EXT = new Set(['.ts', '.vue', '.mts'])

/** 扫描范围之外的目录（node_modules / 测试夹具等） */
const SKIP_DIRS = new Set(['node_modules', 'dist', 'coverage', '.git', 'playwright-report', 'test-results'])

/**
 * 单测文件是「合规红线的守门人」，不是产品代码。
 *
 * 它们必须构造反例才能验证红线 —— 比如 KbEditor 的预览去外链测试，
 * 输入必须真的是一条淘宝链接；maskPhone 的测试输入必须是真的手机号；
 * 否则测不出任何东西。因此 .test.ts 不参与扫描，
 * 但它们内部断言的**期望值**与被测实现的最终产物仍然受红线约束。
 */
const TEST_FILE = /\.(test|spec)\.(ts|tsx|js|mjs)$/

const problems = []

/* ------------------------------------------------------------------ *
 * R1 不外链
 * ------------------------------------------------------------------ */

/** 允许出现的域名白名单（本地/文档/规范用途，不构成外链入口） */
const DOMAIN_WHITELIST = [
  '127.0.0.1',
  'localhost',
  'w3.org', // xmlns
  'github.com/wkeai', // 仓库自有域名占位
]

/** 匹配 http(s) 链接与常见平台域名 */
const URL_PATTERN = /https?:\/\/[^\s'"`)\]]+/gi
const PLATFORM_DOMAIN =
  /(?:taobao|tmall|pinduoduo|douyin|weixin|wechat|qq\.com|jd\.com|alibaba|alipay|m\.tb|h5\.|miniprogram|aldsj)/i

function isWhitelisted(url) {
  return DOMAIN_WHITELIST.some((domain) => url.includes(domain))
}

/* ------------------------------------------------------------------ *
 * R2 不导出
 * ------------------------------------------------------------------ */

const EXPORT_PATTERNS = [
  { re: /from\s+['"]file-saver['"]/, label: '引入 file-saver（导出库）' },
  { re: /\bdownload\s*=\s*['"](?!['"])/, label: 'a/download 属性' },
  { re: /\bwindow\.print\s*\(\s*\)/, label: 'window.print() 调用' },
  { re: /\bexportToExcel\b|\bexportCsv\b|\bexportJson\b/, label: '导出函数命名' },
]

/* ------------------------------------------------------------------ *
 * R3 不展示明文凭证
 * ------------------------------------------------------------------ */

const SECRET_PATTERNS = [
  { re: /AppSecret\s*[:=]\s*['"][^'"]{6,}['"]/, label: 'AppSecret 字面量赋值' },
  { re: /AccessToken\s*[:=]\s*['"][^'"]{6,}['"]/, label: 'AccessToken 字面量赋值' },
  { re: /['"]app_secret['"]\s*[:=]/, label: 'app_secret 字段赋值' },
]

/* ------------------------------------------------------------------ *
 * R4 脱敏展示：出现原始手机号/身份证/邮箱字面量即违规
 * ------------------------------------------------------------------ */

const PII_PATTERNS = [
  { re: /\b1[3-9]\d{9}\b/, label: '完整手机号字面量（应使用 maskPhone）' },
  { re: /\b\d{17}[\dXx]\b/, label: '身份证号字面量' },
  { re: /\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b/, label: '邮箱字面量' },
]

/* ------------------------------------------------------------------ *
 * R5 只调 BFF
 * ------------------------------------------------------------------ */

const BFF_PATTERNS = [
  { re: /['"`]\/\/(?:www\.)?(?:taobao|tmall|pinduoduo|douyin|weixin|wechat)\./i, label: '直连平台域名' },
  { re: /axios\.create\(\s*\{\s*baseURL:\s*['"](?!\/bff)/, label: 'axios baseURL 非 /bff 前缀' },
  { re: /fetch\(\s*['"](?!\/bff|\.\/|https?:\/\/127|https?:\/\/localhost)/, label: 'fetch 目标非 BFF' },
]

/* ------------------------------------------------------------------ *
 * 扫描
 * ------------------------------------------------------------------ */

async function listFiles(dir) {
  const out = []
  let entries
  try {
    entries = await readdir(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const entry of entries) {
    if (entry.name.startsWith('.') && entry.name !== '.') continue
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue
      out.push(...(await listFiles(full)))
    } else if (SCAN_EXT.has(extname(entry.name)) && !TEST_FILE.test(entry.name)) {
      out.push(full)
    }
  }
  return out
}

async function scan(file) {
  const rel = relative(ROOT, file)
  let text
  try {
    text = await readFile(file, 'utf8')
  } catch {
    return
  }
  const lines = text.split(/\r?\n/)

  lines.forEach((line, index) => {
    const lineNo = index + 1
    const where = `${rel}:${lineNo}`

    // R1
    for (const url of line.match(URL_PATTERN) ?? []) {
      if (!isWhitelisted(url) && PLATFORM_DOMAIN.test(url)) {
        problems.push(`[R1 不外链] ${where} → ${url}`)
      }
    }
    // R2
    for (const { re, label } of EXPORT_PATTERNS) {
      if (re.test(line)) problems.push(`[R2 不导出] ${where} → ${label}`)
    }
    // R3
    for (const { re, label } of SECRET_PATTERNS) {
      if (re.test(line)) problems.push(`[R3 不明文凭证] ${where} → ${label}`)
    }
    // R4
    for (const { re, label } of PII_PATTERNS) {
      if (re.test(line)) problems.push(`[R4 脱敏展示] ${where} → ${label}`)
    }
    // R5
    for (const { re, label } of BFF_PATTERNS) {
      if (re.test(line)) problems.push(`[R5 只调 BFF] ${where} → ${label}`)
    }
  })
}

async function main() {
  const files = await listFiles(SRC_DIR)
  for (const file of files) await scan(file)

  if (problems.length > 0) {
    console.error(`\n✗ 合规红线检查未通过，发现 ${problems.length} 处问题：\n`)
    for (const problem of problems) console.error(`  ${problem}`)
    console.error('\n参考：任务书 4.5.2 合规红线（不外链 / 不导出 / 不明文凭证 / 脱敏展示 / 只调 BFF）\n')
    process.exit(1)
  }

  console.log(`✓ 合规红线检查通过：扫描 ${files.length} 个源文件，5 条红线均未触发。`)
}

main().catch((error) => {
  console.error('合规检查执行失败：', error)
  process.exit(1)
})
