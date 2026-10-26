<script setup lang="ts">
import { ref } from 'vue'

/**
 * 区块卡片 —— 后台页与侧栏的通用容器。
 *
 * 提供可选的标题行（含右侧插槽）与折叠能力：
 * 坐席台侧栏（AI 上下文 + 知识检索）纵向空间紧张，折叠是刚需。
 */
interface Props {
  title?: string
  /** 右上角说明文字 */
  hint?: string
  /** 是否可折叠 */
  collapsible?: boolean
  /** 初始是否展开 */
  defaultOpen?: boolean
  /** 紧凑内边距（侧栏用） */
  dense?: boolean
  /** 无内边距（表格类内容自己控制） */
  flush?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  hint: '',
  collapsible: false,
  defaultOpen: true,
  dense: false,
  flush: false,
})

const open = ref(props.defaultOpen)

function toggle(): void {
  if (props.collapsible) open.value = !open.value
}
</script>

<template>
  <section class="section-card wk-card">
    <header
      v-if="props.title || $slots.title || $slots.extra"
      class="section-card__head wk-card__header"
      :class="{ 'is-clickable': props.collapsible }"
      @click="toggle"
    >
      <div class="wk-card__title">
        <span v-if="props.collapsible" class="section-card__caret" :class="{ 'is-open': open }">
          ▸
        </span>
        <slot name="title">{{ props.title }}</slot>
        <span v-if="props.hint" class="section-card__hint">{{ props.hint }}</span>
      </div>
      <div class="section-card__extra" @click.stop>
        <slot name="extra" />
      </div>
    </header>

    <div
      v-show="open"
      class="section-card__body"
      :class="{ 'is-dense': props.dense, 'is-flush': props.flush }"
    >
      <slot />
    </div>
  </section>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.section-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;

  &__head.is-clickable {
    cursor: pointer;
    user-select: none;

    &:hover {
      background: $surface-sunken;
    }
  }

  &__caret {
    display: inline-block;
    color: $ink-400;
    font-size: 11px;
    transition: transform 0.15s ease;

    &.is-open {
      transform: rotate(90deg);
    }
  }

  &__hint {
    margin-left: $space-1;
    font-size: $text-xs;
    font-weight: 400;
    color: $ink-500;
  }

  &__extra {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__body {
    flex: 1;
    min-height: 0;
    padding: $space-4;
    overflow-y: auto;

    &.is-dense {
      padding: $space-3;
    }

    &.is-flush {
      padding: 0;
    }
  }
}
</style>
