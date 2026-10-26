<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import MessageBubble from './MessageBubble.vue'
import TypingIndicator from './TypingIndicator.vue'
import PageState from '@/components/common/PageState.vue'
import type { ChatMessage } from '@/types/contract'

/**
 * 消息流 —— FE-01 核心组件（P2 会话详情中栏）。
 *
 * 任务书 DoD：
 *   「500 条消息滚动 ≥50fps」
 *   「自动滚底但用户上滚时暂停」
 *   「SSE 事件渲染：turn.start→正在输入、delta→打字机、action→状态条、
 *     turn.end→落库、handoff→转人工卡片、error→兜底话术+重试」
 *
 * 三个关键设计决策：
 *
 * 1. 「用户上滚时暂停自动滚底」用阈值判定而不是精确的 scrollTop===0。
 *    原因：浏览器缩放、滚动惯性、内容高度取整都会让「是否到底」出现 1-2px 误差，
 *    用严格相等会导致偶尔不自动滚动，坐席会以为消息卡住了。
 *    这里用 48px 容差，符合「接近底部就算在底部」的人类直觉。
 *
 * 2. 性能靠 content-visibility: auto 而不是引入虚拟列表库。
 *    理由：消息气泡高度差异极大（一行文本 vs 转人工卡片 vs 240px 图片），
 *    虚拟列表需要预先测量/估算高度，反而更脆弱；content-visibility 让浏览器
 *    跳过视口外元素的渲染，对 500 条规模足够，且不引入新依赖。
 *
 * 3. 打字机只累积「本轮 streaming 文本」，不直接往 messages 里塞假消息。
 *    这样 turn.end 到来时只需把完整 reply 落库一次，delta 期间的内容不会
 *    产生重复消息，也天然满足「落库以 turn.end 的完整 reply 为准」。
 */
const props = withDefaults(
  defineProps<{
    messages: ChatMessage[]
    /** 是否正在加载历史消息 */
    loading?: boolean
    /** 加载失败信息 */
    error?: string | null
    /** 机器人是否正在输入（turn.start 后为 true） */
    typing?: boolean
    /** 当前阶段文字（action 事件） */
    stage?: string
    /** 已命中知识条数（retrieval 事件） */
    retrievalHits?: number | null
    retrievalTopScore?: number | null
    /** 识别到的意图（intent 事件） */
    intent?: string
    confidence?: number | null
    /** 正在流式输出的文本（delta 累积） */
    streamingText?: string
    /** 流式过程中出错时的兜底提示（error 事件） */
    streamError?: string | null
    /** 是否启用「这条答错了」按钮（FE-08） */
    feedbackEnabled?: boolean
  }>(),
  {
    loading: false,
    error: null,
    typing: false,
    stage: '',
    retrievalHits: null,
    retrievalTopScore: null,
    intent: '',
    confidence: null,
    streamingText: '',
    streamError: null,
    feedbackEnabled: false,
  },
)

const emit = defineEmits<{
  feedback: [message: ChatMessage]
  retryStream: []
  /** 自动滚底状态变化，供父级「回到底部」按钮使用 */
  autoScrollChange: [auto: boolean]
}>()

const scroller = ref<HTMLElement | null>(null)
/** 是否处于「自动滚底」状态；用户上滚后置 false */
const autoScroll = ref(true)
/** 距离底部多少像素内仍视为「在底部」 */
const BOTTOM_TOLERANCE_PX = 48

function distanceFromBottom(): number {
  const el = scroller.value
  if (!el) return 0
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

function scrollToBottom(behavior: ScrollBehavior = 'auto'): void {
  const el = scroller.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior })
}

function onScroll(): void {
  const near = distanceFromBottom() <= BOTTOM_TOLERANCE_PX
  if (near !== autoScroll.value) {
    autoScroll.value = near
    emit('autoScrollChange', near)
  }
}

/** 新消息 / 流式文本变化时，仅在自动滚底状态下跟随 */
function followBottom(): void {
  if (!autoScroll.value) return
  void nextTick(() => scrollToBottom())
}

watch(() => props.messages.length, followBottom)
watch(() => props.streamingText, followBottom)
watch(() => props.typing, followBottom)

/** 手动回到底部并恢复自动跟随 */
function backToBottom(): void {
  autoScroll.value = true
  emit('autoScrollChange', true)
  scrollToBottom('smooth')
}

onMounted(() => {
  scrollToBottom()
})

/** 供父级（会话切换时）强制滚到底并恢复跟随 */
function resetToBottom(): void {
  autoScroll.value = true
  emit('autoScrollChange', true)
  void nextTick(() => scrollToBottom())
}

defineExpose({ resetToBottom, backToBottom })

