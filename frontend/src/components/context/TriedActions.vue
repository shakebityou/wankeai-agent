<script setup lang="ts">
import { formatDateTime } from '@/utils/format'
import type { TriedAction } from '@/types/contract'

/**
 * 已尝试动作 —— FE-02 七要素之一（tried_actions）。
 *
 * 坐席接管后最怕重复劳动：机器人已经查过订单、已经催过物流，
 * 坐席如果不知道，就会再问一遍买家，体验立刻崩塌。
 * 所以这张卡片的作用是「避免重复」，成功与失败都要展示：
 *   失败的动作用警示色标出 —— 那正是买家不满的来源。
 */
interface Props {
  actions: TriedAction[]
}

const props = defineProps<Props>()
</script>

<template>
  <ul class="actions">
    <li
      v-for="(action, index) in props.actions"
      :key="index"
      class="actions__item"
      :class="{ 'is-failed': !action.ok }"
    >
      <span class="actions__mark" :class="action.ok ? 'is-ok' : 'is-bad'">
        {{ action.ok ? '✓' : '✕' }}
      </span>
      <div class="actions__body">
        <span class="actions__name">{{ action.action }}</span>
        <span v-if="action.detail" class="actions__detail">{{ action.detail }}</span>
      </div>
      <span v-if="action.at" class="actions__at">{{ formatDateTime(action.at) }}</span>
    </li>
    <li v-if="props.actions.length === 0" class="actions__empty">
      机器人尚未尝试任何动作（首轮即转人工）
    </li>
  </ul>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.actions {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;

  &__item {
    display: flex;
    align-items: flex-start;
    gap: $space-2;
    padding: $space-2;
    border-radius: $radius-xs;

    &.is-failed {
      background: $danger-bg;
    }
  }

  &__mark {
    flex: none;
    width: 16px;
    height: 16px;
    margin-top: 1px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: 700;
    color: #fff;

    &.is-ok {
      background: $success-500;
    }

    &.is-bad {
      background: $danger-500;
    }
  }

  &__body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__name {
    font-size: $text-sm;
    color: $ink-800;
  }

  &__detail {
    font-size: $text-xs;
    color: $ink-600;
    line-height: 1.5;
    word-break: break-word;
  }

  &__at {
    flex: none;
    font-size: 11px;
    color: $ink-500;
  }

  &__empty {
    padding: $space-2;
    font-size: $text-xs;
    color: $ink-500;
  }
}
</style>
