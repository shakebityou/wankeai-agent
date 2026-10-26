<script setup lang="ts">
import { computed } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import LatencyWaterfall from './LatencyWaterfall.vue'
import { formatDateTime, formatLatency } from '@/utils/format'
import {
  DECISION_LABELS,
  HANDOFF_REASON_LABELS,
  SENDER_LABELS,
  type ReplayTurn,
} from '@/types/contract'

/**
 * 单轮详情 —— FE-10。
 *
 * 任务书：「TurnDetail.vue（单轮展开：意图/检索命中/工具调用/耗时）」
 *
 * 设计意图 —— 这一页是给「AI 为什么这么答」做归因用的，所以信息要**可追溯**：
 *   意图 + 置信度  -> 是分类器判错了，还是分类对了但答非所问？
 *   检索命中       -> 是没检索到，还是检索到了没用？
 *   工具调用       -> 是商家接口挂了，还是模型没用上工具结果？
 *   耗时瀑布       -> 慢在模型还是慢在接口？
 * 这四组信息正好对应四个排查方向，缺任何一组，坐席/运营就无法给出结论。
 *
 * 合规（4.5.2）：
 *   「不导出：会话回放、工单详情无导出功能」
 *   因此本组件不提供复制 JSON / 下载等任何出口。trace_id 只是展示标识。
 */
interface Props {
  turn: ReplayTurn
  /** 默认是否展开 */
  defaultOpen?: boolean
}

const props = withDefaults(defineProps<Props>(), { defaultOpen: false })

const decisionLabel = computed(() => DECISION_LABELS[props.turn.decision])

const decisionTone = computed<'ok' | 'warn' | 'bad'>(() => {
  switch (props.turn.decision) {
    case 'DIRECT_ANSWER':
      return 'ok'
    case 'WEAK':
      return 'warn'
    case 'NO_EVIDENCE':
      return 'bad'
    default:
      return 'warn'
  }
})

/** 工具调用是否全部成功 —— 失败是最高优先级的排查信号 */
const toolFailed = computed(() => props.turn.tool_calls.some((call) => !call.ok))
</script>

<template>
  <details class="turn" :open="props.defaultOpen">
    <summary class="turn__summary">
      <span class="turn__index">第 {{ props.turn.turn_index }} 轮</span>
      <span class="turn__time wk-mono">{{ formatDateTime(props.turn.started_at) }}</span>
      <span class="turn__intent">
        {{ props.turn.intent }}
        <span class="turn__conf">{{ (props.turn.intent_confidence * 100).toFixed(0) }}%</span>
      </span>
      <EnumTag kind="tone" :value="decisionTone" :label="decisionLabel" />
      <span v-if="props.turn.handoff" class="wk-pill wk-pill--bad">
        转人工 · {{ HANDOFF_REASON_LABELS[props.turn.handoff.reason] }}
      </span>
      <span v-if="toolFailed" class="wk-pill wk-pill--warn">接口调用异常</span>
      <span class="wk-spacer" />
      <span class="turn__latency wk-mono">{{ formatLatency(props.turn.total_ms) }}</span>
    </summary>

    <div class="turn__body">
      <!-- 对话内容 -->
      <div class="turn__dialog">
        <div class="dialog-row">
          <span class="dialog-row__who">{{ SENDER_LABELS.USER }}</span>
          <p class="dialog-row__text">{{ props.turn.user_input }}</p>
        </div>
        <div class="dialog-row dialog-row--reply">
          <span class="dialog-row__who">{{ SENDER_LABELS[props.turn.sender] }}</span>
          <p class="dialog-row__text">{{ props.turn.reply }}</p>
        </div>
      </div>

      <div class="turn__grid">
        <!-- 检索命中 -->
        <section class="block">
          <h4 class="block__title">
            检索命中
            <span class="block__count">{{ props.turn.retrieval.hits.length }} 条</span>
          </h4>
          <p class="block__query">检索词：{{ props.turn.retrieval.query }}</p>
          <ul v-if="props.turn.retrieval.hits.length" class="hits">
            <li v-for="hit in props.turn.retrieval.hits" :key="hit.chunk_id" class="hits__item">
              <span class="hits__score" :class="hit.score >= 0.7 ? 'is-ok' : hit.score >= 0.5 ? 'is-warn' : 'is-bad'">
                {{ hit.score.toFixed(2) }}
              </span>
              <span class="hits__title">{{ hit.title }}</span>
              <span class="hits__id wk-mono">{{ hit.chunk_id }}</span>
            </li>
          </ul>
          <p v-else class="block__none">本轮未命中任何知识条目</p>
        </section>

        <!-- 工具调用 -->
        <section class="block">
          <h4 class="block__title">
            工具调用
            <span class="block__count">{{ props.turn.tool_calls.length }} 次</span>
          </h4>
          <ul v-if="props.turn.tool_calls.length" class="tools">
            <li v-for="(call, index) in props.turn.tool_calls" :key="index" class="tools__item">
              <span class="tools__flag" :class="call.ok ? 'is-ok' : 'is-bad'">
                {{ call.ok ? '✓' : '✕' }}
              </span>
              <span class="tools__name wk-mono">{{ call.tool }}</span>
              <span class="tools__params">
                <template v-for="(value, key) in call.params" :key="key">
                  {{ key }}={{ value }}
                </template>
              </span>
              <span class="tools__dur wk-mono">{{ formatLatency(call.duration_ms) }}</span>
              <span v-if="call.error" class="tools__error">{{ call.error }}</span>
            </li>
          </ul>
          <p v-else class="block__none">本轮未调用任何工具（纯知识问答）</p>
        </section>
      </div>

      <!-- 耗时瀑布 -->
      <section class="block block--full">
        <h4 class="block__title">耗时瀑布</h4>
        <LatencyWaterfall :spans="props.turn.spans" :total-ms="props.turn.total_ms" />
      </section>

      <p class="turn__trace">
        trace_id <span class="wk-mono">{{ props.turn.trace_id }}</span>
      </p>
    </div>
  </details>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.turn {
  border: 1px solid $ink-150;
  border-radius: $radius-sm;
  background: $surface;
  overflow: hidden;

  &[open] {
    border-color: $brand-200;
  }

  &__summary {
    display: flex;
    align-items: center;
    gap: $space-2;
    padding: $space-2 $space-3;
    cursor: pointer;
    background: $surface-sunken;
    list-style: none;

    &::-webkit-details-marker {
      display: none;
    }

    &:hover {
      background: $ink-50;
    }
  }

  &__index {
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-800;
  }

  &__time {
    font-size: 11px;
    color: $ink-500;
  }

  &__intent {
    font-size: $text-xs;
    color: $ink-700;
  }

  &__conf {
    font-family: $font-mono;
    color: $ink-500;
  }

  &__latency {
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-800;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: $space-3;
    padding: $space-3;
    border-top: 1px solid $ink-150;
  }

  &__dialog {
    display: flex;
    flex-direction: column;
    gap: $space-2;
  }

  &__grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: $space-3;

    @media (max-width: 1100px) {
      grid-template-columns: 1fr;
    }
  }

  &__trace {
    margin: 0;
    font-size: 10px;
    color: $ink-400;
  }
}

