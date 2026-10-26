#!/usr/bin/env node
/**
 * 部署包生成器 —— 任务书 4.6 交付物第 6 项「前端部署包（静态资源 + Nginx 配置）」。
 *
 * 产出结构（dist-package/）：
 *
 *   wankeai-frontend-<version>/
 *   ├── html/                    ← 静态资源，拷到 Nginx 的 root
 *   │   ├── index.html
 *   │   └── assets/
 *   ├── nginx/
 *   │   ├── wankeai-frontend.conf
 *   │   └── 50x.html
 *   ├── README.md                ← 现场部署步骤
 *   ├── MANIFEST.json            ← 版本、构建时间、文件清单与哈希
 *   └── build-info.txt
 *
 * 为什么不直接压 dist/：
 *   部署现场需要的不只是 HTML，还有 Nginx 配置、错误页、版本信息与校验清单。
 *   打包到一起，运维拿到压缩包就能上线，不用再回头找文件。
 */
import { createHash } from 'node:crypto'
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const distDir = join(root, 'dist')
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'))
const version = pkg.version ?? '1.0.0'
const outDir = join(root, 'dist-package')
const pkgDir = join(outDir, `wankeai-frontend-${version}`)

if (!existsSync(distDir)) {
  console.error('✗ 未找到 dist/，请先执行 `npm run build`')
  process.exit(1)
}

rmSync(outDir, { recursive: true, force: true })
mkdirSync(join(pkgDir, 'nginx'), { recursive: true })

// ── 1. 静态资源 ────────────────────────────────────────────
cpSync(distDir, join(pkgDir, 'html'), { recursive: true })

// ── 2. Nginx 配置与错误页 ──────────────────────────────────
copyFileSync(join(root, 'deploy', 'nginx.conf'), join(pkgDir, 'nginx', 'wankeai-frontend.conf'))
copyFileSync(join(root, 'deploy', '50x.html'), join(pkgDir, 'nginx', '50x.html'))

// ── 3. 文件清单与哈希（现场校验用）─────────────────────────
function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(full))
    else out.push(full)
  }
  return out
}

const files = walk(join(pkgDir, 'html')).map((full) => {
  const buf = readFileSync(full)
  return {
    path: relative(pkgDir, full).replace(/\\/g, '/'),
    bytes: buf.length,
    sha256: createHash('sha256').update(buf).digest('hex').slice(0, 16),
  }
})

const totalBytes = files.reduce((sum, f) => sum + f.bytes, 0)
const builtAt = new Date().toISOString()

