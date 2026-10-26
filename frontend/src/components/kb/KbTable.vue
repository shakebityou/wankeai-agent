<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { fetchKbChunks } from '@/api/kb'
import { useAsyncData } from '@/composables/useAsyncData'
import { formatDateTime } from '@/utils/format'
import {
  CHANNELS,
  CHANNEL_LABELS,
  KB_TYPES,
  KB_TYPE_LABELS,
  REVIEW_STATUSES,
  REVIEW_STATUS_LABELS,
  type KbChunk,
  type KbChunkListQuery,
  type KbType,
  type ReviewStatus,
} from '@/types/contract'
import type { Paged } from '@/types/bff'

/**
 * 知识条目表格 —— FE-05（P6 知识库列表页）。
 *
 * 任务书组件清单：KbTable.vue（筛选 category/sku_ids/review_status/channel）
 * DoD：「元数据字段完整率 100%」「未审核条目不出现在坐席检索结果中」
 *      「删除后可恢复」「批量审核可用」
 *
 * 设计决策：
 *   1. **审核状态是第一视觉层级** —— 表格里 review_status 用色块标签而不是灰色文字。
 *      理由：运营每天要看的是「哪些还压在待审核」，状态不显眼就会漏审，
 *      而漏审的直接后果是坐席检索不到知识、只能凭记忆答。
 *   2. **字数列带区间提示** —— char_count 超过该 kb_type 的目标区间时标黄/标红。
 *      理由：FE-06 的切分区间（商品 200–400 等）决定了召回质量，运营在列表里就该发现异常条目。
 *   3. **批量选择与批量审核** —— DoD 明确要求「批量审核可用」。
 *      但只允许对 PENDING 条目批量操作，已通过的条目再次审核无意义且会覆盖 reviewer。
 *   4. **删除入口文案是「下架」而非「删除」** —— 后端是软删（DISABLED）保留版本历史，
 *      文案必须与真实语义一致，否则运营会以为数据被物理删除而不敢操作。
 */
interface Props {
  /** 预设筛选（如从纠错待办跳转时带上 category） */
  preset?: Partial<KbChunkListQuery>
  /** 是否显示批量操作 */
  batchable?: boolean
  /** 是否允许写操作（KB_OPERATOR / ADMIN） */
  writable?: boolean
  /** 是否允许审核（SUPERVISOR / ADMIN） */
  reviewable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  batchable: true,
  writable: false,
  reviewable: false,
})

const emit = defineEmits<{
  (e: 'edit', chunk: KbChunk): void
  (e: 'create'): void
  (e: 'review', chunk: KbChunk): void
  (e: 'batch-review', chunks: KbChunk[]): void
  (e: 'versions', chunk: KbChunk): void
  (e: 'removed', chunk: KbChunk): void
}>()

const PAGE_SIZE = 20

const filters = ref({
  keyword: '',
  category: '',
  sku_id: '',
  review_status: '' as ReviewStatus | '',
  channel: '' as KbChunkListQuery['channel'],
  kb_type: '' as KbType | '',
})

const page = ref(1)

/** 预设筛选只在下发时生效一次，之后由用户自己控制 */
watch(
  () => props.preset,
  (preset) => {
    if (!preset) return
    filters.value = { ...filters.value, ...preset }
    page.value = 1
  },
  { immediate: true, deep: true },
)

const query = computed<KbChunkListQuery>(() => ({
  ...filters.value,
  page: page.value,
  page_size: PAGE_SIZE,
}))

const { data, loading, showSkeleton, error, isEmpty, reload } = useAsyncData<Paged<KbChunk> | null>(
  (signal) => fetchKbChunks(query.value, { signal }),
  { initial: null },
)

watch(query, () => reload(), { deep: true })

const chunks = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)

/** 仅 PENDING 可批量审核 —— 已审核条目重复操作会覆盖 reviewer 记录 */
const pendingSelected = computed(() => selected.value.filter((c) => c.review_status === 'PENDING'))

