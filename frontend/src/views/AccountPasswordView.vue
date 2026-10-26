<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { changePasswordApi } from '@/api/auth'
import { homeFor } from '@/router'

/**
 * 修改密码 —— 任何角色都能进。
 *
 * 为什么单独成页而不是弹窗：
 * 1. 首次登录被强制改密时会直接落到这里（must_change=1），弹窗关不掉更难处理；
 * 2. 改密成功后服务端会吊销全部会话，页面必须能明确告诉用户「现在请重新登录」，
 *    弹窗一闪而过容易让人以为界面卡住了。
 */
const router = useRouter()
const auth = useAuthStore()

const form = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
const submitting = ref(false)
const fieldError = reactive<Record<string, string>>({})

const strength = computed(() => {
  const value = form.newPassword
  if (!value) return { level: 0, text: '请输入新密码', tone: 'idle' }
  if (value.length < 8) return { level: 1, text: `还差 ${8 - value.length} 位`, tone: 'bad' }
  const hasLetter = /[a-zA-Z]/.test(value)
  const hasDigit = /\d/.test(value)
  const hasSymbol = /[^a-zA-Z0-9]/.test(value)
  const score = [hasLetter, hasDigit, hasSymbol].filter(Boolean).length
  if (!hasLetter || !hasDigit) return { level: 1, text: '需同时包含字母和数字', tone: 'bad' }
  if (value.length >= 12 && score === 3) return { level: 3, text: '强密码', tone: 'ok' }
  return { level: 2, text: '中等强度，建议加符号并延长', tone: 'warn' }
})

function validate(): boolean {
  for (const key of Object.keys(fieldError)) delete fieldError[key]

  if (!form.oldPassword) fieldError.oldPassword = '请输入原密码'
  if (!form.newPassword) {
    fieldError.newPassword = '请输入新密码'
  } else if (form.newPassword.length < 8) {
    fieldError.newPassword = '密码至少 8 位'
  } else if (!/[a-zA-Z]/.test(form.newPassword) || !/\d/.test(form.newPassword)) {
    fieldError.newPassword = '密码需同时包含字母和数字'
  } else if (form.newPassword === form.oldPassword) {
    fieldError.newPassword = '新密码不能与原密码相同'
  }
  if (!form.confirmPassword) {
    fieldError.confirmPassword = '请再次输入新密码'
  } else if (form.confirmPassword !== form.newPassword) {
    fieldError.confirmPassword = '两次输入的密码不一致'
  }

  return Object.keys(fieldError).length === 0
}

async function submit(): Promise<void> {
  if (submitting.value) return
  if (!validate()) return

  submitting.value = true
  try {
    await changePasswordApi(form.oldPassword, form.newPassword)
    ElMessage.success('密码已修改，请用新密码重新登录')
    // 服务端已吊销所有会话，本地身份也要一起清掉
    await auth.logout()
    void router.replace({ name: 'login' })
  } catch (error) {
    const err = error as { code?: number; message?: string }
    // 10001 是字段级错误，落到原密码输入框旁边最直观
    if (err?.code === 10001) {
      fieldError.oldPassword = err.message ?? '修改失败'
    }
  } finally {
    submitting.value = false
  }
}

const backTarget = computed(() => homeFor(auth.role))

function goBack(): void {
  void router.replace(backTarget.value)
}
</script>

<template>
  <div class="pwd">
    <el-card class="pwd__card" shadow="never">
      <header class="pwd__head">
        <h1 class="pwd__title">修改密码</h1>
        <p class="pwd__sub">
          <template v-if="auth.user?.must_change">
            当前使用的是初始密码，修改后才能继续使用系统。
          </template>
          <template v-else> 修改成功后所有已登录设备都会被强制退出，需要重新登录。 </template>
        </p>
      </header>

      <div class="pwd__identity">
        <span class="pwd__identity-name">{{ auth.displayName }}</span>
        <span class="pwd__identity-account">{{ auth.user?.username }}</span>
      </div>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="原密码" :error="fieldError.oldPassword">
          <el-input
            v-model="form.oldPassword"
            type="password"
            show-password
            size="large"
            placeholder="请输入当前密码"
            autocomplete="current-password"
          />
        </el-form-item>

        <el-form-item label="新密码" :error="fieldError.newPassword">
          <el-input
            v-model="form.newPassword"
            type="password"
            show-password
            size="large"
            placeholder="至少 8 位，需含字母和数字"
            autocomplete="new-password"
          />
          <div v-if="form.newPassword" class="pwd__strength">
            <span class="pwd__bars">
              <i
                v-for="n in 3"
                :key="n"
                class="pwd__bar"
                :class="[`is-${strength.tone}`, { 'is-on': n <= strength.level }]"
              />
            </span>
            <span class="pwd__strength-text" :class="`is-${strength.tone}`">{{ strength.text }}</span>
          </div>
        </el-form-item>

        <el-form-item label="确认新密码" :error="fieldError.confirmPassword">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            show-password
            size="large"
            placeholder="再输入一次新密码"
            autocomplete="new-password"
          />
        </el-form-item>

        <div class="pwd__actions">
          <el-button v-if="!auth.user?.must_change" size="large" @click="goBack">返回</el-button>
          <el-button type="primary" size="large" :loading="submitting" @click="submit">
            确认修改
          </el-button>
        </div>
      </el-form>

      <p class="pwd__hint">
        密码以 scrypt 加盐哈希存储，服务端与前端都不会保存明文；修改记录写入审计日志。
      </p>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.pwd {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: $space-8;
  background:
    radial-gradient(900px 500px at 10% -10%, $brand-100 0%, transparent 55%),
    $ink-50;

  &__card {
    width: 100%;
    max-width: 460px;
    border-radius: $radius-lg;
    box-shadow: $shadow-lg;
  }

  &__head {
    margin-bottom: $space-4;
  }

  &__title {
    margin: 0 0 $space-1;
    font-size: $text-2xl;
    font-weight: 700;
    color: $ink-900;
  }

  &__sub {
    margin: 0;
    font-size: $text-xs;
    line-height: 1.7;
    color: $ink-500;
  }

  &__identity {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-bottom: $space-5;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $ink-50;

    &-name {
      font-size: $text-sm;
      font-weight: 600;
      color: $ink-800;
    }

    &-account {
      font-size: $text-xs;
      color: $ink-500;
    }
  }

  &__strength {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-top: 6px;
  }

  &__bars {
    display: flex;
    gap: 3px;
  }

  &__bar {
    display: block;
    width: 26px;
    height: 4px;
    border-radius: 2px;
    background: $ink-200;

    &.is-on.is-bad {
      background: $danger-ink;
    }

    &.is-on.is-warn {
      background: $warning-ink;
    }

    &.is-on.is-ok {
      background: $success-ink;
    }
  }

  &__strength-text {
    font-size: 11px;

    &.is-bad {
      color: $danger-ink;
    }

    &.is-warn {
      color: $warning-ink;
    }

    &.is-ok {
      color: $success-ink;
    }

    &.is-idle {
      color: $ink-400;
    }
  }

  &__actions {
    display: flex;
    justify-content: flex-end;
    gap: $space-3;
    margin-top: $space-2;
  }

  &__hint {
    margin: $space-4 0 0;
    font-size: 11px;
    line-height: 1.7;
    color: $ink-400;
  }
}
</style>
