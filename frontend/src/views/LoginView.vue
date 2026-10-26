<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useAuthStore, type LoginFailure } from '@/stores/auth'
import { canAccess, ROLE_LABELS, type RoutePermission, type UserRole } from '@/permission/matrix'
import { homeFor } from '@/router'

/**
 * 登录页 —— 账号密码登录。
 *
 * 设计要点：
 * 1. **账号密码走服务端**：提交到 /bff/v1/auth/login，成功后服务端下发
 *    HttpOnly Cookie，前端拿不到 token，也不存在「前端存身份」这回事。
 * 2. **失败分支要说清楚**：密码错、账号被停用、连续失败被锁定是三种
 *    完全不同的处置（重试 / 找管理员 / 等 10 分钟），文案不能合并成「登录失败」。
 * 3. **锁定倒计时**：20001 返回后禁用按钮并倒数，避免用户继续猛点加剧锁定。
 * 4. **演示账号可展开**：这是演示环境，得有地方看到有哪些账号；
 *    但默认折叠，避免把「快捷登录」当成正规入口。
 */
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const form = reactive({ username: '', password: '' })
const submitting = ref(false)
const failure = ref<LoginFailure | null>(null)
const showPassword = ref(false)
const showDemo = ref(false)
/** 被锁定时剩余秒数，>0 时按钮禁用 */
const lockedLeft = ref(0)

let ticker: number | null = null

const DEMO_ACCOUNTS: Array<{ username: string; role: UserRole; name: string; note: string }> = [
  { username: 'wangfang', role: 'AGENT', name: '王芳', note: 'G3 售后处理组 · 仅本组会话' },
  { username: 'limin', role: 'SUPERVISOR', name: '李敏', note: 'G1 售前咨询组 · 全部会话，配置只读' },
  { username: 'zhangtao', role: 'KB_OPERATOR', name: '张涛', note: '知识库运营 · 条目与导入' },
  { username: 'chenjing', role: 'ADMIN', name: '陈静', note: '系统管理员 · 全部权限' },
]

const DEMO_PASSWORD = 'Wk@2026demo'

const canSubmit = computed(() => !submitting.value && lockedLeft.value === 0)
const lockedText = computed(() => {
  if (lockedLeft.value <= 0) return ''
  const min = Math.floor(lockedLeft.value / 60)
  const sec = lockedLeft.value % 60
  return `${min}:${String(sec).padStart(2, '0')}`
})

function startCountdown(seconds: number): void {
  lockedLeft.value = seconds
  if (ticker !== null) window.clearInterval(ticker)
  ticker = window.setInterval(() => {
    lockedLeft.value -= 1
    if (lockedLeft.value <= 0 && ticker !== null) {
      window.clearInterval(ticker)
      ticker = null
      // 倒计时结束后清掉锁定提示，让用户能重新尝试
      if (failure.value?.kind === 'LOCKED') failure.value = null
    }
  }, 1000)
}

onBeforeUnmount(() => {
  if (ticker !== null) window.clearInterval(ticker)
})

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  failure.value = null

  if (!form.username.trim()) {
    failure.value = { kind: 'BAD_CREDENTIAL', message: '请输入登录账号' }
    return
  }
  if (!form.password) {
    failure.value = { kind: 'BAD_CREDENTIAL', message: '请输入密码' }
    return
  }

  submitting.value = true
  const outcome = await auth.login(form.username.trim(), form.password)
  submitting.value = false

  if (!outcome.ok) {
    failure.value = outcome.failure
    if (outcome.failure.kind === 'LOCKED') startCountdown(outcome.failure.retryAfterSec)
    return
  }

  ElMessage.success(`欢迎回来，${outcome.user.display_name}`)

  // 首登强制改密：先进设置页，改完再放行
  if (outcome.user.must_change) {
    ElMessage.warning('这是初始密码，请先修改密码')
    void router.replace('/account/password')
    return
  }

  await gotoLanding(outcome.user.role)
}