const selected = ref<KbChunk[]>([])

watch(chunks, () => {
  selected.value = []
})

/** 字数是否偏离该类型的切分区间（FE-06 口径） */
function lengthFlag(chunk: KbChunk): 'OK' | 'SHORT' | 'LONG' {
  const range = { PRODUCT: [200, 400], POLICY: [100, 300], FAQ: [50, 200], LONG_DOC: [300, 500] }[
    chunk.kb_type
  ] as [number, number] | undefined
  // 类型不在口径表里（后端新增类型或脏数据）时不做长短判定，
  // 否则 range 为 undefined，range[0] 直接抛 TypeError 白屏整张表。
  if (!range) return 'OK'
  if (chunk.char_count < range[0]) return 'SHORT'
  if (chunk.char_count > range[1]) return 'LONG'
  return 'OK'
}

function onFilterChange(): void {
  page.value = 1
}

/** 只有 PENDING 可勾选 —— 已审核条目重复提交会覆盖 reviewer 记录 */
function isSelectable(row: KbChunk): boolean {
  return row.review_status === 'PENDING'
}

/** el-table 的 selection-change 回调载荷是 any[]，在这里一次性收窄 */
function onSelectionChange(rows: KbChunk[]): void {
  selected.value = rows
}

function resetFilters(): void {
  filters.value = {
    keyword: '',
    category: '',
    sku_id: '',
    review_status: '',
    channel: '',
    kb_type: '',
  }
  page.value = 1
}

defineExpose({ reload })
</script>

