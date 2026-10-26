import { fileURLToPath, URL } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { bffMockPlugin } from './mock/vite-plugin-bff-mock'

/**
 * 开发期的 CSP（仅 dev server，生产走 Nginx 响应头）。
 *
 * 为什么需要它：Vue 的 SFC 运行时编译器要用 `new Function` 编译模板，
 * 而 `script-src 'self'` 会连 `unsafe-eval` 一起封掉 —— 表现为控制台报
 * `EvalError: Refused to evaluate a string as JavaScript`，
 * 然后**所有 Element Plus 组件失去样式与尺寸**（图标撑满整屏、布局塌掉）。
 * 生产构建已预编译模板，不需要 'unsafe-eval'，所以只在 dev 放宽。
 */
function devCspPlugin(): Plugin {
  const DEV_CSP = [
    "default-src 'self'",
    // 'unsafe-eval' 仅供 Vue 运行时模板编译，dev-only
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self' ws: wss:",
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ')

  return {
    name: 'wankeai-dev-csp',
    apply: 'serve',
    transformIndexHtml(html) {
      return html.replace(
        /<meta\s+http-equiv="Content-Security-Policy"[\s\S]*?\/>/,
        `<meta http-equiv="Content-Security-Policy" content="${DEV_CSP}" />`,
      )
    },
  }
}

/**
 * 玩可爱智能客服 Agent 前端构建配置。
 *
 * 硬约束：
 * - 前端只调 BFF（/bff/v1/*），不直连内部服务、不直连商家接口；
 * - 不引入任何 CDN / 外链资源（HC-1），全部依赖本地打包；
 * - 开发期由本地 Mock 中间件提供 BFF，联调期切换 VITE_BFF_TARGET 指向真实 BFF。
 */
export default defineConfig(({ mode }) => {
  const bffTarget = process.env.VITE_BFF_TARGET ?? ''

  return {
    plugins: [vue(), devCspPlugin(), bffMockPlugin()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    css: {
      preprocessorOptions: {
        scss: {
          api: 'modern-compiler',
        },
      },
    },
    server: {
      host: '127.0.0.1',
      port: 5273,
      strictPort: false,
      proxy: bffTarget
        ? {
            '/bff': {
              target: bffTarget,
              changeOrigin: true,
            },
          }
        : undefined,
    },
    preview: {
      host: '127.0.0.1',
      port: 4173,
    },
    build: {
      target: 'es2021',
      outDir: 'dist',
      assetsDir: 'assets',
      sourcemap: mode !== 'production',
      chunkSizeWarningLimit: 1500,
      rollupOptions: {
        output: {
          manualChunks: {
            vue: ['vue', 'vue-router', 'pinia'],
            element: ['element-plus', '@element-plus/icons-vue'],
            charts: ['echarts'],
          },
        },
      },
    },
    define: {
      __APP_VERSION__: JSON.stringify(process.env.npm_package_version ?? '1.0.0'),
    },
  }
})
