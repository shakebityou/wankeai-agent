<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute } from 'vue-router'
import UploadZone from '@/components/kb/UploadZone.vue'
import SplitPreview from '@/components/kb/SplitPreview.vue'
import ImportProgress from '@/components/kb/ImportProgress.vue'
import { commitKbImport, fetchImportProgress, previewKbImport } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'
import { ElMessage } from 'element-plus'
import type { ImportCommitRequest, ImportPreviewResponse, ImportProgress as ImportProgressType } from '@/types/contract'

const auth = useAuthStore()

/**
 * 批量导入与切分预览 —— FE-06（P7）。
 *
 * 任务书：UploadZone.vue + SplitPreview.vue + ImportProgress.vue
 * 实现：「两步式导入（先预览切分结果，人工确认后才真导入）」
 * DoD：「74 张知识卡 + 126 条 FAQ 导入成功且 chunk 数与后端一致（无丢块）」
 *      「正确标记超长/过短块」「1,000 条导入进度实时可见」
 *      「支持 4 种知识类型自动识别」「M2 必须可用」
 *
 * 设计决策：
 *   1. **页面只有三个状态：选择文件 → 预览 → 进度**，不提供「跳过预览直接导入」——
 *      切分错了会让错误知识进向量库，而坐席是查得到、用得上的。
 *   2. **导入进度用轮询而非 SSE** —— 导入是一次性任务，不是持续会话流，
 *      轮询 + 「已写入 N 条」足够，也不需要开长连接。
 *   3. **完成后提供「去知识列表查看」而不是自动跳转** —— 运营可能还要看失败明细。
 */
const route = useRoute()
const writable = computed(() => canDo(auth.role, 'kb.write'))

type Step = 'upload' | 'preview' | 'progress'

const step = ref<Step>('upload')
const preview = ref<ImportPreviewResponse | null>(null)
const progress = ref<ImportProgressType | null>(null)
const previewing = ref(false)
const committing = ref(false)
const error = ref('')

let pollTimer: number | undefined

/** 预览：上传 → 切分预览 */
async function onPreview(file: File): Promise<void> {
  if (!writable.value) {
    ElMessage.warning('当前身份没有导入知识库的权限')
    return
  }
  previewing.value = true
  error.value = ''
  try {
    const result = await previewKbImport(file)
    preview.value = result
    step.value = 'preview'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    previewing.value = false
  }
}

/** 确认导入 */
async function onCommit(excludedIndexes: number[]): Promise<void> {
  if (!preview.value) return
  committing.value = true
  try {
    const payload: ImportCommitRequest = {
      import_id: preview.value.import_id,
      kb_type: preview.value.detected_type,
      category: '',
      authority: '',
      channel: 'ALL',
      excluded_indexes: excludedIndexes,
    }
    await commitKbImport(payload)
    step.value = 'progress'
    startPolling(preview.value.import_id)
  } finally {
    committing.value = false
  }
}

function startPolling(importId: string): void {
  stopPolling()
  void pollOnce(importId)
  pollTimer = window.setInterval(() => void pollOnce(importId), 800)
}

async function pollOnce(importId: string): Promise<void> {
  try {
    const result = await fetchImportProgress(importId)
    progress.value = result
    if (result.status !== 'RUNNING') stopPolling()
  } catch {
    stopPolling()
  }
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function reset(): void {
  stopPolling()
  preview.value = null
  progress.value = null
  step.value = 'upload'
  error.value = ''
}

/** 从 URL 带入的目标类型（知识列表「批量导入同类型」入口） */
const forcedType = computed(() => (typeof route.query.kb_type === 'string' ? route.query.kb_type : ''))

onBeforeUnmount(() => stopPolling())
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">批量导入</h1>
        <p class="wk-page__subtitle">
          支持 CSV / XLSX / Markdown。导入分两步：先预览切分结果，确认无误后才真正写入知识库。
        </p>
      </div>
      <div v-if="step !== 'upload'" class="import__actions">
        <el-button size="small" @click="reset">重新选择文件</el-button>
      </div>
    </header>

    <section class="wk-card">
      <template v-if="step === 'upload'">
        <div class="wk-card__header">
          <span class="wk-card__title">1. 选择文件</span>
        </div>
        <div class="wk-card__body">
          <UploadZone
            :disabled="!writable || previewing"
            :uploading="previewing"
            @select="onPreview"
          />
          <p v-if="error" class="import__error">{{ error }}</p>
          <p v-if="forcedType" class="import__forced">
            本次导入的目标类型已指定：{{ forcedType }}
          </p>
          <ul class="import__tips">
            <li>单次最多 10 MB；超过上限请拆分为多个文件分批导入。</li>
            <li>系统会按知识类型自动识别，并在预览中给出置信度，识别错误可手动更改。</li>
            <li>导入后的条目初始状态为「待审核」，审核通过后才会被坐席检索到。</li>
          </ul>
        </div>
      </template>

      <template v-else-if="step === 'preview' && preview">
        <div class="wk-card__header">
          <span class="wk-card__title">2. 确认切分结果</span>
          <span class="wk-card__hint">文件：{{ preview.file_name }}</span>
        </div>
        <div class="wk-card__body">
          <SplitPreview :preview="preview" :submitting="committing" @commit="onCommit" @reset="reset" />
        </div>
      </template>

      <template v-else-if="step === 'progress' && progress">
        <div class="wk-card__header">
          <span class="wk-card__title">3. 导入进度</span>
        </div>
        <div class="wk-card__body">
          <ImportProgress :progress="progress" :polling="pollTimer !== undefined" />
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.import__actions {
  display: flex;
  gap: $space-2;
}

.import__error {
  margin: $space-2 0 0;
  padding: $space-2;
  border-radius: $radius-xs;
  background: $danger-bg;
  font-size: $text-xs;
  color: $danger-ink;
}

.import__forced {
  margin: $space-2 0 0;
  font-size: $text-xs;
  color: $info-ink;
}

.import__tips {
  margin: $space-3 0 0;
  padding-left: $space-4;
  font-size: $text-xs;
  line-height: 1.9;
  color: $ink-500;
}
</style>
