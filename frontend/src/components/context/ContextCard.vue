<script setup lang="ts">
import { computed, watch } from 'vue'
import EmotionTag from './EmotionTag.vue'
import SuggestedAction from './SuggestedAction.vue'
import TriedActions from './TriedActions.vue'
import UnmetDemand from './UnmetDemand.vue'
import VerifiedFacts from './VerifiedFacts.vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { CONTEXT_ERROR_MESSAGE, guardContext, reportContextAnomaly } from '@/utils/context-guard'
import { formatCount, formatDuration } from '@/utils/format'
import type { SessionContext } from '@/types/contract'

/**
 * AI 上下文卡片 —— FE-02 ★ 核心组件（P2 会话详情页右栏）。
 *
 * 任务书原文（4.3 FE-02）：
 *   「七要素缺一即报错…前端做字段完整性守卫，缺失时展示明确错误而非空白」
 *   「字段缺失展示『上下文生成异常，请联系技术支持』并上报埋点，不允许静默空白」
 *   「DoD：七要素完整率 100%；渲染 ≤200ms」
 *
 * 三个关键设计决策：
 *
 * 1. 守卫先于渲染。`guardContext` 在 computed 里执行，缺失时整张卡片换成
 *    错误态 —— 而不是「能渲染几块渲染几块」。理由：缺字段的上下文会误导坐席
 *    （比如缺 unmet_demand 时坐席会以为买家没有未满足诉求），
 *    错误态比「半张卡片」安全得多。
 *
 * 2. 错误态必须可操作。除文案外还展示「缺失要素清单」，坐席能直接告诉技术支持
 *    少了什么，而不是只能说「它坏了」。
 *
 * 3. 埋点只报一次。用 watch 监听 session_id + 缺失字段集合，避免同一异常在
 *    重渲染时反复上报（否则埋点会被噪声淹没）。
 */
interface Props {
  /** 原始上下文；允许是缺字段的部分对象，守卫会检查 */
  context: Partial<SessionContext> | null | undefined
  /** 会话 ID，用于埋点归属 */
  sessionId: string
  /** 加载中 */
  loading?: boolean
  /** 加载失败的错误信息 */
  error?: string | null
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  error: null,
})

const emit = defineEmits<{ retry: [] }>()

const guard = computed(() => guardContext(props.context))

/** 缺失要素的稳定签名，用于埋点去重 */
const missingSignature = computed(() => guard.value.missing.slice().sort().join(','))

watch(
  () => [props.sessionId, missingSignature.value] as const,
  ([sessionId, signature]) => {
    if (!sessionId || !signature) return
    reportContextAnomaly(sessionId, guard.value.missing)
  },
  { immediate: true },
)

const ctx = computed(() => guard.value.context)
</script>

