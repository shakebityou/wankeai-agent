<script setup lang="ts">
/**
 * 未满足诉求 —— FE-02 七要素里最重要的一条。
 *
 * 任务书明确要求「unmet_demand 用警示色高亮」。
 * 理由：坐席接管后第一句话应该直击买家真正没被满足的点，
 * 这个字段如果和别的字段一样是灰色小字，接管效率就无从谈起。
 *
 * 因此这里用整块警示底 + 左侧粗色条 + 稍大的字号，视觉权重仅次于页面标题。
 */
interface Props {
  /** 未满足诉求原文 */
  demand: string
}

const props = defineProps<Props>()
</script>

<template>
  <div class="unmet">
    <div class="unmet__head">
      <span class="unmet__badge">核心</span>
      <span class="unmet__label">未满足诉求</span>
    </div>
    <p class="unmet__text">{{ props.demand }}</p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.unmet {
  padding: $space-3;
  border-radius: $radius-sm;
  background: $danger-bg;
  border-left: 4px solid $danger-500;

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-bottom: $space-2;
  }

  &__badge {
    padding: 1px 6px;
    border-radius: $radius-xs;
    background: $danger-500;
    color: #fff;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  &__label {
    font-size: $text-xs;
    font-weight: 600;
    color: $danger-ink;
  }

  &__text {
    margin: 0;
    font-size: $text-md;
    font-weight: 600;
    line-height: 1.6;
    color: $ink-900;
  }
}
</style>
