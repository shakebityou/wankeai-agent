<script setup lang="ts">
import { computed } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { formatDateTime, formatLatency, scrubText } from '@/utils/format'
import {
  MESSAGE_KIND_GLYPHS,
  MESSAGE_KIND_LABELS,
  containsUrl,
  resolveMessageKind,
  stripUrls,
} from '@/utils/message-kind'
import {
  SENDER_LABELS,
  TEXT_SOURCE_LABELS,
  type ChatMessage,
} from '@/types/contract'

/**
 * 消息气泡 —— FE-01。
 *
 * 任务书 DoD：「五种 msg_type（TEXT/IMAGE/VOICE 转文字/OCR 结果/转人工卡片）渲染正确」。
 * 五种类型不是靠 msg_type 字段直接区分的（后端只回 TEXT/IMAGE），
 * 而是由 resolveMessageKind 推导 —— 推导逻辑集中在 utils/message-kind.ts，
 * 避免这里和回放页各写一套。
 *
 * 合规红线（4.5.2 / HC-1）—— 这是全项目最关键的一条：
 *   1. 气泡正文一律按**纯文本**渲染（不用 v-html），因此即使文本里带 <a> 也不会成为链接；
 *   2. 渲染前调用 stripUrls 把外链替换为「[链接已屏蔽]」，
 *      从根本上不让链接出现在坐席/买家可见内容里；
 *   3. 图片走 image_ref（同源相对路径），HC-1 禁止外链，故不使用任何远程 URL；
 *   4. 手机号等敏感信息再过一遍 scrubText 兜底（HC-2 最小必要）。
 *
 * 注意：`containsUrl` 仅用于**开发期可视化告警**（气泡右上角打标），
 * 不参与合规判定 —— 合规由自动化脚本扫描，不依赖运行时。
 */
const props = defineProps<{
  message: ChatMessage
  /** 是否高亮（知识检索定位、回放跳转时用） */
  highlight?: boolean
  /** 是否显示「这条答错了」按钮（FE-08，仅 BOT 消息） */
  feedbackEnabled?: boolean
}>()

const emit = defineEmits<{ feedback: [message: ChatMessage] }>()

const kind = computed(() => resolveMessageKind(props.message))

const isUser = computed(() => props.message.sender === 'USER')
const isBot = computed(() => props.message.sender === 'BOT')

/** 展示文本：脱敏 + 去外链（顺序很重要：先去链接再抹号码） */
const displayText = computed(() => scrubText(stripUrls(props.message.text)))

/** 该消息原本含外链（开发期告警，不影响渲染结果） */
const hadUrl = computed(() => containsUrl(props.message.text))

const hasLatency = computed(() => props.message.latency_ms !== null)
</script>