const hasContent = computed(() => props.messages.length > 0 || props.typing || !!props.streamingText)
</script>

<template>
  <div class="stream">
    <div ref="scroller" class="stream__scroller" @scroll.passive="onScroll">
      <PageState
        v-if="props.loading && !hasContent"
        state="loading"
        title="正在加载消息历史…"
      />
      <PageState
        v-else-if="props.error && !hasContent"
        state="error"
        :detail="props.error"
      />
      <PageState
        v-else-if="!hasContent"
        state="empty"
        title="该会话还没有消息"
        description="等待买家发起咨询，或由坐席主动发起"
      />

      <template v-else>
        <div class="stream__list">
          <MessageBubble
            v-for="message in props.messages"
            :key="message.message_id"
            :message="message"
            :feedback-enabled="props.feedbackEnabled"
            @feedback="(m: ChatMessage) => emit('feedback', m)"
          />

          <!-- 正在输入的阶段指示（turn.start / action / retrieval / intent） -->
          <TypingIndicator
            :active="props.typing && !props.streamingText"
            :stage="props.stage"
            :retrieval-hits="props.retrievalHits"
            :retrieval-top-score="props.retrievalTopScore"
            :intent="props.intent"
            :confidence="props.confidence"
          />

          <!-- 打字机：delta 累积文本 -->
          <div v-if="props.streamingText" class="stream__streaming">
            <div class="stream__streaming-avatar">🤖</div>
            <div class="stream__streaming-bubble">
              <p class="stream__streaming-text">{{ props.streamingText }}</p>
              <span class="stream__caret" />
            </div>
          </div>

          <!-- error 事件：兜底话术 + 重试按钮 -->
          <div v-if="props.streamError" class="stream__error">
            <span class="stream__error-icon">⚠️</span>
            <div class="stream__error-body">
              <span class="stream__error-text">{{ props.streamError }}</span>
              <span class="stream__error-hint">
                机器人本次未能生成回复，请直接接管回复买家，或重试一次。
              </span>
            </div>
            <el-button size="small" plain @click="emit('retryStream')">重试</el-button>
          </div>
        </div>
      </template>
    </div>

    <!-- 用户上滚后显示「回到底部」，同时提示已暂停自动跟随 -->
    <Transition name="fade">
      <button v-if="!autoScroll" class="stream__jump" type="button" @click="backToBottom">
        ↓ 已暂停自动滚动 · 回到底部
      </button>
    </Transition>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.stream {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;

  &__scroller {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  &__list {
    display: flex;
    flex-direction: column;
    padding: $space-3 0 $space-6;
  }

  &__streaming {
    display: flex;
    gap: $space-3;
    padding: $space-2 $space-4;
  }

  &__streaming-avatar {
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

  &__streaming-bubble {
    position: relative;
    max-width: 620px;
    padding: $space-2 $space-3;
    border-radius: $radius-md;
    background: $mint-bg;
    border: 1px solid rgba(62, 142, 126, 0.25);
  }

  &__streaming-text {
    margin: 0;
    font-size: $text-base;
    line-height: 1.65;
    color: $ink-900;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* 打字光标 */
  &__caret {
    display: inline-block;
    width: 2px;
    height: 14px;
    margin-left: 2px;
    vertical-align: -2px;
    background: $mint-500;
    animation: caret-blink 0.9s steps(2, start) infinite;
  }

  &__error {
    display: flex;
    align-items: center;
    gap: $space-3;
    margin: $space-2 $space-4;
    padding: $space-3;
    border-radius: $radius-sm;
    background: $danger-bg;
    border: 1px solid rgba(217, 74, 74, 0.25);
  }

  &__error-icon {
    font-size: 18px;
  }

  &__error-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__error-text {
    font-size: $text-sm;
    font-weight: 600;
    color: $danger-ink;
  }

  &__error-hint {
    font-size: $text-xs;
    color: $ink-600;
  }

  &__jump {
    position: absolute;
    left: 50%;
    bottom: $space-4;
    transform: translateX(-50%);
    padding: $space-1 $space-3;
    border: 1px solid $ink-200;
    border-radius: $radius-pill;
    background: $surface;
    box-shadow: $shadow-md;
    color: $ink-700;
    font-size: $text-xs;
    cursor: pointer;

    &:hover {
      border-color: $brand-400;
      color: $brand-ink;
    }
  }
}

/* 消息项跳过视口外渲染：500 条规模下维持滚动帧率 */
.stream__list > * {
  content-visibility: auto;
  contain-intrinsic-size: auto 96px;
}

@keyframes caret-blink {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0;
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .stream__caret {
    animation: none;
  }
}
</style>