<template>
  <div class="context-card">
    <!-- 加载态 -->
    <div v-if="props.loading" class="context-card__skeleton">
      <el-skeleton :rows="6" animated />
    </div>

    <!-- 加载失败 -->
    <div v-else-if="props.error" class="context-card__error">
      <span class="context-card__error-icon">⚠️</span>
      <span class="context-card__error-title">上下文加载失败</span>
      <span class="context-card__error-desc">{{ props.error }}</span>
      <el-button type="primary" plain size="small" @click="emit('retry')">重新加载</el-button>
    </div>

    <!-- 七要素守卫未通过：明确报错，绝不静默空白 -->
    <div v-else-if="!guard.ok" class="context-card__error" data-testid="context-guard-error">
      <span class="context-card__error-icon">🚨</span>
      <span class="context-card__error-title">{{ CONTEXT_ERROR_MESSAGE }}</span>
      <span class="context-card__error-desc">
        该会话的 AI 上下文缺少 {{ guard.missing.length }} 项必需要素，为避免误导请勿直接接手。
      </span>
      <div class="context-card__missing">
        <span
          v-for="label in guard.missingLabels"
          :key="label"
          class="wk-pill wk-pill--bad"
        >
          {{ label }}
        </span>
      </div>
      <el-button type="primary" plain size="small" @click="emit('retry')">重新加载</el-button>
    </div>

    <!-- 守卫通过：正常渲染七要素 -->
    <div v-else-if="ctx" class="context-card__body">
      <!-- 顶部概览：轮次 + 等待时长 -->
      <div class="context-card__overview">
        <div class="overview-item">
          <span class="overview-item__label">咨询轮次</span>
          <span class="overview-item__value">{{ formatCount(ctx.turn_count) }}</span>
        </div>
        <div class="overview-item">
          <span class="overview-item__label">已等待</span>
          <span class="overview-item__value">{{ formatDuration(ctx.waiting_seconds) }}</span>
        </div>
        <div class="overview-item">
          <span class="overview-item__label">渠道</span>
          <EnumTag kind="channel" :value="ctx.channel" />
        </div>
        <div v-if="ctx.ticket_no" class="overview-item">
          <span class="overview-item__label">工单</span>
          <span class="overview-item__value wk-mono">{{ ctx.ticket_no }}</span>
        </div>
      </div>

      <!-- 要素 4：未满足诉求（警示色高亮，视觉权重最高） -->
      <section class="context-card__section">
        <UnmetDemand :demand="ctx.unmet_demand" />
      </section>

      <!-- 要素 1：对话摘要 -->
      <section class="context-card__section">
        <h4 class="context-card__heading">对话摘要</h4>
        <p class="context-card__summary">{{ ctx.summary }}</p>
      </section>

      <!-- 要素 6：情绪 -->
      <section class="context-card__section">
        <EmotionTag
          :level="ctx.emotion.level"
          :intensity="ctx.emotion.intensity"
          :trigger="ctx.emotion.trigger"
        />
      </section>

      <!-- 要素 5：建议动作 -->
      <section class="context-card__section">
        <SuggestedAction :action="ctx.suggested_action" />
      </section>

      <!-- 要素 2：已验证事实 -->
      <section class="context-card__section">
        <h4 class="context-card__heading">
          已验证事实
          <span class="context-card__count">{{ ctx.verified.length }}</span>
        </h4>
        <VerifiedFacts :facts="ctx.verified" />
      </section>

      <!-- 要素 3：已尝试动作 -->
      <section class="context-card__section">
        <h4 class="context-card__heading">
          已尝试动作
          <span class="context-card__count">{{ ctx.tried_actions.length }}</span>
        </h4>
        <TriedActions :actions="ctx.tried_actions" />
      </section>

      <!-- 附件（商品图）—— 只展示，不跳转（HC-1） -->
      <section v-if="ctx.attachments.length" class="context-card__section">
        <h4 class="context-card__heading">关联素材</h4>
        <div class="context-card__attachments">
          <figure v-for="(item, index) in ctx.attachments" :key="index" class="attachment">
            <img
              v-if="item.url"
              class="attachment__img"
              :src="item.url"
              :alt="item.tag"
              loading="lazy"
            />
            <figcaption class="attachment__tag">{{ item.tag }}</figcaption>
          </figure>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.context-card {
  &__skeleton {
    padding: $space-2 0;
  }

  &__error {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: $space-2;
    padding: $space-6 $space-4;
    text-align: center;
  }

  &__error-icon {
    font-size: 28px;
  }

  &__error-title {
    font-size: $text-md;
    font-weight: 600;
    color: $danger-ink;
  }

  &__error-desc {
    font-size: $text-xs;
    color: $ink-600;
    line-height: 1.6;
  }

  &__missing {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: $space-1;
    margin: $space-1 0;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: $space-4;
  }

  &__overview {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: $space-2;
    padding: $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;
  }

  &__section {
    display: flex;
    flex-direction: column;
    gap: $space-2;
  }

  &__heading {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin: 0;
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-700;
  }

  &__count {
    padding: 0 6px;
    border-radius: $radius-pill;
    background: $ink-100;
    color: $ink-600;
    font-size: 11px;
    font-weight: 500;
  }

  &__summary {
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;
    font-size: $text-base;
    line-height: 1.7;
    color: $ink-800;
  }

  &__attachments {
    display: flex;
    flex-wrap: wrap;
    gap: $space-2;
  }
}

.overview-item {
  display: flex;
  align-items: center;
  gap: $space-2;
  min-width: 0;

  &__label {
    font-size: $text-xs;
    color: $ink-500;
    flex: none;
  }

  &__value {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-800;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.attachment {
  margin: 0;
  width: 76px;

  &__img {
    width: 76px;
    height: 76px;
    object-fit: cover;
    border-radius: $radius-sm;
    border: 1px solid $ink-200;
    background: $surface-sunken;
  }

  &__tag {
    margin-top: 2px;
    font-size: 11px;
    color: $ink-500;
    text-align: center;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