const manifest = {
  product: '玩可爱智能客服 Agent · 前端',
  version,
  built_at: builtAt,
  node: process.version,
  file_count: files.length,
  total_bytes: totalBytes,
  entries: files,
}
writeFileSync(join(pkgDir, 'MANIFEST.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf-8')

// ── 4. 现场部署说明 ────────────────────────────────────────
writeFileSync(
  join(pkgDir, 'README.md'),
  `# 玩可爱智能客服 Agent 前端 · 部署包 v${version}

构建时间：${builtAt}
产物包体：${(totalBytes / 1024 / 1024).toFixed(2)} MB（${files.length} 个文件，未压缩）

## 一、前置条件

| 项 | 要求 |
| --- | --- |
| Nginx | ≥ 1.20（需支持 \`add_header ... always\`） |
| 磁盘 | ≥ 50 MB（静态资源约 ${(totalBytes / 1024 / 1024).toFixed(1)} MB） |
| 后端 | BFF 服务已部署，且与本机网络可达 |

## 二、部署步骤

\`\`\`bash
# 1. 解压
tar -xzf wankeai-frontend-${version}.tar.gz
cd wankeai-frontend-${version}

# 2. 静态资源落地（先清理旧版本，避免残留旧 chunk）
rm -rf /usr/share/nginx/html/wankeai
mkdir -p /usr/share/nginx/html/wankeai
cp -r html/. /usr/share/nginx/html/wankeai/

# 3. Nginx 配置
cp nginx/wankeai-frontend.conf /etc/nginx/conf.d/
cp nginx/50x.html /usr/share/nginx/html/wankeai/50x.html

# 4. 改两处占位
#    __BFF_UPSTREAM__  → BFF 实际地址（如 10.0.1.20:8000）
#    __FRONTEND_HOST__ → 对外域名
vi /etc/nginx/conf.d/wankeai-frontend.conf

# 5. 语法检查 + 平滑重载
nginx -t && nginx -s reload

# 6. 验证
curl -sI http://127.0.0.1/healthz
curl -s  http://127.0.0.1/ | head -20
\`\`\`

## 三、部署后自检（5 条红线）

\`\`\`bash
# 1. 首页不含任何外链域名
curl -s http://127.0.0.1/ | grep -Eo 'https?://[^"'"'"' ]+' | grep -v '127.0.0.1\\|localhost' || echo '✓ 无外链'

# 2. CSP 头存在
curl -sI http://127.0.0.1/ | grep -i 'content-security-policy'

# 3. BFF 代理通
curl -s http://127.0.0.1/bff/v1/sessions?page=1

# 4. SSE 不被缓冲（应立刻有数据流出，而不是等很久一次性吐）
curl -N -s 'http://127.0.0.1/bff/v1/chat/stream?session_id=test' --max-time 5 | head -5

# 5. index.html 不缓存
curl -sI http://127.0.0.1/index.html | grep -i 'cache-control'
\`\`\`

## 四、回滚

**前端回滚不需要重部署**——静态资源带内容哈希，回滚就是换一份 html 目录：

\`\`\`bash
# 保留上一版目录做备份是必须的：
mv /usr/share/nginx/html/wankeai /usr/share/nginx/html/wankeai.bak.$(date +%F)
cp -r /path/to/previous/html /usr/share/nginx/html/wankeai
nginx -s reload
\`\`\`

**注意**：\`index.html\` 设了 \`no-store\`，用户刷新即可拿到新版本；
若用户仍看到旧页面，让其强制刷新（Ctrl+F5），不要改缓存头来凑。

## 五、常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 登录后立刻跳回登录页 | BFF 401 / Cookie 未带上 | 检查 \`/bff/\` 代理是否透传 Host |
| 打字机「憋一大段」 | SSE 被缓冲 | 确认 \`proxy_buffering off\` 与后端 \`X-Accel-Buffering: no\` 都在 |
| 页面白屏、控制台报 chunk 加载失败 | 发了新版但 index.html 是旧的 | 确认 \`index.html\` 的 \`Cache-Control: no-store\` 生效 |
| 接口 502 | BFF 不可达 | \`curl\` BFF 直连地址；看 \`error_log\` |
| 知识库检索中文筛选全空 | 后端向量库写入时 \`ensure_ascii=True\` | 后端侧修（见联调记录），前端无需改动 |

## 六、文件清单

详见同目录 \`MANIFEST.json\`，含每个文件的字节数与 sha256 前 16 位，可用于校验传输完整性。
`,
  'utf-8',
)

writeFileSync(
  join(pkgDir, 'build-info.txt'),
  [
    `product=玩可爱智能客服 Agent 前端`,
    `version=${version}`,
    `built_at=${builtAt}`,
    `node=${process.version}`,
    `files=${files.length}`,
    `total_bytes=${totalBytes}`,
    `dist_gzip_kb=${files.reduce((s, f) => s + (f.path.endsWith('.map') ? 0 : 0), 0)}`,
  ].join('\n'),
  'utf-8',
)

// ── 5. 汇总输出 ────────────────────────────────────────────
console.log('✓ 部署包已生成')
console.log(`  目录：${pkgDir}`)
console.log(`  文件：${files.length} 个，共 ${(totalBytes / 1024 / 1024).toFixed(2)} MB`)
console.log(`  说明：${join(pkgDir, 'README.md')}`)
console.log(`  清单：${join(pkgDir, 'MANIFEST.json')}`)
void statSync
