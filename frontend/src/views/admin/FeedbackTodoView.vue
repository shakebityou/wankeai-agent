<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import FeedbackTodo from '@/components/feedback/FeedbackTodo.vue'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'

const auth = useAuthStore()

/**
 * 纠错待办 —— FE-08（P9）。
 *
 * 任务书：FeedbackTodo.vue（待办 + 指派）
 * DoD：「标记到待办 ≤3 秒」「待办可指派/关闭/关联 chunk 修改」
 *      「错误类型分布可统计，可与商家历史分布对比」
 *
 * 设计决策：
 *   **指派与处理权限在页面判定后下传** —— 坐席只提交、主管只指派、运营只处理，
 *   三个角色看到的是同一页但可点的按钮不同。权限不下传的话，组件内部各判一次，
 *   三处口径一旦漂移就会出现「看得见点不了」。
 */
const route = useRoute()
const router = useRouter()

const assignable = computed(() => canDo(auth.role, 'feedback.assign'))
const handleable = computed(() => canDo(auth.role, 'feedback.handle'))

function openChunk(chunkId: string): void {
  router.push({ name: 'kb-versions', params: { chunkId } })
}

function openSession(sessionId: string): void {
  router.push({ name: 'session-detail', params: { sessionId } })
}

/** 从 URL 带入的筛选（会话页「这条答错了」跳过来时带上） */
const operator = computed(() => auth.displayName)
void route.query.message_id
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">纠错待办</h1>
        <p class="wk-page__subtitle">
          坐席在会话中标记「这条答错了」后会生成待办。每条待办都指向具体的 AI 回复与命中的知识条目。
        </p>
      </div>
    </header>

    <section class="wk-card">
      <div class="wk-card__body">
        <FeedbackTodo
          :operator="operator"
          :assignable="assignable"
          :handleable="handleable"
          @open-chunk="openChunk"
          @open-session="openSession"
        />
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;
</style>
