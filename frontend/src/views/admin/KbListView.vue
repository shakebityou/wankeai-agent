<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import KbTable from '@/components/kb/KbTable.vue'
import KbEditor from '@/components/kb/KbEditor.vue'
import ReviewDrawer from '@/components/kb/ReviewDrawer.vue'
import { batchReviewKbChunks, createKbChunk, deleteKbChunk, reviewKbChunk, updateKbChunk } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'
import type { KbChunk, KbChunkPayload } from '@/types/contract'

const auth = useAuthStore()

/**
 * 知识条目管理 —— FE-05（P6）。
 *
 * 任务书：KbTable.vue + KbEditor.vue + ReviewDrawer.vue
 * DoD：「元数据字段完整率 100%」「未审核条目不出现在坐席检索结果中」
 *      「删除后可恢复」「批量审核可用」
 *
 * 设计决策：
 *   1. **权限由页面统一判定后下传给子组件**（writable / reviewable），
 *      不让每个子组件各自读 auth store —— 否则同一个权限判断会在多个组件里重复，
 *      改口径时漏改一处就会出现「看得见但点不了」的不一致。
 *   2. **编辑器与审核抽屉都是覆盖层而非独立页面** —— 运营的流程是
 *      「看列表 → 点开一条 → 审 → 回列表」，跳页面会丢失列表滚动位置与筛选条件。
 *   3. **软删只给一个入口，文案是「下架」** —— 后端把 review_status 置为 DISABLED，
 *      条目仍可恢复，所以不能对用户说「删除」。
 */
const route = useRoute()

const writable = computed(() => canDo(auth.role, 'kb.write'))
const reviewable = computed(() => canDo(auth.role, 'kb.review'))

const editorVisible = ref(false)
const editingChunk = ref<KbChunk | null>(null)
const editorSubmitting = ref(false)

const reviewVisible = ref(false)
const reviewingChunk = ref<KbChunk | null>(null)
const reviewSubmitting = ref(false)

const tableRef = ref<InstanceType<typeof KbTable> | null>(null)

/** 新建时的预填（从 URL query 带入，方便从纠错待办跳转过来） */
const preset = computed(() => {
  const sku = route.query.sku
  const category = route.query.category
  return {
    sku_id: typeof sku === 'string' ? sku : undefined,
    category: typeof category === 'string' ? category : undefined,
  }
})

function openCreate(): void {
  editingChunk.value = null
  editorVisible.value = true
}

function openEdit(chunk: KbChunk): void {
  editingChunk.value = chunk
  editorVisible.value = true
}

function openReview(chunk: KbChunk): void {
  reviewingChunk.value = chunk
  reviewVisible.value = true
}

async function submitEditor(payload: KbChunkPayload): Promise<void> {
  editorSubmitting.value = true
  try {
    if (editingChunk.value) {
      await updateKbChunk(editingChunk.value.chunk_id, payload)
    } else {
      await createKbChunk(payload)
    }
    editorVisible.value = false
    tableRef.value?.reload()
  } finally {
    editorSubmitting.value = false
  }
}

async function submitReview(payload: { decision: 'APPROVED' | 'REJECTED'; comment: string }): Promise<void> {
  if (!reviewingChunk.value) return
  reviewSubmitting.value = true
  try {
    await reviewKbChunk(reviewingChunk.value.chunk_id, {
      ...payload,
      reviewer: auth.displayName,
    })
    reviewVisible.value = false
    tableRef.value?.reload()
  } finally {
    reviewSubmitting.value = false
  }
}

async function remove(chunk: KbChunk): Promise<void> {
  await deleteKbChunk(chunk.chunk_id)
  tableRef.value?.reload()
}

async function batchReview(chunks: KbChunk[]): Promise<void> {
  const result = await batchReviewKbChunks(
    chunks.map((chunk) => chunk.chunk_id),
    { decision: 'APPROVED', comment: '批量通过', reviewer: auth.displayName },
  )
  if (result.failed.length) {
    // 部分失败：明细交给表格刷新后的重新拉取，这里先留痕
    console.warn('批量审核存在失败条目', result.failed)
  }
  tableRef.value?.reload()
}

function openVersions(chunk: KbChunk): void {
  window.location.hash = `#/admin/kb/${encodeURIComponent(chunk.chunk_id)}/versions`
}
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">知识条目</h1>
        <p class="wk-page__subtitle">
          商品知识、政策规则、FAQ 与长文档。只有「已通过」的条目会进入向量库并被坐席检索到。
        </p>
      </div>
      <div class="kb-list__actions">
        <el-button size="small" @click="tableRef?.reload()">刷新</el-button>
        <el-button v-if="writable" type="primary" size="small" @click="openCreate">新建条目</el-button>
      </div>
    </header>

    <KbTable
      ref="tableRef"
      :preset="preset"
      :writable="writable"
      :reviewable="reviewable"
      @create="openCreate"
      @edit="openEdit"
      @review="openReview"
      @batch-review="batchReview"
      @versions="openVersions"
      @removed="remove"
    />

    <el-dialog
      v-model="editorVisible"
      :title="editingChunk ? '编辑知识条目' : '新建知识条目'"
      width="1080px"
      top="4vh"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <KbEditor
        :chunk="editingChunk"
        :submitting="editorSubmitting"
        @submit="submitEditor"
        @cancel="editorVisible = false"
      />
    </el-dialog>

    <ReviewDrawer
      v-model="reviewVisible"
      :chunk="reviewingChunk"
      :submitting="reviewSubmitting"
      @submit="submitReview"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.kb-list__actions {
  display: flex;
  align-items: center;
  gap: $space-2;
}
</style>
