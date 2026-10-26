<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { stripUrls } from '@/utils/message-kind'
import {
  CHANNELS,
  CHANNEL_LABELS,
  KB_TYPES,
  KB_TYPE_LABELS,
  CHUNK_LENGTH_RANGE,
  type KbChunk,
  type KbChunkPayload,
} from '@/types/contract'

/**
 * 知识条目编辑器 —— FE-05（P6）。
 *
 * 任务书：KbEditor.vue（Markdown）+ ChunkMetaForm.vue（authority/effective_from/effective_to/channel）
 * DoD：「元数据字段完整率 100%」「变更记录 updated_by」
 *
 * 设计决策：
 *   1. **纯 Markdown，不引富文本编辑器** —— 任务书 4.1 明确「知识条目用纯 Markdown 编辑器（轻量）」。
 *      理由不只是体积：知识条目要进向量库，富文本会带入 HTML 标签噪声，污染检索与切分。
 *   2. **左侧写、右侧实时预览 + 字数** —— 字数直接对着 CHUNK_LENGTH_RANGE 显示区间与状态。
 *      理由：运营在写的时候就该知道这条会落在 200–400 还是已经 620 字（会被切成两段，
 *      可能切断语义），等到导入预览再发现就晚了。
 *   3. **元数据必填校验前置到提交前** —— authority 与 category 是溯源的关键，
 *      缺了它们，坐席检索到这条也判断不了可信度（KbSearchHit.authority 就是展示给坐席看的）。
 *   4. **生效区间留空 = 立即生效且不过期** —— 用文案说明而不是让运营猜。
 */
interface Props {
  /** 编辑已有条目；为空表示新建 */
  chunk?: KbChunk | null
  /** 提交中 */
  submitting?: boolean
}

const props = withDefaults(defineProps<Props>(), { chunk: null, submitting: false })

const emit = defineEmits<{
  (e: 'submit', payload: KbChunkPayload): void
  (e: 'cancel'): void
}>()

const form = ref<KbChunkPayload>({
  title: '',
  content: '',
  kb_type: 'FAQ',
  category: '',
  sku_ids: [],
  authority: '',
  effective_from: null,
  effective_to: null,
  channel: 'ALL',
})

/** 生效区间的日期控件值（与 null 互转） */
const effectiveRange = ref<[string, string] | null>(null)

watch(
  () => props.chunk,
  (chunk) => {
    if (chunk) {
      form.value = {
        title: chunk.title,
        content: chunk.content,
        kb_type: chunk.kb_type,
        category: chunk.category,
        sku_ids: [...chunk.sku_ids],
        authority: chunk.authority,
        effective_from: chunk.effective_from,
        effective_to: chunk.effective_to,
        channel: chunk.channel,
      }
      effectiveRange.value =
        chunk.effective_from && chunk.effective_to
          ? [chunk.effective_from, chunk.effective_to]
          : null
    }
  },
  { immediate: true },
)

watch(effectiveRange, (range) => {
  form.value.effective_from = range?.[0] ?? null
  form.value.effective_to = range?.[1] ?? null
})

/** 去空白后的字数 —— 与后端 char_count 口径一致（不含空白字符） */
const charCount = computed(() => form.value.content.replace(/\s/g, '').length)

const lengthRange = computed(() => CHUNK_LENGTH_RANGE[form.value.kb_type])

const lengthStatus = computed<'OK' | 'TOO_SHORT' | 'TOO_LONG'>(() => {
  if (charCount.value < lengthRange.value.min) return 'TOO_SHORT'
  if (charCount.value > lengthRange.value.max) return 'TOO_LONG'
  return 'OK'
})

const lengthHint = computed(() => {
  const { min, max } = lengthRange.value
  switch (lengthStatus.value) {
    case 'TOO_SHORT':
      return `不足 ${min} 字，召回时可能缺乏上下文，建议补充细节`
    case 'TOO_LONG':
      return `超过 ${max} 字，入库时会被切分，可能切断语义，建议拆成多条`
    default:
      return `字数在目标区间 ${min}–${max} 字内`
  }
})

