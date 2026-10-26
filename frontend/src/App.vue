<script setup lang="ts">
import { RouterView } from 'vue-router'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

/**
 * 应用根组件。
 * 只负责挂载路由出口与全局 Element Plus 中文语言包，业务布局在各 layout 内。
 *
 * 为什么**不能**设 `namespace="wk"`：
 *   Element Plus 的样式表（element-plus/dist/index.css）全部以 `.el-` 开头，
 *   而 namespace 只改变组件渲染出来的 class，不会同步改样式表。
 *   一旦改成 `wk`，所有组件失去全部样式 —— 图标 SVG 撑满父容器（`width/height: 1em`
 *   的规则挂在 `.el-icon svg` 上）、下拉框塌成一条、表格没有边框。
 *   dev 下看到的就是「左边一列巨大的 ⌄ 和时钟图标」。
 *   本项目需要定制 Element 外观时，统一走 src/styles/index.scss 里的
 *   CSS 变量覆写（--el-color-primary 等），不动 namespace。
 */
</script>

<template>
  <ElConfigProvider :locale="zhCn">
    <RouterView />
  </ElConfigProvider>
</template>

<style lang="scss">
#app {
  height: 100%;
}
</style>
