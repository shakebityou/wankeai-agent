<script setup lang="ts">
import { computed, ref } from 'vue'
import { CHUNK_LENGTH_RANGE, KB_TYPE_LABELS, type ImportPreviewResponse } from '@/types/contract'

/**
 * 切分预览 —— FE-06（P7）。
 *
 * 任务书：SplitPreview.vue（导入前必看）
 * DoD：「74 张知识卡 + 126 条 FAQ 导入成功且 chunk 数与后端一致（无丢块）」
 *      「正确标记超长 / 过短块」「支持 4 种知识类型自动识别」
 *
 * 设计决策：
 *   1. **切分单元默认全部勾选，但可以逐条剔除** —— 这是「导入前必看」的核心交互。
 *      运营发现某个单元被切坏了（比如商品描述里的材质表被切断），
 *      应该能直接把它排除掉而不是放弃整个文件重做。
 *   2. **超长 / 过短的单元置顶** —— 文件可能有几百个单元，
 *      需要人工判断的只有异常的那几个，正常的不需要逐个看。
 *   3. **类型识别置信度必须显示** —— 自动识别会出错（商品表被认成 FAQ），
 *      运营看到「FAQ 0.42 / PRODUCT 0.38」才会意识到需要手动改类型。
 *   4. **重叠比例与切分区间同屏展示** —— 让运营理解「为什么这条被切成两段」。
 */
interface Props {
  preview: ImportPreviewResponse
  /** 提交中 */
  submitting?: boolean
}

const props = withDefaults(defineProps<Props>(), { submitting: false })

const emit = defineEmits<{
  (e: 'commit', excludedIndexes: number[]): void
  (e: 'reset'): void
}>()

/** 被剔除的单元下标 */
const excluded = ref<Set<number>>(new Set())

/** 仅看异常单元 */
const onlyAbnormal = ref(false)

const range = computed(() => CHUNK_LENGTH_RANGE[props.preview.detected_type])

const abnormalCount = computed(
  () => props.preview.chunks.filter((chunk) => chunk.length_flag !== 'OK').length,
)

/** 异常置顶 —— 需要人工判断的排前面，正常单元按原顺序 */
const orderedChunks = computed(() => {
  const list = props.preview.chunks
  const sorted = [...list].sort((a, b) => {
    const aAbnormal = a.length_flag !== 'OK' ? 0 : 1
    const bAbnormal = b.length_flag !== 'OK' ? 0 : 1
    return aAbnormal - bAbnormal || a.index - b.index
  })
  return onlyAbnormal.value ? sorted.filter((chunk) => chunk.length_flag !== 'OK') : sorted
})

const includedCount = computed(() => props.preview.total_chunks - excluded.value.size)

const flagMeta: Record<string, { tone: string; label: string }> = {
  OK: { tone: 'ok', label: '长度合适' },
  TOO_SHORT: { tone: 'warn', label: '过短' },
  TOO_LONG: { tone: 'bad', label: '超长' },
}