/**
 * 登录后落点。
 *
 * 优先级：拦截前想去的页面 > 角色默认首页。
 * 但要先确认该角色有权限，否则会出现「刚登录就被弹到无权限页」。
 *
 * 两个坑（都踩过）：
 * 1. `redirect` 可能带着自己的 query（如 /admin/kb?x=1），要把 path 与 query 拆开，
 *    直接把整串当作 path 传给 router.resolve 会解析失败；
 * 2. 同一个标签页里换人登录时，路由可能还停在**上一个人**的页面上，
 *    所以必须按**目标路径**校验权限，不能沿用当前页面的 meta。
 */
async function gotoLanding(role: UserRole): Promise<void> {
  // 唯一的来源是 URL 上的 redirect 参数。
  // 曾经在 store 里缓存一份「登录后回跳」，结果是同标签页换人登录时
  // 会带着上一个人的落地页，把人直接送进无权限页 —— 已废弃。
  const wanted = String(route.query.redirect ?? '')

  // `/` 是重定向占位路由（没有 meta.permission），它不能作为落地目标。
  // 若不排掉，`?redirect=/` 会被解析成一个「无权限要求」的路径而直接放行，
  // 结果是角色专属落地页失效，所有人先被送到 `/` 再被重定向。
  const isPlaceholder = (path: string): boolean => path === '/' || path === ''

  if (wanted && !isPlaceholder(wanted)) {
    const [rawPath, rawQuery = ''] = wanted.split('?')
    const path = rawPath || '/'
    let permission: RoutePermission | undefined
    try {
      permission = router.resolve(path).meta?.permission as RoutePermission | undefined
    } catch {
      // 用户手改过的非法路径：忽略，走角色首页
      permission = undefined
    }

    if (!permission || canAccess(role, permission)) {
      const query: Record<string, string> = {}
      for (const [key, value] of new URLSearchParams(rawQuery)) query[key] = value
      await router.replace({ path, query })
      return
    }
  }

  await router.replace(homeFor(role))
}

function fillDemo(username: string): void {
  form.username = username
  form.password = DEMO_PASSWORD
  failure.value = null
}
</script>

