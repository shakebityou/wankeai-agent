import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { BFF_CODE, type BffError } from '@/types/bff'
import { registerBffErrorHooks } from './client'
import { useAuthStore } from '@/stores/auth'
import { router } from '@/router'

/**
 * 把 BFF 错误码接到 UI 与路由上 —— 任务书 4.5.1 的 6 个错误码统一处理。
 *
 * 为什么单独一个文件：
 *   api/client.ts 不能 import router / ElMessage（会造成循环依赖与可测试性下降），
 *   所以客户端只暴露 registerBffErrorHooks，由入口在挂载前注入具体行为。
 */

/** 10002 跳登录的节流：多个并发请求同时 401 时只跳一次 */
let redirecting = false

export function installBffErrorHandler(): void {
  registerBffErrorHooks({
    onUnauthorized(error) {
      const auth = useAuthStore()

      // 「登录态过期」只对**真的登录过、然后失效了**的人说才有意义。
      // 首屏在没有 Cookie 时问 /auth/me 必然 401 —— 那不是过期，是从没登录过，
      // 提示「登录态已过期」会让第一次打开系统的人莫名其妙。
      //
      // 判断依据不能用 `router.currentRoute.name === 'login'`：
      // 页面刚 reload 时路由还没解析完，name 往往是 undefined，
      // 这条路走过一次，提示照样会冒出来。改用「本次会话有没有登录过」。
      const hadSession = auth.isAuthenticated
      auth.logout()
      if (!hadSession) return

      if (redirecting) return
      redirecting = true
      ElMessage.warning('登录态已过期，请重新登录')
      const current = router.currentRoute.value
      void router
        .replace({ name: 'login', query: { redirect: current.fullPath || '/' } })
        .finally(() => {
          window.setTimeout(() => {
            redirecting = false
          }, 1500)
        })
      void error
    },

    onForbidden() {
      ElMessage.error('无权限查看')
    },

    onConflict(error) {
      // 4.5.1：10005 -> 提示「该会话已被 XXX 接管」并刷新
      ElMessage.warning(error.message || '该会话已被其他坐席接管，正在刷新')
      window.dispatchEvent(new CustomEvent('wankeai:conflict-refresh'))
    },

    onRateLimited(_error, retryAfterSec) {
      // 4.5.1：20001 -> 按钮禁用 + 倒计时（倒计时状态由 composable 管理，
      // 这里通过全局事件广播，具体按钮订阅后自行禁用）
      ElMessage.warning(`操作过于频繁，请 ${retryAfterSec} 秒后再试`)
      window.dispatchEvent(
        new CustomEvent('wankeai:rate-limited', { detail: { seconds: retryAfterSec } }),
      )
    },

    onDownstreamUnavailable(error) {
      // 4.5.1：30001 -> 降级提示并禁用相关操作
      ElNotification({
        title: '下游服务暂不可用',
        message: error.message || '商家接口或大模型服务暂时不可用，相关操作已禁用，请稍后重试',
        type: 'warning',
        duration: 6000,
      })
      window.dispatchEvent(new CustomEvent('wankeai:downstream-down'))
    },

    onParamInvalid(error) {
      ElMessage.error(error.message || '提交的参数有误，请检查标红字段')
    },

    onUnknown(error) {
      // 静默请求（轮询、埋点）的失败不打扰坐席
      if (error.code === BFF_CODE.SERVER_ERROR && error.httpStatus === 0) {
        ElMessage.error(error.message)
        return
      }
      ElMessage.error(error.message || '服务异常，请稍后重试')
    },
  })
}

/** 供页面在捕获到 10005 时弹「已被 XXX 接管」确认框（需要展示坐席名） */
export async function confirmConflictRefresh(message: string): Promise<boolean> {
  try {
    await ElMessageBox.alert(message, '会话归属已变更', {
      confirmButtonText: '刷新会话',
      type: 'warning',
    })
    return true
  } catch {
    return false
  }
}

/** 类型收窄辅助：业务代码里判断错误对象 */
export function isBffError(error: unknown): error is BffError {
  return error instanceof Error && error.name === 'BffError' && 'code' in error
}
