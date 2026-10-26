import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { canAccess, type RoutePermission } from '@/permission/matrix'

/**
 * 路由表 —— 对齐任务书 4.2「信息架构：两个系统 / 四类页面 / 13 页面」。
 *
 * 系统一 坐席工作台：P1 会话列表、P2 会话详情、P4 会话回放、P5 工单列表/详情
 * 系统二 运营后台：P6 知识库、P7 批量导入、P8 版本与生效时间、P9 纠错待办、
 *                  P10 指标看板、P11 渠道授权、P12 分组排班、P13 转人工规则
 *
 * meta.permission 对应 4.5.4 角色权限矩阵，由全局守卫强制执行；
 * 前端守卫只是体验层，最终以后端 BFF 的 10003 为准（不重复实现鉴权）。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    redirect: '/console/sessions',
  },
  {
    path: '/console',
    component: () => import('@/layouts/ConsoleLayout.vue'),
    redirect: '/console/sessions',
    meta: { system: 'console', title: '坐席工作台' },
    children: [
      {
        path: 'sessions',
        name: 'session-list',
        component: () => import('@/views/console/SessionWorkbenchView.vue'),
        meta: { title: '会话工作台', permission: 'console.sessions', keepQuery: true },
      },
      {
        path: 'sessions/:sessionId',
        name: 'session-detail',
        component: () => import('@/views/console/SessionWorkbenchView.vue'),
        meta: { title: '会话详情', permission: 'console.sessions', props: true },
      },
      {
        path: 'replay/:sessionId',
        name: 'session-replay',
        component: () => import('@/views/console/SessionReplayView.vue'),
        meta: { title: '会话回放', permission: 'console.sessions' },
      },
      {
        path: 'tickets',
        name: 'ticket-list',
        component: () => import('@/views/console/TicketListView.vue'),
        meta: { title: '工单列表', permission: 'console.tickets' },
      },
      {
        path: 'tickets/:ticketNo',
        name: 'ticket-detail',
        component: () => import('@/views/console/TicketDetailView.vue'),
        meta: { title: '工单详情', permission: 'console.tickets' },
      },
    ],
  },
  {
    path: '/admin',
    component: () => import('@/layouts/AdminLayout.vue'),
    redirect: '/admin/metrics',
    meta: { system: 'admin', title: '运营后台' },
    children: [
      {
        path: 'metrics',
        name: 'metrics',
        component: () => import('@/views/admin/MetricsDashboardView.vue'),
        meta: { title: '指标看板', permission: 'admin.metrics' },
      },
      {
        path: 'kb',
        name: 'kb-list',
        component: () => import('@/views/admin/KbListView.vue'),
        meta: { title: '知识库管理', permission: 'admin.kb' },
      },
      {
        path: 'kb/import',
        name: 'kb-import',
        component: () => import('@/views/admin/KbImportView.vue'),
        meta: { title: '批量导入', permission: 'admin.kb.write' },
      },
      {
        path: 'kb/:chunkId/versions',
        name: 'kb-versions',
        component: () => import('@/views/admin/KbVersionView.vue'),
        meta: { title: '版本与生效时间', permission: 'admin.kb' },
      },
      {
        path: 'feedback',
        name: 'feedback-todo',
        component: () => import('@/views/admin/FeedbackTodoView.vue'),
        meta: { title: '纠错待办', permission: 'admin.feedback' },
      },
      {
        path: 'channels',
        name: 'channels',
        component: () => import('@/views/admin/ChannelsView.vue'),
        meta: { title: '渠道与授权', permission: 'admin.config' },
      },
      {
        path: 'agent-groups',
        name: 'agent-groups',
        component: () => import('@/views/admin/AgentGroupsView.vue'),
        meta: { title: '分组与排班', permission: 'admin.config' },
      },
      {
        path: 'handoff-rules',
        name: 'handoff-rules',
        component: () => import('@/views/admin/HandoffRulesView.vue'),
        meta: { title: '转人工规则', permission: 'admin.config' },
      },
      {
        path: 'users',
        name: 'user-manage',
        component: () => import('@/views/admin/UserManageView.vue'),
        meta: { title: '账号管理', permission: 'admin.users' },
      },
    ],
  },
  {
    // 任何角色都能改自己的密码，所以挂在顶层而不是某个系统下面
    path: '/account/password',
    name: 'account-password',
    component: () => import('@/views/AccountPasswordView.vue'),
    meta: { title: '修改密码' },
  },
  {
    // 4.5.4 权限矩阵拦截后的落地页：说明「为什么看不到」而不是丢回 404
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/ForbiddenView.vue'),
    meta: { public: true, title: '无权限访问' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]

export const router = createRouter({
  // hash 模式：坐席台部署在 Nginx 静态目录下，无需后端 rewrite 规则即可直达深层页面。
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/**
 * 全局守卫。
 *
 * 关键点：**首次导航必须先问服务端「我是谁」**。
 * Cookie 是 HttpOnly，刷新页面后前端手上什么都没有，
 * 如果不等 ready 就直接判「未登录」，每次刷新都会被踢回登录页。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    if (to.name === 'login') {
      // 进登录页时强制复核一次：可能刚被别处退出、或 Cookie 已过期。
      // 只在内存里缓存身份会让用户看到「已登录却在登录页」的矛盾状态。
      await auth.restore(true)
      if (auth.isAuthenticated) return { path: homeFor(auth.role) }
    }
    return true
  }

  await auth.restore()

  if (!auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  const permission = to.meta.permission as RoutePermission | undefined
  if (permission && !canAccess(auth.role, permission)) {
    return { name: 'forbidden' }
  }

  return true
})

/** 各角色的默认首页。坐席/主管进工作台，其余进指标看板。 */
export function homeFor(role: string): string {
  return role === 'AGENT' || role === 'SUPERVISOR' ? '/console/sessions' : '/admin/metrics'
}

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 玩可爱智能客服` : '玩可爱智能客服'
})