<template>
  <div class="kb-table">
    <!-- 筛选条 -->
    <div class="filters">
      <el-input
        v-model="filters.keyword"
        placeholder="搜索标题 / 正文关键词"
        clearable
        class="filters__search"
        @input="onFilterChange"
      />
      <el-select v-model="filters.kb_type" placeholder="知识类型" clearable class="filters__select" @change="onFilterChange">
        <el-option v-for="type in KB_TYPES" :key="type" :label="KB_TYPE_LABELS[type]" :value="type" />
      </el-select>
      <el-select
        v-model="filters.review_status"
        placeholder="审核状态"
        clearable
        class="filters__select"
        @change="onFilterChange"
      >
        <el-option
          v-for="status in REVIEW_STATUSES"
          :key="status"
          :label="REVIEW_STATUS_LABELS[status]"
          :value="status"
        />
      </el-select>
      <el-select v-model="filters.channel" placeholder="渠道" clearable class="filters__select" @change="onFilterChange">
        <el-option label="全渠道" value="ALL" />
        <el-option v-for="channel in CHANNELS" :key="channel" :label="CHANNEL_LABELS[channel]" :value="channel" />
      </el-select>
      <el-input
        v-model="filters.sku_id"
        placeholder="SKU 编码"
        clearable
        class="filters__sku"
        @input="onFilterChange"
      />
      <el-button @click="resetFilters">重置</el-button>
      <span class="wk-spacer" />
      <el-button v-if="props.batchable && props.reviewable" :disabled="!pendingSelected.length" @click="emit('batch-review', pendingSelected)">
        批量审核（{{ pendingSelected.length }}）
      </el-button>
      <el-button v-if="props.writable" type="primary" @click="emit('create')">新增条目</el-button>
    </div>

    <div class="summary-line">
      共 {{ total }} 条 · 已选 {{ selected.length }} 条（其中待审核 {{ pendingSelected.length }} 条）
    </div>

    <PageState v-if="showSkeleton" state="loading" title="正在加载知识条目" />
    <PageState
      v-else-if="error"
      state="error"
      title="知识条目加载失败"
      :detail="error.message"
      retryable
      @retry="reload"
    />
    <PageState
      v-else-if="isEmpty"
      state="empty"
      title="没有符合条件的知识条目"
      description="可以调整筛选条件，或新增 / 批量导入知识。"
    />

    <el-table
      v-else
      v-loading="loading"
      :data="chunks"
      row-key="chunk_id"
      size="small"
      class="kb-table__table"
      @selection-change="onSelectionChange"
    >
      <el-table-column v-if="props.batchable" type="selection" width="40" :selectable="isSelectable" />
      <el-table-column label="条目标题" min-width="220">
        <template #default="{ row }">
          <div class="cell-title">
            <span class="cell-title__text">{{ (row as KbChunk).title }}</span>
            <span class="wk-mono cell-title__id">{{ (row as KbChunk).chunk_id }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="94">
        <template #default="{ row }">
          <span class="type-tag">{{ KB_TYPE_LABELS[(row as KbChunk).kb_type] }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="category" label="分类" width="110" show-overflow-tooltip />
      <el-table-column label="渠道" width="94">
        <template #default="{ row }">
          <EnumTag
            kind="channel"
            :value="(row as KbChunk).channel === 'ALL' ? 'TAOBAO' : (row as KbChunk).channel"
            :label="(row as KbChunk).channel === 'ALL' ? '全渠道' : undefined"
          />
        </template>
      </el-table-column>
      <el-table-column label="审核状态" width="104">
        <template #default="{ row }">
          <EnumTag kind="review-status" :value="(row as KbChunk).review_status" />
        </template>
      </el-table-column>
      <el-table-column label="字数" width="86" align="right">
        <template #default="{ row }">
          <span
            class="char-count wk-mono"
            :class="`char-count--${lengthFlag(row as KbChunk).toLowerCase()}`"
            :title="lengthFlag(row as KbChunk) === 'OK' ? '字数在目标区间内' : '字数偏离该知识类型的目标区间，可能影响召回'"
          >
            {{ (row as KbChunk).char_count }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="62" align="center">
        <template #default="{ row }">
          <span class="wk-mono">v{{ (row as KbChunk).version }}</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="132">
        <template #default="{ row }">
          <span class="wk-mono cell-time">{{ formatDateTime((row as KbChunk).updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="232" fixed="right">
        <template #default="{ row }">
          <div class="cell-actions">
            <el-button link type="primary" size="small" @click="emit('versions', row as KbChunk)">版本</el-button>
            <el-button v-if="props.reviewable && (row as KbChunk).review_status === 'PENDING'" link type="primary" size="small" @click="emit('review', row as KbChunk)">审核</el-button>
            <el-button v-if="props.writable" link type="primary" size="small" @click="emit('edit', row as KbChunk)">编辑</el-button>
            <el-button
              v-if="props.writable && (row as KbChunk).review_status !== 'DISABLED'"
              link
              type="danger"
              size="small"
              @click="emit('removed', row as KbChunk)"
            >
              下架
            </el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="total > PAGE_SIZE" class="pager">
      <el-pagination
        :current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="prev, pager, next, total"
        background
        @current-change="page = $event"
      />
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.kb-table {
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__table {
    width: 100%;
  }
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: $space-2;

  &__search {
    width: 220px;
  }

  &__select {
    width: 132px;
  }

  &__sku {
    width: 140px;
  }
}

.summary-line {
  font-size: $text-xs;
  color: $ink-500;
}

.cell-title {
  display: flex;
  flex-direction: column;
  gap: 1px;

  &__text {
    color: $ink-900;
    font-size: $text-sm;
  }

  &__id {
    font-size: 10px;
    color: $ink-400;
  }
}

.type-tag {
  font-size: $text-xs;
  color: $ink-700;
}

.char-count {
  font-size: $text-xs;

  &--ok {
    color: $ink-600;
  }

  &--short {
    color: $warning-ink;
    font-weight: 600;
  }

  &--long {
    color: $danger-ink;
    font-weight: 600;
  }
}

.cell-time {
  font-size: 11px;
  color: $ink-500;
}

.cell-actions {
  display: flex;
  gap: $space-1;
}

.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: $space-2;
}
</style>
