<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ROLE_LABELS } from '@/permission/matrix'

/**
 * 无权限页 —— 4.5.4 权限矩阵拦截后的落地页。
 *
 * 为什么单独做一页而不是跳 404：
 *   「页面不存在」与「你无权看这个页面」是两种完全不同的信息。
 *   坐席被弹回 404 会以为系统坏了并报障；明确告知无权限，
 *   他才知道应该找主管而不是找技术支持。
 *
 * 注意：前端守卫只是体验层，真正的鉴权在 BFF（返回 10003）。
 * 因此这里也说明「权限由后端最终裁定」，避免用户以为改前端就能绕过。
 */
const router = useRouter()
const auth = useAuthStore()

const roleLabel = computed(() => ROLE_LABELS[auth.role])

function goHome(): void {
  const target = auth.role === 'KB_OPERATOR' ? '/admin/kb' : '/console/sessions'
  void router.replace(target)
}
</script>

<template>
  <div class="forbidden">
    <span class="forbidden__icon">🔒</span>
    <h1 class="forbidden__title">无权限访问</h1>
    <p class="forbidden__desc">
      当前身份「{{ auth.displayName }}（{{ roleLabel }}）」没有访问该页面的权限。
    </p>
    <p class="forbidden__hint">
      若确实需要该权限，请联系客服主管或系统管理员调整角色。权限最终由服务端裁定。
    </p>
    <div class="forbidden__actions">
      <el-button type="primary" @click="goHome">返回可访问页面</el-button>
      <el-button text @click="router.back()">返回上一页</el-button>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.forbidden {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: $space-3;
  padding: $space-8;
  text-align: center;
  background: $ink-50;

  &__icon {
    font-size: 52px;
    line-height: 1;
  }

  &__title {
    margin: 0;
    font-size: $text-xl;
    font-weight: 600;
    color: $ink-900;
  }

  &__desc {
    margin: 0;
    font-size: $text-base;
    color: $ink-700;
  }

  &__hint {
    margin: 0;
    max-width: 520px;
    font-size: $text-xs;
    line-height: 1.7;
    color: $ink-500;
  }

  &__actions {
    display: flex;
    gap: $space-2;
    margin-top: $space-2;
  }
}
</style>
