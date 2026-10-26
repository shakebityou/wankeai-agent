<script setup lang="ts">
import { computed } from 'vue'
import { COMPANY_LABELS, LOGISTICS_STATUS_LABELS } from '@/types/contract'
import type { LogisticsInfo } from '@/types/order'

/**
 * 物流时间轴 —— FE-14。
 *
 * 任务书 DoD：
 *   「物流七种状态全部渲染正确」
 *   「物流时间轴按时间倒序最新在最上」
 *   「物流 traces[].desc 原文展示不改写」
 *
 * 三个关键设计：
 *
 * 1. **按时间倒序，最新在最上**。物流轨迹是「越新越重要」，
 *    买家问的永远是「现在到哪了」，因此最新的必须第一眼看到，
 *    不需要坐席滚动到底部去找。
 *
 * 2. **desc 原文展示，一个字都不改写**。
 *    任务书明确要求。原因：物流描述里常带地名与状态（「已到达【杭州转运中心】」），
 *    任何「优化措辞」都可能把「已到达」改成「已派送」这类事实错误，
 *    一旦坐席照着念给买家，就是一次错误承诺。
 *
 * 3. **停滞高亮**。stalled_days > 0 时顶部给出橙色提示，
 *    因为物流停滞是买家投诉的高频原因，坐席需要主动先说，
 *    而不是等买家问「怎么不动了」。
 */
interface Props {
  logistics: LogisticsInfo
  /** 最多展示多少条轨迹（0 表示全部） */
  limit?: number
}

const props = withDefaults(defineProps<Props>(), { limit: 0 })

const companyLabel = computed(() => COMPANY_LABELS[props.logistics.company])
const statusLabel = computed(() => LOGISTICS_STATUS_LABELS[props.logistics.status])

/**
 * 倒序排列：最新在最上。
 * 用副本排序，避免直接改 props（Vue 单向数据流）。
 */
const traces = computed(() => {
  const sorted = [...props.logistics.traces].sort(
    (a, b) => Date.parse(b.time) - Date.parse(a.time),
  )
  return props.limit > 0 ? sorted.slice(0, props.limit) : sorted
})

/** 物流状态 -> 语义色 */
function statusTone(status: LogisticsInfo['status']): string {
  switch (status) {
    case 'DELIVERED':
      return 'ok'
    case 'EXCEPTION':
    case 'RETURNED':
      return 'bad'
    case 'OUT_FOR_DELIVERY':
      return 'info'
    default:
      return 'neutral'
  }
}

/** 轨迹时间显示为 MM-DD HH:mm（与订单模板口径一致） */
function formatTraceTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="logistics">
    <header class="logistics__head">
      <span class="logistics__company">{{ companyLabel }}</span>
      <span class="logistics__no wk-mono">{{ props.logistics.tracking_no }}</span>
      <span class="logistics__status" :class="`logistics__status--${statusTone(props.logistics.status)}`">
        {{ statusLabel }}
      </span>
    </header>

    <!-- 停滞告警：买家投诉高频原因，坐席应主动先提 -->
    <div v-if="props.logistics.stalled_days && props.logistics.stalled_days > 0" class="logistics__stalled">
      ⚠️ 物流已停滞 {{ props.logistics.stalled_days }} 天，建议主动为买家催促
    </div>

    <ol class="logistics__timeline">
      <li v-for="(trace, index) in traces" :key="index" class="trace" :class="{ 'is-latest': index === 0 }">
        <span class="trace__dot" />
        <div class="trace__body">
          <span class="trace__time wk-mono">{{ formatTraceTime(trace.time) }}</span>
          <!-- desc 原文展示，不做任何改写（FE-14 硬要求） -->
          <span class="trace__desc">{{ trace.desc }}</span>
          <span v-if="trace.location" class="trace__location">{{ trace.location }}</span>
        </div>
      </li>
    </ol>

    <p v-if="props.logistics.estimated_delivery" class="logistics__eta">
      预计送达：{{ props.logistics.estimated_delivery }}
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.logistics {
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
  }

  &__company {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
  }

  &__no {
    font-size: 11px;
    color: $ink-500;
  }

  &__status {
    padding: 0 6px;
    border-radius: $radius-pill;
    font-size: 11px;
    font-weight: 600;

    &--ok {
      background: $success-bg;
      color: $success-ink;
    }

    &--bad {
      background: $danger-bg;
      color: $danger-ink;
    }

    &--info {
      background: $info-bg;
      color: $info-ink;
    }

    &--neutral {
      background: $ink-100;
      color: $ink-600;
    }
  }

  &__stalled {
    padding: $space-1 $space-2;
    border-radius: $radius-xs;
    background: $warning-bg;
    color: $warning-ink;
    font-size: $text-xs;
    font-weight: 600;
  }

  &__timeline {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    position: relative;
  }

  &__eta {
    margin: 0;
    font-size: 11px;
    color: $ink-500;
  }
}

.trace {
  display: flex;
  gap: $space-2;
  position: relative;
  padding-bottom: $space-3;

  &:last-child {
    padding-bottom: 0;

    // 最后一条不画竖线，避免时间轴悬空
    .trace__dot::after {
      display: none;
    }
  }

  &__dot {
    flex: none;
    position: relative;
    width: 10px;
    padding-top: 4px;

    &::before {
      content: '';
      position: absolute;
      top: 4px;
      left: 2px;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: $ink-300;
    }

    &::after {
      content: '';
      position: absolute;
      top: 12px;
      left: 4px;
      width: 2px;
      bottom: -4px;
      background: $ink-150;
    }
  }

  &.is-latest &__dot::before {
    background: $brand-500;
    box-shadow: 0 0 0 3px $brand-50;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    font-size: $text-xs;
  }

  &__time {
    color: $ink-500;
  }

  &__desc {
    color: $ink-800;
    line-height: 1.6;
  }

  &.is-latest &__desc {
    color: $ink-900;
    font-weight: 600;
  }

  &__location {
    color: $ink-400;
  }
}
</style>
