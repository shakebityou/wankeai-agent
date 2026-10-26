<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchTickets } from '@/api/kb'
import { formatDuration, formatRelative } from '@/utils/format'
import {
  HANDOFF_REASONS,
  HANDOFF_REASON_LABELS,
  PRIORITIES,
  PRIORITY_LABELS,
  TICKET_STATUSES,
  TICKET_STATUS_LABELS,
  type HandoffReason,
  type Priority,
  type TicketListQuery,
  type TicketStatus,
  type TicketSummary,
} from '@/types/contract'

/**
 * 工单列表 —— FE-02（P5）。
 *
 * 任务书 DoD：
 *   「工单列表按 reason/priority/status 筛选」
 *   「工单列表分页 50 条/页」
 *
 * 设计决策 —— 排序为什么不交给用户点表头：
 *   BFF 已经按「SLA 超时 → 优先级 → 等待时长」排好序，这是**业务排序**而非
 *   任意排序：超时未处理的工单必须先被看见。若让用户能按工单号重排，
 *   就等于允许把最该处理的工单藏到第 3 页，这与工单台的使命相反。
 *   因此这里只展示 BFF 的排序结果，并提供筛选缩小范围。
 *
 * 展示口径（4.5.2）：工单号、买家标识均为后端已脱敏值，前端不再加工。
 */
const props = withDefaults(
  defineProps<{
    /** 预置筛选（从会话详情跳转时带上 reason / keyword） */
    preset?: Partial<TicketListQuery>
    /** 行点击 —— 交给父级决定是跳详情还是内嵌展示 */
    selectable?: boolean
  }>(),
  { preset: () => ({}), selectable: true },
)

const emit = defineEmits<{ select: [ticketNo: string] }>()

const PAGE_SIZE = 50

const filters = reactive({
  reason: (props.preset.reason ?? '') as HandoffReason | '',
  priority: (props.preset.priority ?? '') as Priority | '',
  status: (props.preset.status ?? '') as TicketStatus | '',
  keyword: props.preset.keyword ?? '',
})

const page = ref(1)
const rows = ref<TicketSummary[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)

const query = computed<TicketListQuery>(() => ({
  reason: filters.reason || undefined,
  priority: filters.priority || undefined,
  status: filters.status || undefined,
  keyword: filters.keyword.trim() || undefined,
  page: page.value,
  page_size: PAGE_SIZE,
}))

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const result = await fetchTickets(query.value)
    rows.value = result.items
    total.value = result.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '工单列表加载失败'
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

/** 筛选变化必须回到第 1 页，否则会出现「第 3 页无结果」的错觉 */
function applyFilter(): void {
  page.value = 1
  void load()
}

function resetFilter(): void {
  filters.reason = ''
  filters.priority = ''
  filters.status = ''
  filters.keyword = ''
  applyFilter()
}

watch(page, () => void load())
void load()

/**
 * 行高亮与行点击必须在 script 里定义。
 * 原因：Vue 模板表达式不支持 TS 类型标注（`({ row }: { row: TicketSummary }) => ...`
 * 会被模板编译器当成语法错误），类型化逻辑只能放在 <script setup> 中。
 */
function rowClassName({ row }: { row: TicketSummary }): string {
  return row.sla_breached ? 'row-breached' : ''
}

function onRowClick(row: TicketSummary): void {
  if (!props.selectable) return
  emit('select', row.ticket_no)
}

defineExpose({ reload: load })
</script>

