<script setup lang="ts">
import { useRouter } from 'vue-router'

/**
 * 404 —— 路由兜底页。
 */
const router = useRouter()
</script>

<template>
  <div class="fallback">
    <span class="fallback__code">404</span>
    <h1 class="fallback__title">页面不存在</h1>
    <p class="fallback__desc">地址可能已失效，或者您手动输入了错误的路径。</p>
    <el-button type="primary" @click="router.replace('/console/sessions')">返回工作台</el-button>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.fallback {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: $space-3;
  background: $ink-50;

  &__code {
    font-size: 64px;
    font-weight: 800;
    line-height: 1;
    color: $ink-200;
  }

  &__title {
    margin: 0;
    font-size: $text-xl;
    font-weight: 600;
    color: $ink-800;
  }

  &__desc {
    margin: 0;
    font-size: $text-sm;
    color: $ink-500;
  }
}
</style>
