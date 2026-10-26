<script setup lang="ts">
import { formatDateTime } from '@/utils/format'
import type { VerifiedFact } from '@/types/contract'

/**
 * 已验证事实 —— FE-02 七要素之一（verified）。
 *
 * 设计要点（对应 HC-3「会变的数字永远走接口」）：
 *   每条事实都带 source（取数来源）与 fetched_at（取数时间），
 *   坐席必须能判断「这个库存/物流状态是几秒前的，还是缓存里的」。
 *   因此时间与来源不折叠、不省略，是这张卡片的可信度来源。
 */
interface Props {
  facts: VerifiedFact[]
}

const props = defineProps<Props>()

const TYPE_META: Record<VerifiedFact['type'], { glyph: string; label: string }> = {
  order: { glyph: '🧾', label: '订单' },
  logistics: { glyph: '🚚', label: '物流' },
  product: { glyph: '🧸', label: '商品' },
  policy: { glyph: '📜', label: '政策' },
}
</script>

<template>
  <ul class="facts">
    <li v-for="(fact, index) in props.facts" :key="index" class="facts__item">
      <div class="facts__head">
        <span class="facts__glyph">{{ TYPE_META[fact.type]?.glyph ?? '•' }}</span>
        <span class="facts__type">{{ TYPE_META[fact.type]?.label ?? fact.type }}</span>
        <span class="facts__label">{{ fact.label }}</span>
        <span v-if="fact.stalled_days" class="facts__stalled">停滞 {{ fact.stalled_days }} 天</span>
      </div>
      <p class="facts__value">{{ fact.value }}</p>
      <div class="facts__meta">
        <span v-if="fact.source" class="wk-mono">来源：{{ fact.source }}</span>
        <span v-if="fact.fetched_at" class="wk-mono">{{ formatDateTime(fact.fetched_at) }}</span>
      </div>
    </li>
  </ul>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.facts {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__item {
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;
    border: 1px solid $ink-150;
  }

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
  }

  &__glyph {
    font-size: $text-sm;
  }

  &__type {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__label {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-800;
  }

  &__stalled {
    margin-left: auto;
    padding: 1px 6px;
    border-radius: $radius-xs;
    background: $warning-bg;
    color: $warning-ink;
    font-size: 11px;
    font-weight: 600;
  }

  &__value {
    margin: $space-1 0 0;
    font-size: $text-base;
    line-height: 1.55;
    color: $ink-900;
  }

  &__meta {
    display: flex;
    justify-content: space-between;
    gap: $space-2;
    margin-top: $space-1;
    font-size: 11px;
    color: $ink-500;
  }
}
</style>
