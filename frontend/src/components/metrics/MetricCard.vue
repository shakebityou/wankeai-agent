<script setup lang="ts">
import { computed } from 'vue'
import { formatMetricValue } from '@/utils/format'
import type { MetricDefinition } from '@/types/contract'

/**
 * 指标卡 —— FE-09（P10 指标看板）。
 *
 * 任务书 DoD：「每卡显示三态绿/黄/红」「未达标指标置顶」
 *          「等于目标值算达标」「不展示任何含个人信息的明细数据（只展示聚合值）」
 *
 * 设计决策：
 *   1. **三态不只靠颜色** —— 同时给图标与文字（达标/预警/未达标）。
 *      色盲用户、灰度打印、投屏偏色都可能让红绿不可辨，指标看板是给主管看结论的，
 *      结论必须在不依赖颜色的情况下也能读出来。
 *   2. **目标值必须与当前值同屏** —— 只显示「94.2%」无法判断好坏，
 *      必须紧邻显示「目标 ≥93%」与达标状态。
 *   3. **方向差异要显式** —— 准确率是越高越好（≥），转人工率是越低越好（≤）。
 *      用同一个「达标」文案但不同的符号，避免主管把「转人工率 13.3%」误读为超标。
 *   4. **基线与趋势** —— 商家历史准确率 61.2%，当前 96.1%，这个对比是最有说服力的信息，
 *      不能藏进详情里。
 */
interface Props {
  metric: MetricDefinition
}

const props = defineProps<Props>()

const statusMeta = computed(() => {
  switch (props.metric.status) {
    case 'OK':
      return { tone: 'ok', label: '达标', glyph: '✓' }
    case 'WARNING':
      return { tone: 'warn', label: '预警', glyph: '!' }
    default:
      return { tone: 'bad', label: '未达标', glyph: '✕' }
  }
})

/** 目标值展示：方向不同，前缀不同 */
const targetText = computed(() => {
  const symbol = props.metric.direction === 'GTE' ? '≥' : '≤'
  return `${symbol} ${formatMetricValue(props.metric.target, props.metric.unit)}`
})

const currentText = computed(() => formatMetricValue(props.metric.current, props.metric.unit))

/** 与目标值的差距（仅在未达标时有意义） */
const gapText = computed(() => {
  const { current, target, unit, direction } = props.metric
  const diff = Math.abs(current - target)
  if (props.metric.status === 'OK') return ''
  const symbol = direction === 'GTE' ? '还差' : '还高'
  return `${symbol} ${formatMetricValue(diff, unit)}`
})

const baselineText = computed(() => {
  if (props.metric.baseline === null) return ''
  return `商家历史基线 ${formatMetricValue(props.metric.baseline, props.metric.unit)}`
})

/** 达标进度条宽度：GTE 用 current/target，LTE 用 target/current，都截断到 100% */
const progressPct = computed(() => {
  const { current, target, direction } = props.metric
  if (!current || !target) return 0
  const ratio = direction === 'GTE' ? current / target : target / current
  return Math.min(Math.round(ratio * 100), 100)
})
</script>

<template>
  <article class="metric" :class="`metric--${statusMeta.tone}`">
    <header class="metric__head">
      <h3 class="metric__name" :title="props.metric.definition">{{ props.metric.name }}</h3>
      <span class="metric__status">
        <span class="metric__glyph">{{ statusMeta.glyph }}</span>
        {{ statusMeta.label }}
      </span>
    </header>

    <div class="metric__body">
      <span class="metric__current">{{ currentText }}</span>
      <span class="metric__target">{{ targetText }}</span>
    </div>

    <div class="metric__bar">
      <div class="metric__fill" :style="{ width: `${progressPct}%` }" />
    </div>

    <footer class="metric__foot">
      <span v-if="gapText" class="metric__gap">{{ gapText }}</span>
      <span v-if="baselineText" class="metric__baseline">{{ baselineText }}</span>
      <span class="metric__measured">统计至 {{ props.metric.measured_at.slice(5, 16).replace('T', ' ') }}</span>
    </footer>

    <p class="metric__definition">{{ props.metric.definition }}</p>
  </article>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.metric {
  display: flex;
  flex-direction: column;
  gap: $space-2;
  padding: $space-3;
  border: 1px solid $ink-150;
  border-left: 4px solid $ink-300;
  border-radius: $radius-sm;
  background: $surface;

  &--ok {
    border-left-color: $success-500;

    .metric__status {
      background: $success-bg;
      color: $success-ink;
    }

    .metric__fill {
      background: $success-500;
    }
  }

  &--warn {
    border-left-color: $warning-500;
    background: linear-gradient(0deg, $warning-bg 0%, $surface 60%);

    .metric__status {
      background: $warning-bg;
      color: $warning-ink;
    }

    .metric__fill {
      background: $warning-500;
    }
  }

  &--bad {
    border-left-color: $danger-500;
    background: linear-gradient(0deg, $danger-bg 0%, $surface 60%);

    .metric__status {
      background: $danger-bg;
      color: $danger-ink;
    }

    .metric__fill {
      background: $danger-500;
    }
  }

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: $space-2;
  }

  &__name {
    margin: 0;
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
    line-height: 1.4;
  }

  &__status {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 1px 6px;
    border-radius: $radius-xs;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
  }

  &__glyph {
    font-size: 10px;
  }

  &__body {
    display: flex;
    align-items: baseline;
    gap: $space-2;
  }

  &__current {
    font-size: $text-2xl;
    font-weight: 700;
    font-family: $font-mono;
    color: $ink-900;
    line-height: 1.1;
  }

  &__target {
    font-size: $text-xs;
    color: $ink-500;
    font-family: $font-mono;
  }

  &__bar {
    height: 5px;
    border-radius: $radius-pill;
    background: $ink-100;
    overflow: hidden;
  }

  &__fill {
    height: 100%;
    border-radius: $radius-pill;
    transition: width 0.3s ease;
  }

  &__foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
    font-size: 11px;
    color: $ink-500;
  }

  &__gap {
    font-weight: 600;
    color: $danger-ink;
  }

  &__measured {
    margin-left: auto;
    font-family: $font-mono;
    color: $ink-400;
  }

  &__definition {
    margin: 0;
    font-size: 11px;
    line-height: 1.6;
    color: $ink-400;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
}
</style>
