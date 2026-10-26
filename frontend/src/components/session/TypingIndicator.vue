<script setup lang="ts">
/**
 * 打字机指示器 —— FE-01。
 *
 * 任务书 SSE 事件映射：
 *   turn.start -> 显示正在输入
 *   action     -> 状态条「正在查询订单…」
 *   retrieval  -> 可选显示「已命中 N 条知识」
 *   intent     -> 可选显示意图标签
 *
 * 设计意图：坐席最怕「点了发送之后什么都没有」。
 * 因此这里不只是三个跳动的点，而是把**机器人正在做什么**逐级暴露出来：
 * 意图识别 → 检索知识 → 调用工具 → 生成回复。
 * 每一步都让等待变得可解释，也顺便让联调时能一眼看出卡在哪一步。
 */
interface Props {
  /** 是否正在输入 */
  active: boolean
  /** 当前阶段文字（来自 action 事件，如「正在查询订单…」） */
  stage?: string
  /** 命中的知识条数（retrieval 事件） */
  retrievalHits?: number | null
  /** 检索最高分 */
  retrievalTopScore?: number | null
  /** 识别到的意图（intent 事件） */
  intent?: string
  /** 意图置信度 */
  confidence?: number | null
}

const props = withDefaults(defineProps<Props>(), {
  stage: '',
  retrievalHits: null,
  retrievalTopScore: null,
  intent: '',
  confidence: null,
})
</script>

<template>
  <div v-if="props.active" class="typing" data-testid="typing-indicator">
    <div class="typing__avatar">🤖</div>
    <div class="typing__body">
      <div class="typing__dots">
        <span class="typing__dot" />
        <span class="typing__dot" />
        <span class="typing__dot" />
      </div>

      <div class="typing__stages">
        <span v-if="props.stage" class="typing__stage">{{ props.stage }}</span>
        <span v-if="props.intent" class="typing__chip">
          意图 {{ props.intent }}
          <template v-if="props.confidence !== null">
            （{{ (props.confidence * 100).toFixed(0) }}%）
          </template>
        </span>
        <span v-if="props.retrievalHits !== null" class="typing__chip">
          已命中 {{ props.retrievalHits }} 条知识
          <template v-if="props.retrievalTopScore !== null">
            · 最高 {{ props.retrievalTopScore.toFixed(2) }}
          </template>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.typing {
  display: flex;
  gap: $space-3;
  padding: $space-2 $space-4;

  &__avatar {
    flex: none;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
    background: $mint-bg;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: $space-1;
    padding: $space-2 $space-3;
    border-radius: $radius-md;
    background: $mint-bg;
    border: 1px solid rgba(62, 142, 126, 0.25);
  }

  &__dots {
    display: flex;
    align-items: center;
    gap: 4px;
    height: 16px;
  }

  &__dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: $mint-500;
    animation: typing-bounce 1.2s infinite ease-in-out;

    &:nth-child(2) {
      animation-delay: 0.15s;
    }

    &:nth-child(3) {
      animation-delay: 0.3s;
    }
  }

  &__stages {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
  }

  &__stage {
    font-size: $text-xs;
    color: $mint-ink;
  }

  &__chip {
    padding: 0 6px;
    border-radius: $radius-xs;
    background: rgba(255, 255, 255, 0.7);
    color: $ink-600;
    font-size: 11px;
  }
}

@keyframes typing-bounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.45;
  }

  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}

/* 尊重系统的「减少动态效果」偏好：关闭跳动，改为静态呼吸 */
@media (prefers-reduced-motion: reduce) {
  .typing__dot {
    animation: none;
    opacity: 0.7;
  }
}
</style>
