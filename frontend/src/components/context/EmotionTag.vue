<script setup lang="ts">
import { EMOTION_LABELS, type Emotion, type EmotionIntensity } from '@/types/contract'

/**
 * 情绪标签 —— FE-02 七要素之一（emotion）。
 *
 * 设计意图：坐席扫一眼侧栏就要知道「这个买家现在有多生气」。
 * 所以情绪用「等级色 + 强度描边」双通道表达，而不是只写一行小字：
 *   中性 -> 灰、不满 -> 橙、愤怒 -> 红，强度用左侧色条的深浅体现。
 */
interface Props {
  level: Emotion
  intensity: EmotionIntensity
  /** 触发原因（后端给的 trigger），有则展示 */
  trigger?: string
}

const props = withDefaults(defineProps<Props>(), { trigger: '' })

const TONE: Record<Emotion, 'neutral' | 'warn' | 'bad'> = {
  NEUTRAL: 'neutral',
  DISSATISFIED: 'warn',
  ANGRY: 'bad',
}

/** 强度决定色条宽度：轻度 33% / 中度 66% / 强烈 100% */
const INTENSITY_WIDTH: Record<EmotionIntensity, string> = {
  轻度: '33%',
  中度: '66%',
  强烈: '100%',
}
</script>

<template>
  <div class="emotion" :class="`emotion--${TONE[props.level]}`">
    <div class="emotion__head">
      <span class="emotion__label">情绪</span>
      <span class="emotion__level">{{ EMOTION_LABELS[props.level] }}</span>
      <span class="emotion__intensity">{{ props.intensity }}</span>
    </div>
    <div class="emotion__bar">
      <div class="emotion__bar-fill" :style="{ width: INTENSITY_WIDTH[props.intensity] }" />
    </div>
    <p v-if="props.trigger" class="emotion__trigger">{{ props.trigger }}</p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.emotion {
  padding: $space-2 $space-3;
  border-radius: $radius-sm;
  background: $surface-sunken;
  border-left: 3px solid $ink-300;

  &--warn {
    background: $warning-bg;
    border-left-color: $warning-500;
  }

  &--bad {
    background: $danger-bg;
    border-left-color: $danger-500;
  }

  &__head {
    display: flex;
    align-items: baseline;
    gap: $space-2;
  }

  &__label {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__level {
    font-size: $text-md;
    font-weight: 600;
    color: $ink-900;
  }

  &__intensity {
    margin-left: auto;
    font-size: $text-xs;
    color: $ink-600;
  }

  &__bar {
    margin-top: $space-2;
    height: 4px;
    border-radius: $radius-pill;
    background: rgba(0, 0, 0, 0.08);
    overflow: hidden;
  }

  &__bar-fill {
    height: 100%;
    border-radius: $radius-pill;
    background: $ink-400;
    transition: width 0.2s ease;
  }

  &--warn &__bar-fill {
    background: $warning-500;
  }

  &--bad &__bar-fill {
    background: $danger-500;
  }

  &__trigger {
    margin: $space-2 0 0;
    font-size: $text-xs;
    color: $ink-600;
    line-height: 1.5;
  }
}
</style>
