<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TicketDetail from '@/components/ticket/TicketDetail.vue'

/**
 * 工单详情页（P5）—— 坐席工作台。
 *
 * 合规红线（4.5.2）：本页**不提供任何导出功能**，也不渲染跳转会话的外链。
 * 「查看关联会话」按钮走前端路由跳转（站内导航），不是 http 外链，符合 HC-1。
 */
const route = useRoute()
const router = useRouter()

const ticketNo = computed(() => String(route.params.ticketNo ?? ''))

function openSession(sessionId: string): void {
  void router.push({ name: 'session-detail', params: { sessionId } })
}
</script>

<template>
  <div class="wk-page ticket-detail-page">
    <header class="wk-page__header">
      <div class="ticket-detail-page__lead">
        <el-button size="small" text @click="router.back()">← 返回工单列表</el-button>
        <h2 class="wk-page__title">工单详情</h2>
      </div>
      <p class="wk-page__subtitle">
        含转人工时刻的 AI 上下文快照；该页不提供导出，如需留档请联系主管。
      </p>
    </header>

    <TicketDetail :ticket-no="ticketNo" />

    <!-- 站内跳转（非外链）：从工单回到原会话继续沟通 -->
    <div class="ticket-detail-page__foot wk-no-print">
      <el-button size="small" plain @click="openSession(ticketNo)">前往关联会话</el-button>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.ticket-detail-page {
  display: flex;
  flex-direction: column;
  gap: $space-4;

  &__lead {
    display: flex;
    align-items: center;
    gap: $space-3;
  }

  &__foot {
    display: flex;
    justify-content: flex-end;
  }
}
</style>
