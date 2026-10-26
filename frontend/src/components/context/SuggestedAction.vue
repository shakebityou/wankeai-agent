<script setup lang="ts">
/**
 * 建议动作 —— FE-02 七要素之一（suggested_action）。
 *
 * 与「未满足诉求」形成闭环：一个说明问题是什么，一个说明该怎么解。
 * 视觉上用品牌色（薄荷绿，代表「机器人/知识」侧）而非警示色，
 * 与未满足诉求的红色形成对比 —— 一个报警，一个给方案。
 *
 * 注意 HC-1：这里只展示建议文案，不生成任何跳转链接或操作按钮的外链。
 */
interface Props {
  action: string
}

const props = defineProps<Props>()
</script>

<template>
  <div class="suggest">
    <div class="suggest__head">
      <span class="suggest__glyph">💡</span>
      <span class="suggest__label">建议动作</span>
    </div>
    <p class="suggest__text">{{ props.action }}</p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.suggest {
  padding: $space-3;
  border-radius: $radius-sm;
  background: $mint-bg;
  border-left: 4px solid $mint-500;

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-bottom: $space-2;
  }

  &__glyph {
    font-size: $text-sm;
  }

  &__label {
    font-size: $text-xs;
    font-weight: 600;
    color: $mint-ink;
  }

  &__text {
    margin: 0;
    font-size: $text-md;
    font-weight: 500;
    line-height: 1.6;
    color: $ink-900;
  }
}
</style>
