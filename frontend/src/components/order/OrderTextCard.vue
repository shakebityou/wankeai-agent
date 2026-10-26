<script setup lang="ts">
import { computed } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import {
  COMPANY_LABELS,
  ORDER_STATUS_LABELS,
} from '@/types/contract'
import type { OrderInfo } from '@/types/order'

/**
 * 订单文本卡片 —— FE-14。
 *
 * 任务书原文：「OrderTextCard.vue（纯文本分行，替代平台卡片）」
 * 商家 0.8 节：「不接入平台商品/订单卡片」，且 HC-1 禁止任何外链与跳转卡片。
 *
 * 因此这个组件有**两个输出通道**，必须严格区分：
 *
 *   1. 坐席工作台内部渲染（模板部分）：可以用色块、图标、布局，
 *      让坐席一眼看清订单状态，这只是给人看的界面。
 *
 *   2. 发给买家的内容（toPlainText / 模板里的「纯文本预览」区）：
 *      只能是分行纯文本，不能有任何链接、按钮、跳转卡片。
 *      任务书给的模板形态就是：
 *        「您的订单 202501150001 目前状态：已发货 🚚」
 *        「商品：奶油小熊 15cm 挂件 × 1」
 *        「物流：中通 ZT123456789」
 *        「最新：01-17 09:20 已到达【杭州转运中心】」
 *        「预计送达：01-19」
 *        「需要我帮您催一下快递吗？」
 *
 * 为什么把纯文本生成逻辑放在组件里而不是后端：
 *   后端返回的是结构化事实（FE-01/FE-02 响应），话术模板属于展示层职责；
 *   放在这里可以让坐席在 UI 上直接看到「买家会收到什么」，避免出现
 *   「界面上很漂亮、买家收到的是一堆乱码」这类事故。
 */
interface Props {
  order: OrderInfo
  /** 是否展示发给买家的纯文本预览 */
  showPlainText?: boolean
}

const props = withDefaults(defineProps<Props>(), { showPlainText: true })

const statusLabel = computed(() => ORDER_STATUS_LABELS[props.order.status])

/** 订单状态 -> 语义色 */
const statusTone = computed<'ok' | 'warn' | 'bad' | 'info' | 'neutral'>(() => {
  switch (props.order.status) {
    case 'DELIVERED':
      return 'ok'
    case 'SHIPPED':
      return 'info'
    case 'PAID':
      return 'warn'
    case 'CLOSED':
      return 'neutral'
    case 'CREATED':
      return 'warn'
    default:
      return 'neutral'
  }
})

const companyLabel = computed(() =>
  props.order.company ? COMPANY_LABELS[props.order.company] : '',
)

/**
 * 生成发给买家的纯文本（HC-1 落地形态）。
 * 注意：这里输出的字符串里**不允许出现 http(s):// 或任何可点击内容**，
 * 由 scripts/compliance-check.mjs 扫描校验。
 */
const plainText = computed(() => {
  const order = props.order
  const lines: string[] = [`您的订单 ${order.order_no} 目前状态：${statusLabel.value}`]

  for (const item of order.items) {
    const spec = item.spec ? ` ${item.spec}` : ''
    lines.push(`商品：${item.title}${spec} × ${item.quantity}`)
  }

  if (companyLabel.value && order.tracking_no) {
    lines.push(`物流：${companyLabel.value} ${order.tracking_no}`)
  }

  if (order.latest_trace) {
    const at = order.latest_trace_at ? `${formatTraceTime(order.latest_trace_at)} ` : ''
    lines.push(`最新：${at}${order.latest_trace}`)
  }

  if (order.estimated_delivery) {
    lines.push(`预计送达：${order.estimated_delivery}`)
  }

  lines.push('需要我帮您催一下快递吗？')
  return lines.join('\n')
})

