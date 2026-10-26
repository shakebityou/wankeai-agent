<script setup lang="ts">
import {
  CHANNEL_LABELS,
  HANDOFF_REASON_LABELS,
  PRIORITY_LABELS,
  REVIEW_STATUS_LABELS,
  TICKET_STATUS_LABELS,
  type Channel,
  type HandoffReason,
  type Priority,
  type ReviewStatus,
  type TicketStatus,
} from '@/types/contract'

/**
 * 语义标签 —— 把契约枚举映射为中文 + 语义色。
 *
 * 统一组件的原因：同一个枚举在列表、详情、看板里都要出现，
 * 若各页各写 el-tag + 颜色，色板会立刻失控（尤其优先级与审核状态）。
 *
 * 合规说明（4.5.2）：这里只做「枚举 -> 中文」映射，
 * 不渲染任何链接，也不展示任何凭证类字段。
 */
type Tone = 'neutral' | 'ok' | 'warn' | 'bad' | 'info' | 'brand' | 'mint'

interface Props {
  /** 标签种类 */
  kind: 'channel' | 'ticket-status' | 'priority' | 'review-status' | 'handoff-reason' | 'tone'
  /** 枚举值；kind=tone 时直接传 tone */
  value: string
  /** 覆盖默认文案 */
  label?: string
  /** 是否显示前置圆点（列表里用于快速扫读） */
  dot?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  label: '',
  dot: false,
})

/** 各枚举 -> 语义色。刻意让「需要立刻处理」的状态用 bad/warn 抢占注意力。 */
const TICKET_STATUS_TONE: Record<TicketStatus, Tone> = {
  OPEN: 'bad',
  PROCESSING: 'warn',
  CLOSED: 'neutral',
}

const PRIORITY_TONE: Record<Priority, Tone> = {
  URGENT: 'bad',
  HIGH: 'warn',
  NORMAL: 'info',
  LOW: 'neutral',
}

const REVIEW_STATUS_TONE: Record<ReviewStatus, Tone> = {
  PENDING: 'warn',
  APPROVED: 'ok',
  REJECTED: 'bad',
  DISABLED: 'neutral',
}

const HANDOFF_REASON_TONE: Record<HandoffReason, Tone> = {
  USER_REQUEST: 'info',
  EMOTION: 'warn',
  UNRESOLVED: 'warn',
  HIGH_RISK: 'bad',
  SYSTEM_ERROR: 'bad',
}

const computed = ((): { text: string; tone: Tone } => {
  switch (props.kind) {
    case 'channel':
      return {
        text: CHANNEL_LABELS[props.value as Channel] ?? props.value,
        tone: 'mint',
      }
    case 'ticket-status':
      return {
        text: TICKET_STATUS_LABELS[props.value as TicketStatus] ?? props.value,
        tone: TICKET_STATUS_TONE[props.value as TicketStatus] ?? 'neutral',
      }
    case 'priority':
      return {
        text: PRIORITY_LABELS[props.value as Priority] ?? props.value,
        tone: PRIORITY_TONE[props.value as Priority] ?? 'neutral',
      }
    case 'review-status':
      return {
        text: REVIEW_STATUS_LABELS[props.value as ReviewStatus] ?? props.value,
        tone: REVIEW_STATUS_TONE[props.value as ReviewStatus] ?? 'neutral',
      }
    case 'handoff-reason':
      return {
        text: HANDOFF_REASON_LABELS[props.value as HandoffReason] ?? props.value,
        tone: HANDOFF_REASON_TONE[props.value as HandoffReason] ?? 'neutral',
      }
    case 'tone':
    default:
      return { text: props.label || props.value, tone: (props.value as Tone) ?? 'neutral' }
  }
})()
</script>

<template>
  <span class="wk-pill" :class="`wk-pill--${computed.tone}`">
    <span v-if="props.dot" class="enum-tag__dot" />
    {{ props.label || computed.text }}
  </span>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.enum-tag__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.75;
}
</style>
