<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ROLE_LABELS, canAccess } from '@/permission/matrix'
import { useAuthStore } from '@/stores/auth'

/**
 * 运营后台布局（P6–P13 容器）。
 *
 * 与坐席台的区别：运营后台是「阅读 + 配置」型界面，
 * 因此用完整侧边导航（带分组标题）而非图标轨，便于在 8 个页面间定位。
 * 无权访问的菜单项直接不渲染 —— 与 4.5.4 权限矩阵一致。
 */
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

interface NavGroup {
  title: string
  items: Array<{ name: string; path: string; label: string; glyph: string; permission: string }>
}

const groups: NavGroup[] = [
  {
    title: '效果与质量',
    items: [
      {
        name: 'metrics',
        path: '/admin/metrics',
        label: '指标看板',
        glyph: '📊',
        permission: 'admin.metrics',
      },
      {
        name: 'feedback-todo',
        path: '/admin/feedback',
        label: '纠错待办',
        glyph: '🩹',
        permission: 'admin.feedback',
      },
    ],
  },
  {
    title: '知识库',
    items: [
      {
        name: 'kb-list',
        path: '/admin/kb',
        label: '知识条目',
        glyph: '📚',
        permission: 'admin.kb',
      },
      {
        name: 'kb-import',
        path: '/admin/kb/import',
        label: '批量导入',
        glyph: '📥',
        permission: 'admin.kb.write',
      },
    ],
  },
  {
    title: '系统配置',
    items: [
      {
        name: 'channels',
        path: '/admin/channels',
        label: '渠道与授权',
        glyph: '🔌',
        permission: 'admin.config',
      },
      {
        name: 'agent-groups',
        path: '/admin/agent-groups',
        label: '分组与排班',
        glyph: '🗓️',
        permission: 'admin.config',
      },
      {
        name: 'handoff-rules',
        path: '/admin/handoff-rules',
        label: '转人工规则',
        glyph: '🚦',
        permission: 'admin.config',
      },
    ],
  },
  {
    title: '账号与安全',
    items: [
      {
        name: 'user-manage',
        path: '/admin/users',
        label: '账号管理',
        glyph: '🪪',
        permission: 'admin.users',
      },
    ],
  },
]

const visibleGroups = computed(() =>
  groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) =>
        canAccess(auth.role, item.permission as Parameters<typeof canAccess>[1]),
      ),
    }))
    .filter((group) => group.items.length > 0),
)

const activeName = computed(() => {
  const current = String(route.name ?? '')
  if (current === 'kb-versions') return 'kb-list'
  return current
})

const roleLabel = computed(() => ROLE_LABELS[auth.role])

function isActive(name: string): boolean {
  if (name === 'kb-list') return activeName.value === 'kb-list' || activeName.value === 'kb-versions'
  return activeName.value === name
}

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确认退出运营后台？', '退出登录', {
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

function goConsole(): void {
  void router.push('/console/sessions')
}

function goPassword(): void {
  void router.push({ name: 'account-password' })
}
</script>

<template>
  <div class="admin-layout">
    <aside class="admin-side">
      <div class="admin-side__brand">
        <span class="admin-side__logo">玩</span>
        <div class="admin-side__brandtext">
          <span class="admin-side__brandname">玩可爱智能客服</span>
          <span class="admin-side__brandsub">运营后台</span>
        </div>
      </div>

      <nav class="admin-side__nav">
        <div v-for="group in visibleGroups" :key="group.title" class="admin-side__group">
          <div class="admin-side__grouptitle">{{ group.title }}</div>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="item.path"
            class="admin-side__item"
            :class="{ 'is-active': isActive(item.name) }"
          >
            <span class="admin-side__glyph">{{ item.glyph }}</span>
            <span>{{ item.label }}</span>
          </RouterLink>
        </div>
      </nav>

      <div class="admin-side__foot">
        <button class="admin-side__back" type="button" @click="goConsole">
          ← 返回坐席工作台
        </button>
      </div>
    </aside>

    <div class="admin-main">
      <header class="admin-topbar">
        <div class="admin-topbar__title">{{ route.meta.title }}</div>
        <div class="admin-topbar__spacer" />
        <div class="admin-topbar__identity">
          <span class="wk-pill wk-pill--mint">{{ roleLabel }}</span>
          <span class="admin-topbar__name">{{ auth.displayName }}</span>
          <el-button link type="primary" size="small" @click="goPassword">改密</el-button>
          <el-button link type="primary" size="small" @click="handleLogout">退出</el-button>
        </div>
      </header>

      <main class="admin-body">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.admin-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: $ink-50;
}

.admin-side {
  display: flex;
  flex-direction: column;
  flex: 0 0 $sidebar-width;
  width: $sidebar-width;
  background: $surface;
  border-right: 1px solid $ink-200;

  &__brand {
    display: flex;
    align-items: center;
    gap: $space-3;
    height: $topbar-height;
    padding: 0 $space-4;
    border-bottom: 1px solid $ink-150;
  }

  &__logo {
    display: grid;
    place-items: center;
    flex: 0 0 30px;
    width: 30px;
    height: 30px;
    border-radius: $radius-sm;
    background: linear-gradient(135deg, $brand-400, $brand-600);
    color: #fff;
    font-size: 15px;
    font-weight: 700;
  }

  &__brandtext {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  &__brandname {
    font-size: $text-sm;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  &__brandsub {
    font-size: 11px;
    color: $ink-500;
  }

  &__nav {
    flex: 1;
    overflow-y: auto;
    padding: $space-3 $space-2;
  }

  &__group + &__group {
    margin-top: $space-4;
  }

  &__grouptitle {
    padding: 0 $space-2 $space-2;
    font-size: 11px;
    font-weight: 600;
    color: $ink-400;
    letter-spacing: 0.06em;
  }

  &__item {
    display: flex;
    align-items: center;
    gap: $space-2;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    color: $ink-700;
    font-size: $text-sm;
    transition:
      background 0.15s ease,
      color 0.15s ease;

    &:hover {
      background: $ink-100;
      color: $ink-900;
    }

    &.is-active {
      background: $brand-50;
      color: $brand-ink;
      font-weight: 600;
    }
  }

  &__glyph {
    font-size: 15px;
    line-height: 1;
  }

  &__foot {
    padding: $space-3;
    border-top: 1px solid $ink-150;
  }

  &__back {
    width: 100%;
    padding: $space-2;
    border: 1px solid $ink-200;
    border-radius: $radius-sm;
    background: transparent;
    color: $ink-600;
    font-family: inherit;
    font-size: $text-xs;
    cursor: pointer;

    &:hover {
      border-color: $brand-300;
      color: $brand-ink;
    }
  }
}

.admin-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.admin-topbar {
  display: flex;
  align-items: center;
  gap: $space-3;
  flex: 0 0 $topbar-height;
  height: $topbar-height;
  padding: 0 $space-6;
  background: $surface;
  border-bottom: 1px solid $ink-200;

  &__title {
    font-size: $text-lg;
    font-weight: 600;
    letter-spacing: -0.01em;
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
}

.admin-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
</style>
