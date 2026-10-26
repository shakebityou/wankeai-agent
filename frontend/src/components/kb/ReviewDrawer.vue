<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { KB_TYPE_LABELS, type KbChunk } from '@/types/contract'

/**
 * 审核抽屉 —— FE-05（P6）。
 *
 * 任务书：ReviewDrawer.vue（通过/驳回 + 意见）
 * DoD：「未审核条目不出现在坐席检索结果中」—— 所以审核这一步是知识进库的唯一闸门。
 *
 * 设计决策：
 *   1. **驳回必须填意见** —— 通过可以不填，驳回必须说明原因。
 *      理由：驳回后运营要改，没有意见就只能猜；而且意见会写进 review_comment 留痕。
 *   2. **原文与元数据同屏** —— 审核人要能同时看到正文、权威来源、生效区间、关联 SKU。
 *      只看正文就通过，等于放弃了「这条知识的出处是否可信」这个最关键的判断。
 *   3. **不支持批量通过无意见** —— 批量审核走的是另一条路径（默认意见），
 *      抽屉只处理单条，避免「一路回车全通过」把审核变成形式。
 */
interface Props {
  modelValue: boolean
  chunk: KbChunk | null
  submitting?: boolean
}

const props = withDefaults(defineProps<Props>(), { submitting: false })

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'submit', payload: { decision: 'APPROVED' | 'REJECTED'; comment: string }): void
}>()

const decision = ref<'APPROVED' | 'REJECTED'>('APPROVED')
const comment = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      decision.value = 'APPROVED'
      comment.value = ''
    }
  },
)

const canSubmit = computed(() => decision.value === 'APPROVED' || comment.value.trim().length > 0)

function close(): void {
  emit('update:modelValue', false)
}

function submit(): void {
  if (!canSubmit.value) return
  emit('submit', { decision: decision.value, comment: comment.value.trim() })
}
</script>

<template>
  <el-drawer
    :model-value="props.modelValue"
    title="审核知识条目"
    size="620px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-if="props.chunk" class="review">
      <header class="review__head">
        <h3 class="review__title">{{ props.chunk.title }}</h3>
        <div class="review__meta">
          <span class="wk-mono">{{ props.chunk.chunk_id }}</span>
          <EnumTag kind="review-status" :value="props.chunk.review_status" />
          <span class="wk-pill wk-pill--neutral">{{ KB_TYPE_LABELS[props.chunk.kb_type] }}</span>
          <span class="wk-pill wk-pill--neutral">{{ props.chunk.char_count }} 字</span>
        </div>
      </header>

      <dl class="review__fields">
        <div class="review__field">
          <dt>分类</dt>
          <dd>{{ props.chunk.category }}</dd>
        </div>
        <div class="review__field">
          <dt>权威来源</dt>
          <dd class="review__authority">{{ props.chunk.authority }}</dd>
        </div>
        <div class="review__field">
          <dt>适用渠道</dt>
          <dd>{{ props.chunk.channel === 'ALL' ? '全渠道' : props.chunk.channel }}</dd>
        </div>
        <div class="review__field">
          <dt>生效区间</dt>
          <dd class="wk-mono">
            {{ props.chunk.effective_from ?? '立即生效' }} ~ {{ props.chunk.effective_to ?? '长期有效' }}
          </dd>
        </div>
        <div v-if="props.chunk.sku_ids.length" class="review__field">
          <dt>关联 SKU</dt>
          <dd class="wk-mono">{{ props.chunk.sku_ids.join('、') }}</dd>
        </div>
        <div class="review__field">
          <dt>提交人</dt>
          <dd>{{ props.chunk.updated_by }} · {{ props.chunk.updated_at.slice(0, 16).replace('T', ' ') }}</dd>
        </div>
      </dl>

      <section class="review__content">
        <h4 class="review__section-title">正文</h4>
        <pre class="review__pre">{{ props.chunk.content }}</pre>
      </section>

      <section class="review__decision">
        <h4 class="review__section-title">审核结论</h4>
        <el-radio-group v-model="decision">
          <el-radio value="APPROVED">通过 —— 进入向量库，坐席可检索</el-radio>
          <el-radio value="REJECTED">驳回 —— 退回修改，不进入检索</el-radio>
        </el-radio-group>

        <el-input
          v-model="comment"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 6 }"
          :placeholder="decision === 'REJECTED' ? '请说明驳回原因（必填），运营需要据此修改' : '审核意见（可选）'"
          class="review__comment"
        />
        <p v-if="decision === 'REJECTED' && !comment.trim()" class="review__warn">驳回必须填写原因</p>
        <p v-else class="review__hint">
          审核结论与意见会写入该条目的版本历史（reviewer = 当前登录人）
        </p>
      </section>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button :type="decision === 'APPROVED' ? 'primary' : 'danger'" :disabled="!canSubmit" :loading="props.submitting" @click="submit">
        {{ decision === 'APPROVED' ? '确认通过' : '确认驳回' }}
      </el-button>
    </template>
  </el-drawer>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.review {
  display: flex;
  flex-direction: column;
  gap: $space-4;

  &__head {
    display: flex;
    flex-direction: column;
    gap: $space-2;
  }

  &__title {
    margin: 0;
    font-size: $text-md;
    font-weight: 700;
    color: $ink-900;
  }

  &__meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
    font-size: 11px;
    color: $ink-500;
  }

  &__fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: $space-2 $space-3;
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 1px;

    dt {
      font-size: 11px;
      color: $ink-500;
    }

    dd {
      margin: 0;
      font-size: $text-xs;
      color: $ink-800;
    }
  }

  &__authority {
    font-weight: 600;
    color: $brand-ink;
  }

  &__section-title {
    margin: 0 0 $space-2;
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-700;
  }

  &__content {
    min-width: 0;
  }

  &__pre {
    margin: 0;
    padding: $space-3;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    background: $surface-sunken;
    font-family: $font-sans;
    font-size: $text-sm;
    line-height: 1.8;
    color: $ink-800;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 280px;
    overflow-y: auto;
  }

  &__decision {
    display: flex;
    flex-direction: column;
    gap: $space-2;
    padding-top: $space-3;
    border-top: 1px dashed $ink-150;
  }

  &__comment {
    margin-top: $space-1;
  }

  &__warn {
    margin: 0;
    font-size: $text-xs;
    color: $danger-ink;
  }

  &__hint {
    margin: 0;
    font-size: 11px;
    color: $ink-400;
  }
}
</style>