/** 必填校验 —— 字段与中文标签一一对应 */
const missing = computed(() => {
  const list: string[] = []
  if (!form.value.title.trim()) list.push('标题')
  if (!form.value.content.trim()) list.push('正文')
  if (!form.value.category.trim()) list.push('分类')
  if (!form.value.authority.trim()) list.push('权威来源')
  if (!form.value.kb_type) list.push('知识类型')
  if (!form.value.channel) list.push('适用渠道')
  return list
})

const canSubmit = computed(() => missing.value.length === 0)

/** SKU 关联：用输入框录入，逗号分隔 —— 商品知识才需要 */
const skuInput = ref('')

watch(
  () => form.value.sku_ids,
  (ids) => {
    skuInput.value = ids.join('、')
  },
  { immediate: true, deep: true },
)

function onSkuInput(value: string): void {
  skuInput.value = value
  form.value.sku_ids = value
    .split(/[、,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function submit(): void {
  if (!canSubmit.value) return
  emit('submit', { ...form.value, sku_ids: [...form.value.sku_ids] })
}

/**
 * 极简 Markdown 渲染 —— 只支持标题/粗体/列表/换行。
 *
 * 安全性：先做 HTML 转义，因此任何 `<a href>` 都无法注入；且不自动链接化裸 URL，
 * 所以预览区不会出现可点击外链（HC-1）。额外再过一遍 stripUrls，
 * 把形如 https://… 的纯文本替换为 [链接已屏蔽]，与消息气泡保持同一口径。
 */
function renderMarkdown(text: string): string {
  const escaped = stripUrls(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped
    .split('\n')
    .map((line) => {
      if (line.startsWith('### ')) return `<h4>${line.slice(4)}</h4>`
      if (line.startsWith('## ')) return `<h3>${line.slice(3)}</h3>`
      if (line.startsWith('# ')) return `<h2>${line.slice(2)}</h2>`
      if (/^[-*] /.test(line)) return `<li>${line.slice(2)}</li>`
      if (!line.trim()) return ''
      return `<p>${line.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')}</p>`
    })
    .join('')
    .replace(/(<li>.*?<\/li>)+/g, (match) => `<ul>${match}</ul>`)
}
</script>

<template>
  <div class="kb-editor">
    <!-- 左侧：编辑 -->
    <section class="kb-editor__pane">
      <h3 class="kb-editor__pane-title">条目内容</h3>

      <label class="field">
        <span class="field__label">标题 <em>*</em></span>
        <el-input v-model="form.title" placeholder="一句话说明这条知识解决什么问题" maxlength="80" show-word-limit />
      </label>

      <label class="field field--grow">
        <span class="field__label">
          正文（Markdown） <em>*</em>
          <span class="field__counter" :class="`is-${lengthStatus.toLowerCase()}`">
            {{ charCount }} 字 / 目标 {{ lengthRange.min }}–{{ lengthRange.max }}
          </span>
        </span>
        <el-input
          v-model="form.content"
          type="textarea"
          :autosize="{ minRows: 12, maxRows: 24 }"
          placeholder="支持 Markdown：# 标题、**加粗**、- 列表"
        />
        <span class="field__hint" :class="`is-${lengthStatus.toLowerCase()}`">{{ lengthHint }}</span>
      </label>
    </section>

    <!-- 右侧：元数据 + 预览 -->
    <section class="kb-editor__pane kb-editor__pane--side">
      <h3 class="kb-editor__pane-title">元数据</h3>

      <label class="field">
        <span class="field__label">知识类型 <em>*</em></span>
        <el-select v-model="form.kb_type" class="field__control">
          <el-option v-for="type in KB_TYPES" :key="type" :label="KB_TYPE_LABELS[type]" :value="type" />
        </el-select>
      </label>

      <label class="field">
        <span class="field__label">分类 <em>*</em></span>
        <el-input v-model="form.category" placeholder="如：洗护保养 / 退换货政策" />
      </label>

      <label class="field">
        <span class="field__label">权威来源 <em>*</em></span>
        <el-input v-model="form.authority" placeholder="如：售后政策 4.2 条 / 商品主数据表·洗护方式字段" />
        <span class="field__hint">坐席检索时会看到该来源，用于判断可信度，必须可追溯</span>
      </label>

      <label class="field">
        <span class="field__label">适用渠道 <em>*</em></span>
        <el-select v-model="form.channel" class="field__control">
          <el-option label="全渠道" value="ALL" />
          <el-option v-for="channel in CHANNELS" :key="channel" :label="CHANNEL_LABELS[channel]" :value="channel" />
        </el-select>
      </label>

      <label class="field">
        <span class="field__label">关联 SKU（商品知识填写）</span>
        <el-input :model-value="skuInput" placeholder="多个用、分隔，如 WK-BEAR-20、WK-BEAR-15" @update:model-value="onSkuInput" />
      </label>

      <label class="field">
        <span class="field__label">生效区间</span>
        <el-date-picker
          v-model="effectiveRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          class="field__control"
        />
        <span class="field__hint">留空表示立即生效且长期有效</span>
      </label>

      <div class="preview">
        <h4 class="preview__title">正文预览</h4>
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div v-if="form.content.trim()" class="preview__body" v-html="renderMarkdown(form.content)" />
        <p v-else class="preview__empty">尚未填写正文</p>
      </div>
    </section>

    <!-- 底部操作 -->
    <footer class="kb-editor__foot">
      <span v-if="missing.length" class="kb-editor__missing">
        以下字段必填：{{ missing.join('、') }}
      </span>
      <span v-else class="kb-editor__ok">
        <template v-if="props.chunk">保存后内容变更将重置为「待审核」，并保留版本历史</template>
        <template v-else>新建条目提交后状态为「待审核」，审核通过才会进入坐席检索</template>
      </span>
      <span class="wk-spacer" />
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :disabled="!canSubmit" :loading="props.submitting" @click="submit">
        {{ props.chunk ? '保存修改' : '创建条目' }}
      </el-button>
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.kb-editor {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: $space-4;
  align-items: start;

  @media (max-width: 1200px) {
    grid-template-columns: 1fr;
  }

  &__pane {
    display: flex;
    flex-direction: column;
    gap: $space-3;
    min-width: 0;

    &--side {
      padding: $space-3;
      border: 1px solid $ink-150;
      border-radius: $radius-sm;
      background: $surface-sunken;
    }
  }

  &__pane-title {
    margin: 0;
    font-size: $text-sm;
    font-weight: 700;
    color: $ink-800;
  }

  &__foot {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: $space-3;
    padding-top: $space-3;
    border-top: 1px solid $ink-150;
  }

  &__missing {
    font-size: $text-xs;
    color: $danger-ink;
  }

  &__ok {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;

  &--grow {
    flex: 1;
  }

  &__label {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-700;

    em {
      color: $danger-500;
      font-style: normal;
    }
  }

  &__counter {
    margin-left: auto;
    font-family: $font-mono;
    font-weight: 500;

    &.is-ok {
      color: $success-ink;
    }

    &.is-too_short {
      color: $warning-ink;
    }

    &.is-too_long {
      color: $danger-ink;
    }
  }

  &__hint {
    font-size: 11px;
    color: $ink-400;

    &.is-ok {
      color: $success-ink;
    }

    &.is-too_short {
      color: $warning-ink;
    }

    &.is-too_long {
      color: $danger-ink;
    }
  }

  &__control {
    width: 100%;
  }
}

.preview {
  margin-top: $space-2;
  padding: $space-2;
  border: 1px dashed $ink-200;
  border-radius: $radius-sm;
  background: $surface;
  max-height: 260px;
  overflow-y: auto;

  &__title {
    margin: 0 0 $space-1;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-500;
  }

  &__body {
    font-size: $text-sm;
    line-height: 1.75;
    color: $ink-800;

    :deep(h2),
    :deep(h3),
    :deep(h4) {
      margin: $space-2 0 $space-1;
      color: $ink-900;
    }

    :deep(h2) {
      font-size: $text-md;
    }

    :deep(h3) {
      font-size: $text-sm;
    }

    :deep(h4) {
      font-size: $text-sm;
    }

    :deep(p) {
      margin: 0 0 6px;
    }

    :deep(ul) {
      margin: 0 0 6px;
      padding-left: $space-4;
    }
  }

  &__empty {
    margin: 0;
    font-size: $text-xs;
    color: $ink-400;
  }
}
</style>
