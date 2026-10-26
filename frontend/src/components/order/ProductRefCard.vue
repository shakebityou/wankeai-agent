<script setup lang="ts">
import { computed } from 'vue'
import { stockStatusText, stockStatusTone, type ProductRef } from '@/types/order'

/**
 * 商品引用卡片 —— FE-14。
 *
 * 任务书：「ProductRefCard.vue」，数据来自 FE-01 / FE-02 响应。
 *
 * 关键约束（FE-14 DoD）：
 *   「库存三种状态全部渲染正确」—— 现货 / 预售 N 天 / 缺货，且**不显示英文枚举**。
 *
 * 为什么库存状态要单独强调「不显示英文枚举」：
 *   商家原始数据是「预售(7天)」这种中文串，接口枚举是 IN_STOCK / PRESALE / OUT_OF_STOCK。
 *   坐席如果看到「PRESALE」，回答买家时还得自己翻译，既慢又容易说错；
 *   而「预售 7 天」是坐席可以直接念给买家的话。这是把数据翻译成话术的最后一厘米。
 *
 * HC-3：库存与价格是「会变的数字」，必须来自接口（verified.product 事实），
 * 本组件只负责渲染，不做任何本地缓存或推断。
 */
interface Props {
  product: ProductRef
  /** 紧凑模式（嵌入消息流时用） */
  compact?: boolean
}

const props = withDefaults(defineProps<Props>(), { compact: false })

const stockText = computed(() => stockStatusText(props.product))
const stockTone = computed(() => stockStatusTone(props.product.stock_status))

/** 是否有价格优惠（用于展示划线原价） */
const hasDiscount = computed(
  () =>
    props.product.price !== undefined &&
    props.product.original_price !== undefined &&
    props.product.price < props.product.original_price,
)
</script>

<template>
  <div class="product-ref" :class="{ 'is-compact': props.compact }">
    <img
      v-if="props.product.image_ref"
      class="product-ref__img"
      :src="props.product.image_ref"
      :alt="props.product.title"
      loading="lazy"
    />

    <div class="product-ref__body">
      <div class="product-ref__head">
        <span class="product-ref__title">{{ props.product.title }}</span>
        <span class="product-ref__stock" :class="`product-ref__stock--${stockTone}`">
          {{ stockText }}
        </span>
      </div>

      <div class="product-ref__meta">
        <span class="wk-mono product-ref__sku">{{ props.product.sku_id }}</span>
        <span v-if="props.product.spec">{{ props.product.spec }}</span>
        <span v-if="props.product.material">{{ props.product.material }}</span>
      </div>

      <div v-if="props.product.price !== undefined" class="product-ref__price">
        <span class="product-ref__price-now">¥{{ props.product.price }}</span>
        <span v-if="hasDiscount" class="product-ref__price-was">
          ¥{{ props.product.original_price }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.product-ref {
  display: flex;
  gap: $space-3;
  padding: $space-2 $space-3;
  border: 1px solid $ink-150;
  border-radius: $radius-sm;
  background: $surface;

  &.is-compact {
    gap: $space-2;
    padding: $space-1 $space-2;
  }

  &__img {
    flex: none;
    width: 56px;
    height: 56px;
    border-radius: $radius-xs;
    object-fit: cover;
    background: $ink-100;
  }

  &.is-compact &__img {
    width: 40px;
    height: 40px;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  &__head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: $space-2;
  }

  &__title {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__stock {
    flex: none;
    padding: 0 6px;
    border-radius: $radius-pill;
    font-size: 11px;
    font-weight: 600;

    &--ok {
      background: $success-bg;
      color: $success-ink;
    }

    &--warn {
      background: $warning-bg;
      color: $warning-ink;
    }

    &--bad {
      background: $danger-bg;
      color: $danger-ink;
    }

    &--neutral {
      background: $ink-100;
      color: $ink-600;
    }
  }

  &__meta {
    display: flex;
    flex-wrap: wrap;
    gap: $space-2;
    font-size: 11px;
    color: $ink-500;
  }

  &__sku {
    color: $ink-400;
  }

  &__price {
    display: flex;
    align-items: baseline;
    gap: $space-1;
  }

  &__price-now {
    font-size: $text-sm;
    font-weight: 700;
    color: $danger-ink;
  }

  &__price-was {
    font-size: 11px;
    color: $ink-400;
    text-decoration: line-through;
  }
}
</style>
