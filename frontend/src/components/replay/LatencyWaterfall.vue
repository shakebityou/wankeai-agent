<script setup lang="ts">
import { computed } from 'vue'
import { formatLatency } from '@/utils/format'
import type { LatencySpan } from '@/types/contract'

/**
 * 耗时瀑布图 —— FE-10。
 *
 * 任务书 DoD：
 *   「瀑布图各跳耗时之和与总耗时误差 ≤5%」
 *   「按 trace_id 一键聚合」
 *
 * 设计决策：不引入 ECharts，用纯 CSS 绝对定位画横条。
 * 理由：瀑布图的本质是「在一根时间轴上摆若干段区间」，数据量是个位数（6~7 跳），
 * ECharts 的 gantt 能力反而不如手写定位精确；而且这里要展示每一跳的**名称与耗时数字**，
 * 图表库的 tooltip 交互对坐席来说是多余的一层。用 CSS 的好处是：
 * 数字永远可见、可以精确对齐、无额外体积。
 *
 * 关键口径：
 *   横轴范围取 max(total_ms, 各跳 offset+duration 的最大值)，
 *   若各跳之和小于总耗时（存在未归因耗时），右侧会留出灰色空档 ——
 *   这个空档是**故意保留**的，它表示「还有时间没被归因」，
 *   而不是把每段按比例拉伸到填满（后者会让耗时数字与条长不成比例，等于骗人）。
 */
interface Props {
  spans: LatencySpan[]
  /** 该轮总耗时（毫秒） */
  totalMs: number
}

const props = withDefaults(defineProps<Props>(), {})

/** 各跳耗时之和 */
const sumMs = computed(() => props.spans.reduce((acc, span) => acc + span.duration_ms, 0))

/**
 * 误差口径：|各跳之和 - 总耗时| / 总耗时。
 * 任务书允许 ≤5%；这里直接算出来展示，让联调时一眼看出后端有没有漏归因。
 */
const deviation = computed(() => {
  if (!props.totalMs) return 0
  return Math.abs(sumMs.value - props.totalMs) / props.totalMs
})

const deviationTone = computed<'ok' | 'warn' | 'bad'>(() => {
  if (deviation.value <= 0.05) return 'ok'
  if (deviation.value <= 0.15) return 'warn'
  return 'bad'
})

/** 横轴上限：取总耗时与所有区间右端点的较大值 */
const axisMax = computed(() => {
  const ends = props.spans.map((span) => span.offset_ms + span.duration_ms)
  return Math.max(props.totalMs, ...ends, 1)
})

/** 每跳的类别色 —— 用色相区分「模型慢」还是「接口慢」 */
const KIND_COLORS: Record<LatencySpan['kind'], string> = {
  asr: '#8b7cd8',
  intent: '#4a7fd9',
  retrieval: '#3e8e7e',
  tool: '#e0952a',
  llm: '#f2653f',
  tts: '#8b7cd8',
  other: '#9aa0ae',
}

const KIND_LABELS: Record<LatencySpan['kind'], string> = {
  asr: '语音',
  intent: '意图',
  retrieval: '检索',
  tool: '接口',
  llm: '模型',
  tts: '合成',
  other: '其它',
}

interface Bar {
  span: LatencySpan
  leftPct: number
  widthPct: number
  color: string
  kindLabel: string
}

const bars = computed<Bar[]>(() =>
  props.spans.map((span) => ({
    span,
    leftPct: (span.offset_ms / axisMax.value) * 100,
    widthPct: Math.max((span.duration_ms / axisMax.value) * 100, 0.6),
    color: KIND_COLORS[span.kind] ?? KIND_COLORS.other,
    kindLabel: KIND_LABELS[span.kind] ?? '其它',
  })),
)

/** 时间轴刻度（0 / 25% / 50% / 75% / 100%） */
const ticks = computed(() =>
  [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    ratio,
    leftPct: ratio * 100,
    label: formatLatency(Math.round(axisMax.value * ratio)),
  })),
)

/** 未归因耗时 */
const unattributedMs = computed(() => Math.max(props.totalMs - sumMs.value, 0))
</script>

<template>
  <div class="waterfall">
    <div class="waterfall__summary">
      <span class="waterfall__total">
        总耗时 <strong>{{ formatLatency(props.totalMs) }}</strong>
      </span>
      <span class="waterfall__sum">
        各跳之和 <strong>{{ formatLatency(sumMs) }}</strong>
      </span>
      <span class="waterfall__dev" :class="`waterfall__dev--${deviationTone}`">
        偏差 {{ (deviation * 100).toFixed(1) }}%
        <template v-if="deviationTone === 'ok'">· 达标（≤5%）</template>
      </span>
      <span v-if="unattributedMs > 0" class="waterfall__unattributed">
        未归因 {{ formatLatency(unattributedMs) }}
      </span>
    </div>

    <div class="waterfall__chart">
      <!-- 刻度 -->
      <div class="waterfall__axis">
        <span
          v-for="tick in ticks"
          :key="tick.ratio"
          class="waterfall__tick"
          :style="{ left: `${tick.leftPct}%` }"
        >
          {{ tick.label }}
        </span>
      </div>

      <ul class="waterfall__rows">
        <li v-for="bar in bars" :key="bar.span.name" class="row">
          <span class="row__name" :title="bar.span.name">{{ bar.span.name }}</span>
          <div class="row__track">
            <div
              class="row__bar"
              :style="{
                left: `${bar.leftPct}%`,
                width: `${bar.widthPct}%`,
                background: bar.color,
              }"
              :title="`${bar.span.name}：${formatLatency(bar.span.duration_ms)}（偏移 ${formatLatency(bar.span.offset_ms)}）`"
            />
            <span class="row__value" :style="{ left: `${Math.min(bar.leftPct + bar.widthPct, 92)}%` }">
              {{ formatLatency(bar.span.duration_ms) }}
            </span>
          </div>
          <span class="row__kind" :style="{ color: bar.color }">{{ bar.kindLabel }}</span>
          <span v-if="bar.span.cached" class="row__cached" title="命中缓存">缓存</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.waterfall {
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-3;
    font-size: $text-xs;
    color: $ink-600;

    strong {
      color: $ink-900;
      font-family: $font-mono;
    }
  }

  &__dev {
    padding: 0 6px;
    border-radius: $radius-xs;
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
  }

  &__unattributed {
    color: $ink-500;
  }

  &__chart {
    position: relative;
  }

  &__axis {
    position: relative;
    height: 14px;
    margin-left: 132px;
    margin-right: 96px;
  }

  &__tick {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    font-size: 10px;
    color: $ink-400;
    font-family: $font-mono;
    white-space: nowrap;
  }

  &__rows {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
}

.row {
  display: flex;
  align-items: center;
  gap: $space-2;

  &__name {
    flex: none;
    width: 124px;
    font-size: $text-xs;
    color: $ink-700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__track {
    position: relative;
    flex: 1;
    height: 18px;
    border-radius: $radius-xs;
    background: $ink-100;
  }

  &__bar {
    position: absolute;
    top: 3px;
    height: 12px;
    border-radius: $radius-xs;
    min-width: 2px;
  }

  &__value {
    position: absolute;
    top: 2px;
    transform: translateX(4px);
    font-size: 10px;
    font-family: $font-mono;
    color: $ink-600;
    white-space: nowrap;
  }

  &__kind {
    flex: none;
    width: 30px;
    font-size: 10px;
    text-align: right;
  }

  &__cached {
    flex: none;
    padding: 0 4px;
    border-radius: $radius-xs;
    background: $success-bg;
    color: $success-ink;
    font-size: 10px;
  }
}
</style>