<template>
  <div class="login">
    <div class="login__panel">
      <header class="login__head">
        <span class="login__logo">🧸</span>
        <h1 class="login__title">玩可爱智能客服</h1>
        <p class="login__subtitle">坐席工作台 · 运营后台</p>
      </header>

      <form class="login__form" @submit.prevent="submit">
        <label class="field">
          <span class="field__label">登录账号</span>
          <el-input
            v-model="form.username"
            size="large"
            placeholder="请输入登录账号"
            autocomplete="username"
            :prefix-icon="User"
            :disabled="submitting"
          />
        </label>

        <label class="field">
          <span class="field__label">密码</span>
          <el-input
            v-model="form.password"
            size="large"
            :type="showPassword ? 'text' : 'password'"
            placeholder="请输入密码"
            autocomplete="current-password"
            :prefix-icon="Lock"
            :disabled="submitting"
            show-password
          />
        </label>

        <p v-if="failure" class="login__error" role="alert">
          <span class="login__error-icon">⚠</span>
          <span>{{ failure.message }}</span>
          <span v-if="lockedLeft > 0" class="login__error-timer">{{ lockedText }}</span>
        </p>

        <el-button
          class="login__submit"
          type="primary"
          size="large"
          native-type="submit"
          :loading="submitting"
          :disabled="!canSubmit"
        >
          {{ lockedLeft > 0 ? `账号已锁定 ${lockedText}` : '登 录' }}
        </el-button>
      </form>

      <div class="login__demo">
        <button class="login__demo-toggle" type="button" @click="showDemo = !showDemo">
          <span>{{ showDemo ? '收起' : '展开' }}演示账号</span>
          <span class="login__demo-caret" :class="{ 'is-open': showDemo }">⌄</span>
        </button>

        <ul v-if="showDemo" class="login__demo-list">
          <li v-for="item in DEMO_ACCOUNTS" :key="item.username" class="demo-row" @click="fillDemo(item.username)">
            <div class="demo-row__main">
              <span class="demo-row__name">{{ item.name }}</span>
              <span class="demo-row__role">{{ ROLE_LABELS[item.role] }}</span>
              <code class="demo-row__account">{{ item.username }}</code>
            </div>
            <span class="demo-row__note">{{ item.note }}</span>
          </li>
          <li class="login__demo-foot">
            统一演示密码 <code>{{ DEMO_PASSWORD }}</code> · 点击任一账号自动填入
          </li>
        </ul>
      </div>
    </div>

    <footer class="login__legal">
      数据仅用于本次演示，不出境、不用于模型训练 · 登录行为记入审计日志
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.login {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: $space-5;
  padding: $space-8;
  background:
    radial-gradient(1200px 600px at 15% -10%, $brand-100 0%, transparent 55%),
    radial-gradient(900px 500px at 110% 110%, $mint-bg 0%, transparent 50%),
    $ink-50;

  &__panel {
    width: 100%;
    max-width: 440px;
    padding: $space-8;
    border-radius: $radius-lg;
    background: $surface;
    box-shadow: $shadow-lg;
  }

  &__head {
    text-align: center;
    margin-bottom: $space-6;
  }

  &__logo {
    font-size: 44px;
    line-height: 1;
  }

  &__title {
    margin: $space-3 0 $space-1;
    font-size: $text-2xl;
    font-weight: 700;
    letter-spacing: 1px;
    color: $ink-900;
  }

  &__subtitle {
    margin: 0;
    font-size: $text-sm;
    color: $ink-500;
  }

  &__form {
    display: flex;
    flex-direction: column;
    gap: $space-4;
  }

  &__error {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $danger-bg;
    color: $danger-ink;
    font-size: $text-xs;
    line-height: 1.6;

    &-icon {
      font-size: 13px;
    }

    &-timer {
      margin-left: auto;
      font-variant-numeric: tabular-nums;
      font-weight: 700;
      letter-spacing: 0.5px;
    }
  }

  &__submit {
    width: 100%;
    height: 42px;
    font-size: $text-base;
    letter-spacing: 2px;
  }

  &__demo {
    margin-top: $space-5;
    padding-top: $space-4;
    border-top: 1px dashed $ink-200;
  }

  &__demo-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: $space-2;
    width: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    color: $ink-500;
    font-size: $text-xs;
    cursor: pointer;

    &:hover {
      color: $brand-ink;
    }
  }

  &__demo-caret {
    display: inline-block;
    transition: transform 0.18s ease;

    &.is-open {
      transform: rotate(180deg);
    }
  }

  &__demo-list {
    margin: $space-3 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  &__demo-foot {
    margin-top: $space-2;
    font-size: 11px;
    color: $ink-400;
    text-align: center;
    line-height: 1.7;

    code {
      padding: 1px 5px;
      border-radius: 3px;
      background: $ink-100;
      color: $ink-700;
      font-size: 11px;
    }
  }

  &__legal {
    font-size: 11px;
    color: $ink-400;
    text-align: center;
  }
}

.field {
  display: block;

  &__label {
    display: block;
    margin-bottom: 6px;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-600;
  }
}

.demo-row {
  padding: 8px 10px;
  border: 1px solid $ink-150;
  border-radius: $radius-sm;
  cursor: pointer;
  transition:
    border-color 0.15s ease,
    background 0.15s ease;

  &:hover {
    border-color: $brand-400;
    background: $brand-50;
  }

  &__main {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__name {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
  }

  &__role {
    padding: 1px 6px;
    border-radius: $radius-pill;
    background: $brand-50;
    color: $brand-ink;
    font-size: 11px;
    font-weight: 600;
  }

  &__account {
    margin-left: auto;
    font-size: 11px;
    color: $ink-500;
  }

  &__note {
    display: block;
    margin-top: 2px;
    font-size: 11px;
    color: $ink-500;
  }
}
</style>
