import { config } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

/**
 * Vitest 全局 setup。
 *
 * 只做两件事：
 *   1. 注册 Element Plus —— 组件里用到 el-skeleton / el-button / el-drawer 等，
 *      不注册的话挂载时报 "Failed to resolve component"。
 *   2. 挂一个空的 ResizeObserver —— ECharts 的 BaseChart 依赖它，
 *      而 jsdom 没有实现（不补这个任何带图表的组件测试都会挂）。
 */

config.global.plugins = [ElementPlus as never]

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver

/**
 * jsdom 同样没有实现 Element.prototype.scrollTo，而消息流首屏 / 回到底部都要调它。
 * 这里补一个空实现，让组件可以在测试里跑完挂载。
 */
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Element.prototype.scrollTo = function scrollTo(): void {
    /* noop：jsdom 无布局，滚动由测试用 stub 几何模拟 */
  }
}