<template>
  <div class="ticket-list">
    <!-- 筛选条 -->
    <div class="ticket-list__filters">
      <el-select
        v-model="filters.reason"
        placeholder="转人工原因"
        clearable
        size="small"
        class="filter-item"
        @change="applyFilter"
      >
        <el-option
          v-for="reason in HANDOFF_REASONS"
          :key="reason"
          :label="HANDOFF_REASON_LABELS[reason]"
          :value="reason"
        />
      </el-select>

      <el-select
        v-model="filters.priority"
        placeholder="优先级"
        clearable
        size="small"
        class="filter-item"
        @change="applyFilter"
      >
        <el-option
          v-for="priority in PRIORITIES"
          :key="priority"
          :label="PRIORITY_LABELS[priority]"
          :value="priority"
        />
      </el-select>

      <el-select
        v-model="filters.status"
        placeholder="工单状态"
        clearable
        size="small"
        class="filter-item"
        @change="applyFilter"
      >
        <el-option
          v-for="status in TICKET_STATUSES"
          :key="status"
          :label="TICKET_STATUS_LABELS[status]"
          :value="status"
        />
      </el-select>

      <el-input
        v-model="filters.keyword"
        placeholder="工单号 / 诉求关键词"
        clearable
        size="small"
        class="filter-item filter-item--wide"
        @keyup.enter="applyFilter"
        @clear="applyFilter"
      />

      <el-button size="small" type="primary" plain @click="applyFilter">筛选</el-button>
      <el-button size="small" text @click="resetFilter">重置</el-button>

      <span class="wk-spacer" />
      <span class="ticket-list__total">共 {{ total }} 条 · 每页 {{ PAGE_SIZE }} 条</span>
    </div>

    <!-- 三态 -->
    <PageState
      v-if="loading && rows.length === 0"
      state="loading"
      title="正在加载工单…"
    />
    <PageState
      v-else-if="error"
      state="error"
      :detail="error"
      retryable
      @retry="load"
    />
    <PageState
      v-else-if="rows.length === 0"
      state="empty"
      title="没有符合条件的工单"
      description="放宽筛选条件或清空关键词后重试"
    />

    <!-- 表格 -->
    <el-table
      v-else
      v-loading="loading"
      :data="rows"
      size="small"
      class="ticket-list__table"
      :row-class-name="rowClassName"
      @row-click="onRowClick"
    >
      <el-table-column label="工单号" width="132">
        <template #default="{ row }">
          <span class="wk-mono cell-ticket">{{ row.ticket_no }}</span>
        </template>
      </el-table-column>

      <el-table-column label="买家" width="118" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="cell-buyer">{{ row.buyer_display }}</span>
        </template>
      </el-table-column>

      <el-table-column label="渠道" width="92">
        <template #default="{ row }">
          <EnumTag kind="channel" :value="row.channel" />
        </template>
      </el-table-column>

      <el-table-column label="诉求" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="cell-subject">{{ row.subject }}</span>
        </template>
      </el-table-column>

      <el-table-column label="原因" width="120">
        <template #default="{ row }">
          <EnumTag kind="handoff-reason" :value="row.reason" />
        </template>
      </el-table-column>

      <el-table-column label="优先级" width="86">
        <template #default="{ row }">
          <EnumTag kind="priority" :value="row.priority" />
        </template>
      </el-table-column>

      <el-table-column label="状态" width="92">
        <template #default="{ row }">
          <EnumTag kind="ticket-status" :value="row.status" />
        </template>
      </el-table-column>

      <el-table-column label="等待" width="104">
        <template #default="{ row }">
          <span :class="{ 'cell-breached': row.sla_breached }">
            {{ formatDuration(row.waiting_seconds) }}
          </span>
          <span v-if="row.sla_breached" class="cell-breach-flag">超时</span>
        </template>
      </el-table-column>

      <el-table-column label="处理人" width="88">
        <template #default="{ row }">
          <span v-if="row.assignee">{{ row.assignee }}</span>
          <span v-else class="wk-dim">未指派</span>
        </template>
      </el-table-column>

      <el-table-column label="更新" width="96">
        <template #default="{ row }">
          <span class="wk-dim">{{ formatRelative(row.updated_at) }}</span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页：固定 50 条/页 -->
    <div v-if="total > PAGE_SIZE" class="ticket-list__pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="prev, pager, next, jumper"
        background
        small
      />
    </div>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.ticket-list {
  display: flex;
  flex-direction: column;
  gap: $space-3;
  min-height: 0;

  &__filters {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
  }

  &__total {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__table {
    width: 100%;
  }

  &__pager {
    display: flex;
    justify-content: flex-end;
    padding-top: $space-1;
  }
}

.filter-item {
  width: 132px;

  &--wide {
    width: 200px;
  }
}

.cell-ticket {
  font-size: $text-xs;
  font-weight: 600;
  color: $ink-800;
}

.cell-buyer {
  font-size: $text-sm;
}

.cell-subject {
  font-size: $text-sm;
  color: $ink-700;
}

.cell-breached {
  color: $danger-ink;
  font-weight: 600;
}

.cell-breach-flag {
  margin-left: $space-1;
  padding: 0 4px;
  border-radius: $radius-xs;
  background: $danger-bg;
  color: $danger-ink;
  font-size: 10px;
  font-weight: 600;
}

:deep(.row-breached) {
  background: rgba(217, 74, 74, 0.04);
}
</style>
