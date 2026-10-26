<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * 上传区 —— FE-06（P7 批量导入）。
 *
 * 任务书：UploadZone.vue（拖拽 CSV/XLSX/MD）
 * DoD：「74 张知识卡 + 126 条 FAQ 导入成功且 chunk 数与后端一致（无丢块）」
 *      「1,000 条导入进度实时可见」「支持 4 种知识类型自动识别」
 *
 * 设计决策：
 *   1. **只做客户端格式预检，不解析文件** —— 真正的切分与识别在后端（BFF）。
 *      前端解析会引出「前端切分结果与后端不一致」的口径灾难，而预览接口本来就是为此存在的。
 *   2. **明确列出支持的扩展名与体积上限** —— 运营拿着一个 .numbers 或 50MB 的 xlsx 反复试，
 *      不如一开始就说清楚。上限 10MB 与 BFF 上传超时（60s）匹配。
 *   3. **拖拽区同时是点击区** —— 大按钮 + 明确文案，减少「拖到哪儿」的犹豫。
 */
interface Props {
  /** 是否禁用（如已有预览结果正在确认） */
  disabled?: boolean
  /** 上传中 */
  uploading?: boolean
}

const props = withDefaults(defineProps<Props>(), { disabled: false, uploading: false })

const emit = defineEmits<{
  (e: 'select', file: File): void
}>()

const ACCEPT_EXT = ['.csv', '.xlsx', '.xls', '.md', '.markdown']
const ACCEPT_ATTR = ACCEPT_EXT.join(',')
const MAX_SIZE_MB = 10

const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const localError = ref('')

const hint = computed(() => `支持 ${ACCEPT_EXT.join(' / ')}，单文件不超过 ${MAX_SIZE_MB}MB`)

function extOf(name: string): string {
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index).toLowerCase() : ''
}

function validate(file: File): string {
  if (!ACCEPT_EXT.includes(extOf(file.name))) {
    return `不支持的格式「${extOf(file.name) || '无扩展名'}」，请上传 ${ACCEPT_EXT.join(' / ')} 文件`
  }
  if (file.size > MAX_SIZE_MB * 1024 * 1024) {
    return `文件体积 ${(file.size / 1024 / 1024).toFixed(1)}MB 超过 ${MAX_SIZE_MB}MB 上限，请拆分后分批导入`
  }
  if (file.size === 0) {
    return '文件内容为空，请检查后重新上传'
  }
  return ''
}

function accept(file: File): void {
  const message = validate(file)
  localError.value = message
  if (message) return
  emit('select', file)
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  if (props.disabled) return
  const file = event.dataTransfer?.files?.[0]
  if (file) accept(file)
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) accept(file)
  // 清空 value，否则同一个文件第二次选择不会触发 change
  input.value = ''
}

function openPicker(): void {
  if (props.disabled) return
  fileInput.value?.click()
}

defineExpose({ reset: () => (localError.value = '') })
</script>

<template>
  <div class="upload">
    <div
      class="upload__zone"
      :class="{ 'is-dragging': dragging, 'is-disabled': props.disabled, 'is-uploading': props.uploading }"
      @click="openPicker"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input ref="fileInput" type="file" :accept="ACCEPT_ATTR" class="upload__input" @change="onPick" />

      <div class="upload__icon">{{ props.uploading ? '⏳' : '📥' }}</div>
      <p class="upload__title">
        {{ props.uploading ? '正在解析并切分文件…' : '把文件拖到这里，或点击选择文件' }}
      </p>
      <p class="upload__hint">{{ hint }}</p>

      <ul class="upload__formats">
        <li><strong>CSV / XLSX</strong>：商品知识卡、FAQ 知识库等表格</li>
        <li><strong>MD</strong>：政策规则、长文档（洗护指南等）</li>
      </ul>
    </div>

    <el-alert v-if="localError" type="error" :title="localError" show-icon :closable="false" class="upload__error" />

    <el-alert
      type="warning"
      title="导入前必看切分预览"
      description="系统会先按知识类型切分并标出超长 / 过短的单元，人工确认后才真正写入知识库。未经确认的内容不会进入检索。"
      show-icon
      :closable="false"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.upload {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__zone {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: $space-2;
    padding: $space-6 $space-4;
    border: 2px dashed $ink-200;
    border-radius: $radius-md;
    background: $surface-sunken;
    cursor: pointer;
    transition: border-color 0.15s ease, background 0.15s ease;

    &:hover:not(.is-disabled) {
      border-color: $brand-400;
      background: $brand-50;
    }

    &.is-dragging {
      border-color: $brand-500;
      background: $brand-50;
    }

    &.is-disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }

    &.is-uploading {
      cursor: progress;
    }
  }

  &__input {
    display: none;
  }

  &__icon {
    font-size: 30px;
    line-height: 1;
  }

  &__title {
    margin: 0;
    font-size: $text-md;
    font-weight: 600;
    color: $ink-800;
  }

  &__hint {
    margin: 0;
    font-size: $text-xs;
    color: $ink-500;
  }

  &__formats {
    margin: $space-1 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    color: $ink-500;
    text-align: center;

    strong {
      color: $ink-700;
    }
  }

  &__error {
    margin: 0;
  }
}
</style>
