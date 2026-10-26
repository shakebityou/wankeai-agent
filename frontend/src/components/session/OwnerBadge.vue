<script setup lang="ts">
import { computed } from 'vue'
import type { SessionOwner } from '@/types/contract'

/**
 * 会话归属标识 —— FE-03。
 *
 * 任务书：「OwnerBadge.vue（归属 BOT/AGENT+坐席名）」。
 *
 * 设计意图：坐席扫一眼就必须知道「现在是谁在回这个买家」。
 * 这个信息如果不显眼，就会出现「机器人还在自动回复，坐席也在回」的双回复事故。
 * 所以：
 *   - 机器人持有：薄荷色 + 「机器人自动回复中」；
 *   - 人工持有：品牌橙 + 「坐席 XXX 接待中」+ 机器人已静音标记。
 * 另外 bot_muted 单独展示，因为它是「机器人是否已停止」的直接证据，
 * 与 owner 是两个字段（理论上可以 owner=AGENT 但 bot_muted 尚未生效）。
 */
interface Props {
  owner: SessionOwner
  /** 接管坐席名，owner=BOT 时为 null */
  agentName?: string | null
  /** 机器人是否已静音 */
  botMuted: boolean
  /** 当前登录坐席 ID，用于区分「我接管的」与「别人接管的」 */
  currentAgentId?: string
  /** 会话归属坐席 ID */
  ownerAgentId?: string | null
  /** 紧凑模式（列表内使用） */
  compact?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  agentName: null,
  currentAgentId: '',
  ownerAgentId: null,
  compact: false,
})

const isAgent = computed(() => props.owner === 'AGENT')
const isMine = computed(
  () => isAgent.value && !!props.currentAgentId && props.ownerAgentId === props.currentAgentId,
)

const label = computed(() => {
  if (!isAgent.value) return '机器人自动回复中'
  if (props.agentName) return `坐席 ${props.agentName} 接待中`
  return '人工接待中'
})
</script>

<template>
  <span
    class="owner-badge"
    :class="{ 'is-agent': isAgent, 'is-bot': !isAgent, 'is-compact': props.compact }"
    :title="isAgent ? '该会话已由人工接管，机器人已停止自动回复' : '该会话由机器人自动回复'"
  >
    <span class="owner-badge__glyph">{{ isAgent ? '🎧' : '🤖' }}</span>
    <span class="owner-badge__label">{{ label }}</span>
    <span v-if="isMine" class="owner-badge__mine">我</span>
    <span v-if="isAgent && props.botMuted" class="owner-badge__muted" title="机器人已静音">
      已静音
    </span>
  </span>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.owner-badge {
  display: inline-flex;
  align-items: center;
  gap: $space-1;
  padding: 2px 8px;
  border-radius: $radius-pill;
  font-size: $text-xs;
  font-weight: 600;
  white-space: nowrap;

  &.is-bot {
    background: $mint-bg;
    color: $mint-ink;
  }

  &.is-agent {
    background: $brand-50;
    color: $brand-ink;
  }

  &.is-compact {
    padding: 1px 6px;
    font-size: 11px;
  }

  &__glyph {
    font-size: 11px;
  }

  &__mine {
    padding: 0 4px;
    border-radius: $radius-xs;
    background: $brand-500;
    color: #fff;
    font-size: 10px;
  }

  &__muted {
    padding: 0 4px;
    border-radius: $radius-xs;
    background: rgba(0, 0, 0, 0.06);
    font-size: 10px;
    font-weight: 500;
  }
}
</style>