function toggle(index: number): void {
  const next = new Set(excluded.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  excluded.value = next
}

function excludeAllAbnormal(): void {
  const next = new Set(excluded.value)
  for (const chunk of props.preview.chunks) {
    if (chunk.length_flag !== 'OK') next.add(chunk.index)
  }
  excluded.value = next
}

function includeAll(): void {
  excluded.value = new Set()
}

function commit(): void {
  emit('commit', [...excluded.value].sort((a, b) => a - b))
}

const confidencePct = (value: number): string => `${(value * 100).toFixed(0)}%`
</script>

<template>
  <div class="preview">
    <!-- 概览 -->
    <div class="preview__summary">
      <div class="preview__stat">
        <span class="preview__stat-label">文件名</span>
        <span class="preview__stat-value">{{ props.preview.file_name }}</span>
      </div>
      <div class="preview__stat">
        <span class="preview__stat-label">识别类型</span>
        <span class="preview__stat-value">
          {{ KB_TYPE_LABELS[props.preview.detected_type] }}
          <span class="preview__range">目标 {{ range.min }}–{{ range.max }} 字</span>
        </span>
      </div>
      <div class="preview__stat">
        <span class="preview__stat-label">切分单元</span>
        <span class="preview__stat-value wk-mono">
          {{ includedCount }} / {{ props.preview.total_chunks }} 条
        </span>
      </div>
      <div class="preview__stat">
        <span class="preview__stat-label">重叠比例</span>
        <span class="preview__stat-value wk-mono">{{ (props.preview.overlap_ratio * 100).toFixed(0) }}%</span>
      </div>
      <div class="preview__stat" :class="{ 'is-warn': abnormalCount > 0 }">
        <span class="preview__stat-label">长度异常</span>
        <span class="preview__stat-value wk-mono">{{ abnormalCount }} 条</span>
      </div>
    </div>

    <!-- 类型识别候选 -->
    <div class="preview__candidates">
      <span class="preview__candidates-label">类型识别置信度</span>
      <span v-for="candidate in props.preview.type_candidates" :key="candidate.kb_type" class="preview__candidate">
        {{ KB_TYPE_LABELS[candidate.kb_type] }}
        <span class="wk-mono">{{ confidencePct(candidate.confidence) }}</span>
      </span>
    </div>

    <!-- 警告 -->
    <el-alert
      v-for="(warning, index) in props.preview.warnings"
      :key="index"
      type="warning"
      :title="warning"
      show-icon
      :closable="false"
    />

    <!-- 操作条 -->
    <div class="preview__actions">
      <el-checkbox v-model="onlyAbnormal" label="只看长度异常单元" />
      <span class="wk-spacer" />
      <el-button size="small" :disabled="abnormalCount === 0" @click="excludeAllAbnormal">
        全部剔除异常（{{ abnormalCount }}）
      </el-button>
      <el-button size="small" :disabled="!excluded.size" @click="includeAll">全部恢复</el-button>
    </div>

    <!-- 单元列表 -->
    <ul class="units">
      <li
        v-for="chunk in orderedChunks"
        :key="chunk.index"
        class="units__item"
        :class="{ 'is-excluded': excluded.has(chunk.index) }"
      >
        <div class="units__head">
          <el-checkbox
            :model-value="!excluded.has(chunk.index)"
            @update:model-value="toggle(chunk.index)"
          />
          <span class="units__index wk-mono">#{{ chunk.index + 1 }}</span>
          <span class="units__title">{{ chunk.title }}</span>
          <span class="units__flag" :class="`units__flag--${flagMeta[chunk.length_flag]?.tone}`">
            {{ flagMeta[chunk.length_flag]?.label }}
          </span>
          <span class="units__count wk-mono">{{ chunk.char_count }} 字</span>
          <span class="units__ref wk-mono">{{ chunk.source_ref }}</span>
        </div>
        <pre class="units__content">{{ chunk.content }}</pre>
      </li>
    </ul>

    <div v-if="!orderedChunks.length" class="units__empty">
      {{ onlyAbnormal ? '没有长度异常的切分单元' : '没有切分结果' }}
    </div>

    <!-- 底部确认 -->
    <footer class="preview__foot">
      <span class="preview__note">
        确认后将写入 {{ includedCount }} 条知识条目，初始状态为「待审核」，审核通过后才进入坐席检索。
      </span>
      <span class="wk-spacer" />
      <el-button @click="emit('reset')">重新上传</el-button>
      <el-button type="primary" :disabled="includedCount === 0" :loading="props.submitting" @click="commit">
        确认导入 {{ includedCount }} 条
      </el-button>
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.preview {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: $space-2;
    padding: $space-3;
    border-radius: $radius-sm;
    background: $surface-sunken;

    .is-warn .preview__stat-value {
      color: $warning-ink;
    }
  }

  &__stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__stat-label {
    font-size: 11px;
    color: $ink-500;
  }

  &__stat-value {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
  }

  &__range {
    margin-left: $space-1;
    font-size: 11px;
    font-weight: 400;
    color: $ink-500;
  }

  &__candidates {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
    color: $ink-600;
  }

  &__candidates-label {
    color: $ink-500;
  }

  &__candidate {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 1px 8px;
    border-radius: $radius-pill;
    background: $ink-100;
    color: $ink-800;
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: $space-2;
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

.units {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: $space-2;
  max-height: 520px;
  overflow-y: auto;

  &__item {
    padding: $space-2 $space-3;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    background: $surface;

    &.is-excluded {
      opacity: 0.5;
      background: $surface-sunken;

      .units__title {
        text-decoration: line-through;
      }
    }
  }

  &__head {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-bottom: 4px;
  }

  &__index {
    font-size: 11px;
    color: $ink-400;
  }

  &__title {
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-800;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__flag {
    flex: none;
    padding: 0 6px;
    border-radius: $radius-xs;
    font-size: 10px;

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

  &__count {
    margin-left: auto;
    flex: none;
    font-size: 11px;
    color: $ink-600;
  }

  &__ref {
    flex: none;
    font-size: 10px;
    color: $ink-400;
  }

  &__content {
    margin: 0;
    padding: $space-2;
    border-radius: $radius-xs;
    background: $surface-sunken;
    font-family: $font-sans;
    font-size: 11px;
    line-height: 1.7;
    color: $ink-600;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 96px;
    overflow: hidden;
  }

  &__empty {
    padding: $space-4;
    text-align: center;
    font-size: $text-xs;
    color: $ink-400;
  }
}
</style>
