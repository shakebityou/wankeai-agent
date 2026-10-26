<script setup lang="ts">
import { computed, ref } from 'vue'
import { FEEDBACK_ERROR_TYPES, FEEDBACK_ERROR_TYPE_LABELS, type FeedbackErrorType } from '@/types/contract'

/**
 * 「这条答错了」表单 —— FE-08（P9 纠错待办）。
 *
 * 任务书：FeedbackForm.vue（错误类型：事实错误/话术过时/口径错误/答非所问）
 * DoD：「标记到待办 ≤3 秒」「错误类型分布可统计」
 *
 * 设计决策：
 *   1. **错误类型是必选的单选，不是自由文本** —— 任务书要求「错误类型分布可统计」，
 *      自由文本无法统计；而且四类错误对应四种完全不同的修复动作
 *      （事实错误→改数据、话术过时→改措辞、口径错误→改政策、答非所问→改召回），
 *      选错类型会让待办派给错的人。
 *   2. **每类错误给一句示例** —— 坐席不一定能分清「口径错误」和「事实错误」，
 *      给例子比给定义有效。示例取自商家历史错误分布里的真实场景。
 *   3. **展示 AI 当时的回答与命中的知识条目** —— 纠错的价值在于能定位到具体 chunk。
 *      不带 chunk_ids 的纠错，运营只能靠猜。
 *   4. **提交即反馈，不做二次确认** —— 坐席在会话中，每多一步就多一次放弃的理由。
 *      DoD 要求「标记到待办 ≤3 秒」，交互步骤也必须 ≤3 秒。
 */
interface Props {
  messageId: string
  sessionId: string
  /** AI 当时的回复原文，供坐席确认标记对象 */
  replyText: string
  /** 该条回复命中的知识条目 */
  chunkIds: string[]
  submitting?: boolean
}

const props = withDefaults(defineProps<Props>(), { submitting: false })

const emit = defineEmits<{
  (e: 'submit', payload: { errorType: FeedbackErrorType; comment: string }): void
  (e: 'cancel'): void
}>()

/** 每类错误的示例 —— 帮助坐席快速归类 */
const EXAMPLES: Record<FeedbackErrorType, string> = {
  FACT_ERROR: '例：回答「支持机洗」但实际洗标写的是「建议手洗」',
  OUTDATED: '例：仍在用旧话术「7 天发货」，而政策已改为 48 小时内发货',
  POLICY_MISMATCH: '例：退货运费口径与最新售后政策 4.2 条不一致',
  IRRELEVANT: '例：买家问「多久发货」，机器人答了「怎么洗护」',
}

const errorType = ref<FeedbackErrorType>('FACT_ERROR')
const comment = ref('')

/** 评论可选，但选了「答非所问」以外的类型时建议补充说明 */
const commentHint = computed(() =>
  comment.value.trim() ? '' : '补充说明能让运营更快定位问题（可选）',
)

const chunkSummary = computed(() => {
  if (!props.chunkIds.length) return '本条回复未命中任何知识条目（可能是模型自由生成）'
  return `命中知识条目：${props.chunkIds.join('、')}`
})

function submit(): void {
  emit('submit', { errorType: errorType.value, comment: comment.value.trim() })
}
</script>

<template>
  <div class="feedback-form">
    <section class="feedback-form__section">
      <h4 class="feedback-form__title">AI 当时的回答</h4>
      <pre class="feedback-form__reply">{{ props.replyText }}</pre>
      <p class="feedback-form__chunks">{{ chunkSummary }}</p>
    </section>

    <section class="feedback-form__section">
      <h4 class="feedback-form__title">这条回答哪里错了？</h4>
      <ul class="types">
        <li v-for="type in FEEDBACK_ERROR_TYPES" :key="type" class="types__item">
          <label class="types__label" :class="{ 'is-active': errorType === type }">
            <input v-model="errorType" type="radio" :value="type" class="types__radio" />
            <span class="types__name">{{ FEEDBACK_ERROR_TYPE_LABELS[type] }}</span>
            <span class="types__example">{{ EXAMPLES[type] }}</span>
          </label>
        </li>
      </ul>
    </section>

    <section class="feedback-form__section">
      <h4 class="feedback-form__title">补充说明</h4>
      <el-input
        v-model="comment"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 5 }"
        placeholder="描述具体问题，例如正确说法应该是什么"
      />
      <p v-if="commentHint" class="feedback-form__hint">{{ commentHint }}</p>
    </section>

    <footer class="feedback-form__foot">
      <span class="feedback-form__note">
        提交后立即进入纠错待办，由知识库运营处理；坐席无需等待处理结果。
      </span>
      <span class="wk-spacer" />
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="props.submitting" @click="submit">提交纠错</el-button>
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.feedback-form {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__section {
    display: flex;
    flex-direction: column;
    gap: $space-2;
  }

  &__title {
    margin: 0;
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-700;
  }

  &__reply {
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;
    border-left: 3px solid $mint-500;
    font-family: $font-sans;
    font-size: $text-sm;
    line-height: 1.7;
    color: $ink-800;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 160px;
    overflow-y: auto;
  }

  &__chunks {
    margin: 0;
    font-size: 11px;
    color: $ink-500;
  }

  &__hint {
    margin: 0;
    font-size: 11px;
    color: $ink-400;
  }

  &__foot {
    display: flex;
    align-items: center;
    gap: $space-3;
    padding-top: $space-3;
    border-top: 1px solid $ink-150;
  }

  &__note {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.types {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: $space-2;

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }

  &__item {
    min-width: 0;
  }

  &__label {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: $space-2 $space-3;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    cursor: pointer;
    transition: border-color 0.15s ease, background 0.15s ease;

    &:hover {
      border-color: $brand-300;
    }

    &.is-active {
      border-color: $brand-500;
      background: $brand-50;
    }
  }

  &__radio {
    display: none;
  }

  &__name {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
  }

  &__example {
    font-size: 11px;
    line-height: 1.6;
    color: $ink-500;
  }
}
</style>
