<script setup lang="ts">
/**
 * 「这条答错了」按钮 —— FE-08。
 *
 * 任务书：FeedbackButton.vue（消息气泡上）
 * DoD：「标记到待办 ≤3 秒」
 *
 * 设计决策：
 *   1. **只出现在机器人消息上** —— 买家消息与坐席自己的回复没有「答错了」的概念。
 *   2. **已标记后变成不可重复的确认态** —— 后端对同一 message_id 做了幂等，
 *      但 UI 也必须给出「已经标记过了」的明确反馈，否则坐席会反复点，怀疑没生效。
 *   3. **低干扰样式** —— 这是个辅助动作，不能比消息本身还显眼；
 *      默认是淡淡的文字链，hover 才变色。
 */
interface Props {
  /** 是否已标记过 */
  marked?: boolean
  /** 是否禁用（如无纠错提交权限） */
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), { marked: false, disabled: false })

const emit = defineEmits<{
  (e: 'click'): void
}>()

function onClick(): void {
  if (props.marked || props.disabled) return
  emit('click')
}
</script>

<template>
  <button
    type="button"
    class="feedback-button"
    :class="{ 'is-marked': props.marked, 'is-disabled': props.disabled }"
    :disabled="props.marked || props.disabled"
    :title="props.disabled ? '当前身份没有提交纠错的权限' : props.marked ? '已标记，纠错待办已生成' : '标记这条回答有误，进入纠错待办'"
    @click="onClick"
  >
    <template v-if="props.marked">✓ 已标记答错</template>
    <template v-else>⚑ 这条答错了</template>
  </button>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.feedback-button {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border: 1px solid transparent;
  border-radius: $radius-xs;
  background: transparent;
  font-size: 11px;
  color: $ink-400;
  cursor: pointer;
  transition: color 0.15s ease, background 0.15s ease, border-color 0.15s ease;

  &:hover:not(:disabled) {
    color: $danger-ink;
    background: $danger-bg;
    border-color: $danger-500;
  }

  &.is-marked {
    color: $success-ink;
    background: $success-bg;
    cursor: default;
  }

  &.is-disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
}
</style>
