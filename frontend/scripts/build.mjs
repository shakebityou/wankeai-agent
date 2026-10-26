#!/usr/bin/env node
/**
 * 生产构建入口。
 *
 * 为什么需要这层包装而不是直接 `vite build`：
 *   esbuild 会把中间产物写到系统临时目录（Windows 上是 %TEMP%），
 *   而本项目在受限沙箱/CI 里运行时，系统临时目录可能不可写或被安全策略拒绝
 *   （表现为 `[vite:esbuild-transpile] remove ...: Access is denied`）。
 *   把 TMPDIR / TEMP / TMP 指到项目内的 `.esbuild-tmp`，
 *   构建产物和临时文件就都落在工作目录里，不依赖系统盘权限。
 *
 * 顺带做一件事：构建完成后打印 gzip 体积汇总，
 * 方便对照任务书 4.5 的性能基线（首屏 P95 ≤2s）。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const tmpDir = join(root, '.esbuild-tmp')

mkdirSync(tmpDir, { recursive: true })

const child = spawn(
  process.platform === 'win32' ? 'npx.cmd' : 'npx',
  ['vite', 'build', ...process.argv.slice(2)],
  {
    cwd: root,
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: {
      ...process.env,
      TMPDIR: tmpDir,
      TEMP: tmpDir,
      TMP: tmpDir,
    },
  },
)

child.on('exit', (code) => {
  // 清理临时目录，避免残留污染下次构建
  try {
    rmSync(tmpDir, { recursive: true, force: true })
  } catch {
    /* 清理失败不影响构建结果 */
  }

  if (code !== 0) {
    process.exit(code ?? 1)
  }

  reportBundle()
})

/** 输出构建产物体积汇总（含 gzip），供部署与性能评审参考 */
function reportBundle() {
  const distDir = join(root, 'dist')
  let assetsDir
  try {
    assetsDir = join(distDir, 'assets')
    statSync(assetsDir)
  } catch {
    console.log('\n[build] 未找到 dist/assets，跳过体积汇总')
    return
  }

  const rows = []
  let rawTotal = 0
  let gzipTotal = 0

  for (const name of readdirSync(assetsDir)) {
    const full = join(assetsDir, name)
    const size = statSync(full).size
    const gz = gzipSync(readFileSync(full)).length
    rawTotal += size
    gzipTotal += gz
    rows.push({ name, size, gz })
  }

  rows.sort((a, b) => b.gz - a.gz)

  const kb = (n) => `${(n / 1024).toFixed(1)} kB`
  console.log('\n[build] 产物体积（按 gzip 倒序，前 10）')
  for (const row of rows.slice(0, 10)) {
    console.log(`  ${row.name.padEnd(44)} ${kb(row.size).padStart(11)}  → gzip ${kb(row.gz).padStart(10)}`)
  }
  console.log(`  ${'合计'.padEnd(42)} ${kb(rawTotal).padStart(11)}  → gzip ${kb(gzipTotal).padStart(10)}`)
  console.log(`\n[build] 产物目录：${distDir}`)
}