<template>
  <div
    class="bubble-row"
    :class="{
      'is-user': isUser,
      'is-bot': isBot,
      'is-agent': props.message.sender === 'AGENT',
      'is-handoff': kind === 'HANDOFF_CARD',
      'is-highlight': props.highlight,
    }"
  >
    <!-- 发送方标识：转人工卡片不显示 -->
    <div v-if="kind !== 'HANDOFF_CARD'" class="bubble-row__avatar">
      {{ isUser ? '👤' : isBot ? '🤖' : '🎧' }}
    </div>

    <div class="bubble-row__main">
      <!-- 转人工卡片：独立形态，不走普通气泡 -->
      <div v-if="kind === 'HANDOFF_CARD'" class="handoff-card">
        <div class="handoff-card__head">
          <span class="handoff-card__glyph">🎫</span>
          <span class="handoff-card__title">已转接人工客服</span>
          <EnumTag
            v-if="props.message.handoff_reason"
            kind="handoff-reason"
            :value="props.message.handoff_reason"
          />
        </div>
        <p class="handoff-card__text">{{ displayText }}</p>
        <div class="handoff-card__meta">
          <span>{{ formatDateTime(props.message.created_at) }}</span>
          <span v-if="props.message.trace_id" class="wk-mono">{{ props.message.trace_id }}</span>
        </div>
      </div>

      <!-- 普通气泡 -->
      <template v-else>
        <div class="bubble-row__meta">
          <span class="bubble-row__sender">{{ SENDER_LABELS[props.message.sender] }}</span>
          <span
            v-if="kind !== 'TEXT'"
            class="bubble-row__kind"
            :title="MESSAGE_KIND_LABELS[kind]"
          >
            {{ MESSAGE_KIND_GLYPHS[kind] }} {{ MESSAGE_KIND_LABELS[kind] }}
          </span>
          <span v-if="props.message.text_source !== 'ORIGINAL'" class="bubble-row__source">
            {{ TEXT_SOURCE_LABELS[props.message.text_source] }}
          </span>
          <span class="bubble-row__time">{{ formatDateTime(props.message.created_at) }}</span>
        </div>

        <div class="bubble" :class="`bubble--${kind.toLowerCase()}`">
          <!-- 图片消息：同源相对路径，HC-1 禁止外链 -->
          <figure v-if="kind === 'IMAGE' && props.message.image_ref" class="bubble__figure">
            <img
              class="bubble__img"
              :src="props.message.image_ref"
              :alt="props.message.image_alt ?? '买家上传图片'"
              loading="lazy"
            />
            <figcaption v-if="props.message.image_alt" class="bubble__caption">
              {{ props.message.image_alt }}
            </figcaption>
          </figure>

          <!-- 正文：纯文本渲染，绝不使用 v-html -->
          <p v-if="displayText" class="bubble__text">{{ displayText }}</p>
          <p v-else-if="kind === 'IMAGE'" class="bubble__text wk-dim">（图片消息，无文字说明）</p>
        </div>

        <div class="bubble-row__foot">
          <span v-if="hasLatency" class="bubble-row__latency">
            耗时 {{ formatLatency(props.message.latency_ms) }}
          </span>
          <span v-if="hadUrl" class="bubble-row__warn" title="原文本含外链，已屏蔽">
            ⚠️ 外链已屏蔽
          </span>
          <button
            v-if="props.feedbackEnabled && isBot"
            class="bubble-row__feedback"
            type="button"
            @click="emit('feedback', props.message)"
          >
            {{ props.message.feedback_marked ? '✓ 已标记答错' : '这条答错了' }}
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.bubble-row {
  display: flex;
  gap: $space-3;
  padding: $space-2 $space-4;
  transition: background 0.15s ease;

  &.is-highlight {
    background: $warning-bg;
  }

  &__avatar {
    flex: none;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
    background: $ink-100;
  }

  &.is-bot &__avatar {
    background: $mint-bg;
  }

  &.is-agent &__avatar {
    background: $brand-50;
  }

  &__main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
    align-items: flex-start;
  }

  &.is-user &__main {
    align-items: flex-end;
  }

  &__meta {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: 11px;
    color: $ink-500;
  }

  &__sender {
    font-weight: 600;
    color: $ink-600;
  }

  &__kind {
    padding: 0 5px;
    border-radius: $radius-xs;
    background: $info-bg;
    color: $info-ink;
  }

  &__source {
    padding: 0 5px;
    border-radius: $radius-xs;
    background: $mint-bg;
    color: $mint-ink;
  }

  &__foot {
    display: flex;
    align-items: center;
    gap: $space-2;
    min-height: 16px;
  }

  &__latency {
    font-size: 11px;
    color: $ink-400;
  }

  &__warn {
    font-size: 11px;
    color: $warning-ink;
  }

  &__feedback {
    padding: 0;
    border: 0;
    background: none;
    color: $ink-400;
    font-size: 11px;
    cursor: pointer;
    text-decoration: underline dotted;

    &:hover {
      color: $danger-ink;
    }
  }
}

.bubble {
  max-width: 620px;
  padding: $space-2 $space-3;
  border-radius: $radius-md;
  background: $surface-sunken;
  border: 1px solid $ink-150;
  font-size: $text-base;
  line-height: 1.65;
  color: $ink-900;
  word-break: break-word;

  /* 买家消息：品牌浅色 + 右对齐由父级控制 */
  .is-user & {
    background: $brand-50;
    border-color: $brand-200;
  }

  /* 机器人消息：薄荷浅色，与「知识侧」呼应 */
  .is-bot & {
    background: $mint-bg;
    border-color: rgba(62, 142, 126, 0.25);
  }

  /* 坐席消息：白底 + 品牌左边条，与机器人明显区分（接管后一眼可辨） */
  .is-agent & {
    background: $surface;
    border-color: $ink-200;
    box-shadow: inset 3px 0 0 $brand-500;
  }

  &--voice {
    border-style: dashed;
  }

  &--ocr {
    border-style: dashed;
  }

  &__figure {
    margin: 0;
  }

  &__img {
    display: block;
    max-width: 240px;
    max-height: 240px;
    border-radius: $radius-sm;
    border: 1px solid $ink-200;
    background: $surface;
    object-fit: contain;
  }

  &__caption {
    margin-top: $space-1;
    font-size: 11px;
    color: $ink-500;
  }

  &__text {
    margin: 0;
    white-space: pre-wrap;
  }
}

.handoff-card {
  width: 100%;
  max-width: 520px;
  padding: $space-3;
  border-radius: $radius-md;
  background: $info-bg;
  border: 1px solid rgba(74, 127, 217, 0.3);

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-bottom: $space-2;
  }

  &__glyph {
    font-size: $text-md;
  }

  &__title {
    font-size: $text-sm;
    font-weight: 600;
    color: $info-ink;
  }

  &__text {
    margin: 0;
    font-size: $text-base;
    line-height: 1.65;
    color: $ink-900;
  }

  &__meta {
    display: flex;
    justify-content: space-between;
    gap: $space-2;
    margin-top: $space-2;
    font-size: 11px;
    color: $ink-500;
  }
}
</style>