.dialog-row {
  display: flex;
  gap: $space-2;

  &__who {
    flex: none;
    width: 56px;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-500;
  }

  &__text {
    margin: 0;
    font-size: $text-sm;
    line-height: 1.7;
    color: $ink-800;
  }

  &--reply &__text {
    color: $ink-900;
  }
}

.block {
  display: flex;
  flex-direction: column;
  gap: $space-1;

  &--full {
    grid-column: 1 / -1;
  }

  &__title {
    margin: 0;
    display: flex;
    align-items: center;
    gap: $space-1;
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-700;
  }

  &__count {
    padding: 0 5px;
    border-radius: $radius-xs;
    background: $ink-100;
    color: $ink-600;
    font-size: 10px;
    font-weight: 500;
  }

  &__query {
    margin: 0;
    font-size: 11px;
    color: $ink-500;
  }

  &__none {
    margin: 0;
    font-size: 11px;
    color: $ink-400;
  }
}

.hits {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 3px;

  &__item {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
  }

  &__score {
    flex: none;
    padding: 0 5px;
    border-radius: $radius-xs;
    font-family: $font-mono;
    font-weight: 700;

    &.is-ok {
      background: $success-bg;
      color: $success-ink;
    }

    &.is-warn {
      background: $warning-bg;
      color: $warning-ink;
    }

    &.is-bad {
      background: $danger-bg;
      color: $danger-ink;
    }
  }

  &__title {
    color: $ink-800;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__id {
    flex: none;
    margin-left: auto;
    font-size: 10px;
    color: $ink-400;
  }
}

.tools {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 3px;

  &__item {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
  }

  &__flag {
    flex: none;
    width: 14px;
    height: 14px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    font-size: 10px;
    color: #fff;

    &.is-ok {
      background: $success-500;
    }

    &.is-bad {
      background: $danger-500;
    }
  }

  &__name {
    color: $ink-800;
  }

  &__params {
    color: $ink-500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__dur {
    margin-left: auto;
    flex: none;
    color: $ink-500;
  }

  &__error {
    flex: none;
    color: $danger-ink;
  }
}
</style>
