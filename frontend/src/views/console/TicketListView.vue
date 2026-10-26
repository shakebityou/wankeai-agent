<script setup lang="ts">
import { useRouter } from 'vue-router'
import TicketList from '@/components/ticket/TicketList.vue'

/**
 * 工单列表页（P5）—— 坐席工作台。
 *
 * 页面本身只做两件事：标题区 + 把工单列表嵌进来。
 * 筛选、分页、三态都在 TicketList 组件内，便于 FE-02 的单测直接针对组件写。
 */
const router = useRouter()

function openTicket(ticketNo: string): void {
  void router.push({ name: 'ticket-detail', params: { ticketNo } })
}
</script>

<template>
  <div class="wk-page ticket-page">
    <header class="wk-page__header">
      <div>
        <h2 class="wk-page__title">工单列表</h2>
        <p class="wk-page__subtitle">
          转人工生成的工单，按「SLA 超时 → 优先级 → 等待时长」排序，超时工单置顶提醒。
        </p>
      </div>
    </header>

    <TicketList @select="openTicket" />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.ticket-page {
  display: flex;
  flex-direction: column;
  gap: $space-4;
  min-height: 0;
}
</style>
