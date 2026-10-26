<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ROLE_LABELS } from '@/permission/matrix'
import { useAuthStore } from '@/stores/auth'

/**
 * 坐席工作台布局（P1–P5 容器）。
 *
 * 设计取向：坐席台是「三栏并行」的高强度工作界面 ——
 * 左导航（系统切换）、中列表、右会话详情。因此这里只提供
 * 极窄的图标导航轨 + 顶栏，把横向空间最大限度让给业务内容。
 */
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const navItems = [
  { name: 'session-list', path: '/console/sessions', label: '会话工作台', glyph: '💬' },
  { name: 'ticket-list', path: '/console/tickets', label: '工单', glyph: '🎫' },
] as const

const activeName = computed(() => {
  const current = String(route.name ?? '')
  if (current.startsWith('session')) return 'session-list'
  if (current.startsWith('ticket')) return 'ticket-list'
  return ''
})

const roleLabel = computed(() => ROLE_LABELS[auth.role])

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('退出后需重新登录才能继续接待，确认退出？', '退出登录', {
      confirmButtonText: '确认退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await auth.logout()
    ElMessage.success('已退出登录')
    void router.replace({ name: 'login' })
  } catch {
    // 用户取消
  }
}

function goAdmin(): void {
  void router.push('/admin/metrics')
}

function goPassword(): void {
  void router.push({ name: 'account-password' })
}
</script>

<template>
  <div class="console-layout">
    <aside class="console-rail">
      <div class="console-rail__brand" title="玩可爱智能客服">
        <span class="console-rail__logo">玩</span>
      </div>

      <nav class="console-rail__nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.name"
          :to="item.path"
          class="console-rail__item"
          :class="{ 'is-active': activeName === item.name }"
        >
          <span class="console-rail__glyph">{{ item.glyph }}</span>
          <span class="console-rail__text">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="console-rail__foot">
        <button class="console-rail__item" type="button" @click="goAdmin">
          <span class="console-rail__glyph">⚙️</span>
          <span class="console-rail__text">运营后台</span>
        </button>
      </div>
    </aside>

    <div class="console-main">
      <header class="console-topbar">
        <div class="console-topbar__title">
          <span class="console-topbar__system">坐席工作台</span>
          <span class="console-topbar__sep">/</span>
          <span class="console-topbar__page">{{ route.meta.title }}</span>
        </div>

        <div class="console-topbar__spacer" />

        <!-- HC-2：顶栏不展示任何买家身份信息，只展示坐席自身归属 -->
        <div class="console-topbar__identity">
          <span class="wk-pill wk-pill--brand">{{ roleLabel }}</span>
          <span class="console-topbar__name">{{ auth.displayName }}</span>
          <span v-if="auth.user" class="console-topbar__group">{{ auth.user.group_name }}</span>
          <el-button link type="primary" size="small" @click="goPassword">改密</el-button>
          <el-button link type="primary" size="small" @click="handleLogout">退出</el-button>
        </div>
      </header>

      <main class="console-body">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </main>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.console-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: $ink-50;
}

.console-rail {
  display: flex;
  flex-direction: column;
  flex: 0 0 $rail-width;
  width: $rail-width;
  background: $ink-900;
  color: #fff;

  &__brand {
    display: flex;
    align-items: center;
    justify-content: center;
    height: $topbar-height;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }

  &__logo {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: $radius-md;
    background: linear-gradient(135deg, $brand-400, $brand-600);
    font-size: 17px;
    font-weight: 700;
  }

  &__nav {
    display: flex;
    flex-direction: column;
    gap: $space-1;
    padding: $space-3 $space-2;
  }

  &__foot {
    margin-top: auto;
    padding: $space-3 $space-2;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
  }

  &__item {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    width: 100%;
    padding: $space-2 2px;
    border: 0;
    border-radius: $radius-sm;
    background: transparent;
    color: rgba(255, 255, 255, 0.62);
    font-family: inherit;
    font-size: 11px;
    cursor: pointer;
    transition:
      background 0.15s ease,
      color 0.15s ease;

    &:hover {
      background: rgba(255, 255, 255, 0.08);
      color: #fff;
    }

    &.is-active {
      background: rgba(242, 101, 63, 0.18);
      color: #fff;
      box-shadow: inset 2px 0 0 $brand-400;
    }
  }

  &__glyph {
    font-size: 18px;
    line-height: 1.2;
  }

  &__text {
    letter-spacing: 0.02em;
  }
}

.console-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.console-topbar {
  display: flex;
  align-items: center;
  gap: $space-3;
  flex: 0 0 $topbar-height;
  height: $topbar-height;
  padding: 0 $space-5;
  background: $surface;
  border-bottom: 1px solid $ink-200;

  &__title {
    display: flex;
    align-items: baseline;
    gap: $space-2;
  }

  &__system {
    font-size: $text-md;
    font-weight: 600;
    color: $ink-900;
  }

  &__sep {
    color: $ink-300;
  }

  &__page {
    font-size: $text-sm;
    color: $ink-600;
  }

  &__spacer {
    flex: 1;
  }

  &__identity {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__name {
    font-size: $text-sm;
    font-weight: 500;
  }

  &__group {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.console-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