/** 物流时间显示为 MM-DD HH:mm，与任务书模板一致 */
function formatTraceTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="order-card">
    <header class="order-card__head">
      <span class="order-card__no wk-mono">{{ props.order.order_no }}</span>
      <EnumTag kind="tone" :value="statusTone" :label="statusLabel" dot />
      <span v-if="props.order.company" class="order-card__company">
        {{ companyLabel }} {{ props.order.tracking_no }}
      </span>
    </header>

    <ul class="order-card__items">
      <li v-for="(item, index) in props.order.items" :key="index" class="item">
        <img
          v-if="item.image_ref"
          class="item__img"
          :src="item.image_ref"
          :alt="item.title"
          loading="lazy"
        />
        <div class="item__body">
          <span class="item__title">{{ item.title }}</span>
          <span v-if="item.spec" class="item__spec">{{ item.spec }}</span>
          <span class="item__qty">
            × {{ item.quantity }}
            <template v-if="item.price !== undefined">· ¥{{ item.price }}</template>
          </span>
        </div>
      </li>
    </ul>

    <dl class="order-card__facts">
      <div v-if="props.order.latest_trace" class="order-card__fact">
        <dt>最新物流</dt>
        <dd>
          <span v-if="props.order.latest_trace_at" class="wk-mono">
            {{ formatTraceTime(props.order.latest_trace_at) }}
          </span>
          {{ props.order.latest_trace }}
        </dd>
      </div>
      <div v-if="props.order.estimated_delivery" class="order-card__fact">
        <dt>预计送达</dt>
        <dd>{{ props.order.estimated_delivery }}</dd>
      </div>
      <div v-if="props.order.total_amount !== undefined" class="order-card__fact">
        <dt>订单金额</dt>
        <dd>¥{{ props.order.total_amount }}</dd>
      </div>
    </dl>

    <!-- 发给买家的纯文本预览：让坐席看到买家真正会收到什么 -->
    <details v-if="props.showPlainText" class="order-card__plain">
      <summary>发给买家的纯文本（无链接、无卡片）</summary>
      <pre class="order-card__plain-body">{{ plainText }}</pre>
    </details>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.order-card {
  border: 1px solid $ink-150;
  border-radius: $radius-sm;
  background: $surface;
  overflow: hidden;

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
    padding: $space-2 $space-3;
    background: $surface-sunken;
    border-bottom: 1px solid $ink-150;
  }

  &__no {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
  }

  &__company {
    font-size: 11px;
    color: $ink-500;
  }

  &__items {
    margin: 0;
    padding: $space-2 $space-3;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: $space-2;
  }

  &__facts {
    margin: 0;
    padding: 0 $space-3 $space-2;
    display: flex;
    flex-direction: column;
    gap: $space-1;
  }

  &__fact {
    display: flex;
    gap: $space-2;
    font-size: $text-xs;

    dt {
      flex: none;
      width: 56px;
      color: $ink-500;
    }

    dd {
      margin: 0;
      color: $ink-800;
      line-height: 1.6;
    }
  }

  &__plain {
    border-top: 1px dashed $ink-150;
    padding: $space-2 $space-3;

    summary {
      font-size: 11px;
      color: $ink-500;
      cursor: pointer;
    }
  }

  &__plain-body {
    margin: $space-2 0 0;
    padding: $space-2;
    border-radius: $radius-xs;
    background: $surface-sunken;
    font-family: $font-sans;
    font-size: $text-xs;
    line-height: 1.8;
    color: $ink-700;
    white-space: pre-wrap;
    word-break: break-word;
  }
}

.item {
  display: flex;
  gap: $space-2;
  align-items: center;

  &__img {
    flex: none;
    width: 40px;
    height: 40px;
    border-radius: $radius-xs;
    object-fit: cover;
    background: $ink-100;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
  }

  &__title {
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-900;
  }

  &__spec {
    font-size: 11px;
    color: $ink-500;
  }

  &__qty {
    font-size: 11px;
    color: $ink-600;
  }
}
</style>
