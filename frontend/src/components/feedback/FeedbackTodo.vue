<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import PageState from '@/components/common/PageState.vue'
import { fetchFeedbackList, updateFeedback } from '@/api/kb'
import { useAsyncData } from '@/composables/useAsyncData'
import { formatDateTime, formatRelative } from '@/utils/format'
import {
  FEEDBACK_ERROR_TYPES,
  FEEDBACK_ERROR_TYPE_LABELS,
  MERCHANT_HISTORICAL_DISTRIBUTION,
  type FeedbackErrorType,
  type FeedbackItem,
} from '@/types/contract'
import type { Paged } from '@/types/bff'

/**
 * 纠错待办 —— FE-08（P9）。
 *
 * 任务书：FeedbackTodo.vue（待办 + 指派）
 * DoD：「标记到待办 ≤3 秒」「待办可指派/关闭/关联 chunk 修改」
 *      「错误类型分布可统计，可与商家历史分布对比（关键词误命中 40.5% / 话术过时 24.8% /
 *        缺少上下文 20.3% / 政策口径错误 14.4%）」
 *
 * 设计决策：
 *   1. **分布对比放在顶部** —— 这是这一页最有价值的信息：不只是「有多少待办」，
 *      而是「我们的错误结构跟商家历史相比变了没有」。
 *      如果「答非所问」占比从 20.3% 涨到 45%，说明召回出了问题，不是单条知识的问题。
 *   2. **对比用双色条而非两个独立饼图** —— 并排的双色条能直接看出每条占比的偏移方向，
 *      两个饼图需要来回对照，读图成本高得多。
 *   3. **指派是就地下拉，不弹窗** —— 主管一次要处理几十条待办，
 *      每指派一条弹一次窗是纯粹的效率损耗。
 *   4. **状态流转有明确路径** —— OPEN → ASSIGNED → RESOLVED → CLOSED，
 *      已关闭的不能再改，避免历史被篡改。
 */
interface Props {
  /** 当前操作人（写入 assignee 与 resolution 的留痕） */
  operator?: string
  /** 是否允许指派（SUPERVISOR / ADMIN） */
  assignable?: boolean
  /** 是否允许处理（KB_OPERATOR / ADMIN） */
  handleable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  operator: '',
  assignable: false,
  handleable: false,
})

const emit = defineEmits<{
  (e: 'open-chunk', chunkId: string): void
  (e: 'open-session', sessionId: string): void
}>()

const PAGE_SIZE = 20

/** 可指派的处理人 —— 与商家材料包的岗位对应 */
const ASSIGNEES = ['知识库运营·张涛', '知识库运营·刘洋', '客服主管·李敏', '客服主管·王芳']

const filters = ref({
  error_type: '' as FeedbackErrorType | '',
  status: '' as FeedbackItem['status'] | '',
  assignee: '',
})

const page = ref(1)

const query = computed(() => ({ ...filters.value, page: page.value, page_size: PAGE_SIZE }))

const { data, loading, showSkeleton, error, isEmpty, reload } = useAsyncData<Paged<FeedbackItem> | null>(
  () => fetchFeedbackList(query.value),
  { initial: null },
)

watch(query, () => reload(), { deep: true })

const items = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)

/** 当前筛选结果下的错误类型分布（百分比） */
const distribution = computed(() => {
  const counts: Record<FeedbackErrorType, number> = {
    FACT_ERROR: 0,
    OUTDATED: 0,
    POLICY_MISMATCH: 0,
    IRRELEVANT: 0,
  }
  for (const item of items.value) counts[item.error_type] += 1
  const sum = Object.values(counts).reduce((acc, value) => acc + value, 0)
  return FEEDBACK_ERROR_TYPES.map((type) => ({
    type,
    label: FEEDBACK_ERROR_TYPE_LABELS[type],
    count: counts[type],
    percent: sum ? (counts[type] / sum) * 100 : 0,
    historical: MERCHANT_HISTORICAL_DISTRIBUTION[type],
  }))
})

/** 正在提交的行 */
const pendingId = ref('')

const STATUS_LABELS: Record<FeedbackItem['status'], string> = {
  OPEN: '待指派',
  ASSIGNED: '已指派',
  RESOLVED: '已处理',
  CLOSED: '已关闭',
}

const STATUS_TONE: Record<FeedbackItem['status'], string> = {
  OPEN: 'bad',
  ASSIGNED: 'warn',
  RESOLVED: 'ok',
  CLOSED: 'neutral',
}

function onFilterChange(): void {
  page.value = 1
}

async function assign(item: FeedbackItem, assignee: string): Promise<void> {
  if (!assignee) return
  pendingId.value = item.feedback_id
  try {
    await updateFeedback(item.feedback_id, { status: 'ASSIGNED', assignee })
    await reload()
  } finally {
    pendingId.value = ''
  }
}

async function close(item: FeedbackItem): Promise<void> {
  pendingId.value = item.feedback_id
  try {
    await updateFeedback(item.feedback_id, {
      status: 'CLOSED',
      resolution: `由 ${props.operator || '当前操作人'} 确认关闭`,
    })
    await reload()
  } finally {
    pendingId.value = ''
  }
}

function resetFilters(): void {
  filters.value = { error_type: '', status: '', assignee: '' }
  page.value = 1
}

defineExpose({ reload })
</script>

<template>
  <div class="todo">
    <!-- 错误类型分布 vs 商家历史 -->
    <section class="dist">
      <header class="dist__head">
        <h3 class="dist__title">错误类型分布</h3>
        <span class="dist__hint">当前筛选结果与商家历史基线的对比</span>
      </header>
      <ul class="dist__list">
        <li v-for="entry in distribution" :key="entry.type" class="dist__item">
          <span class="dist__label">{{ entry.label }}</span>
          <span class="dist__count wk-mono">{{ entry.count }} 条</span>
          <div class="dist__bars">
            <div class="dist__bar dist__bar--current" :style="{ width: `${entry.percent}%` }" />
            <div class="dist__bar dist__bar--history" :style="{ width: `${entry.historical}%` }" />
          </div>
          <span class="dist__value wk-mono">{{ entry.percent.toFixed(1) }}%</span>
          <span class="dist__history wk-mono">历史 {{ entry.historical }}%</span>
        </li>
      </ul>
      <p class="dist__legend">
        <span class="dist__legend-item dist__legend-item--current">当前占比</span>
        <span class="dist__legend-item dist__legend-item--history">商家历史基线</span>
        <span class="dist__note">
          若某类占比明显偏离历史基线，通常指向召回或话术的系统性问题，而不只是单条知识需要修改。
        </span>
      </p>
    </section>

    <!-- 筛选 -->
    <div class="filters">
      <el-select v-model="filters.error_type" placeholder="错误类型" clearable class="filters__select" @change="onFilterChange">
        <el-option
          v-for="type in FEEDBACK_ERROR_TYPES"
          :key="type"
          :label="FEEDBACK_ERROR_TYPE_LABELS[type]"
          :value="type"
        />
      </el-select>
      <el-select v-model="filters.status" placeholder="处理状态" clearable class="filters__select" @change="onFilterChange">
        <el-option label="待指派" value="OPEN" />
        <el-option label="已指派" value="ASSIGNED" />
        <el-option label="已处理" value="RESOLVED" />
        <el-option label="已关闭" value="CLOSED" />
      </el-select>
      <el-select v-model="filters.assignee" placeholder="处理人" clearable class="filters__select" @change="onFilterChange">
        <el-option v-for="name in ASSIGNEES" :key="name" :label="name" :value="name" />
      </el-select>
      <el-button @click="resetFilters">重置</el-button>
      <span class="wk-spacer" />
      <span class="filters__total">共 {{ total }} 条待办</span>
    </div>

    <PageState v-if="showSkeleton" state="loading" title="正在加载纠错待办" />
    <PageState
      v-else-if="error"
      state="error"
      title="纠错待办加载失败"
      :detail="error.message"
      retryable
      @retry="reload"
    />
    <PageState
      v-else-if="isEmpty"
      state="empty"
      title="没有符合条件的纠错待办"
      description="坐席在会话中标记「这条答错了」后，待办会出现在这里。"
    />

    <ul v-else v-loading="loading" class="list">
      <li v-for="item in items" :key="item.feedback_id" class="list__item">
        <header class="list__head">
          <span class="list__id wk-mono">{{ item.feedback_id }}</span>
          <span class="wk-pill" :class="`wk-pill--${STATUS_TONE[item.status]}`">
            {{ STATUS_LABELS[item.status] }}
          </span>
          <span class="list__type">{{ FEEDBACK_ERROR_TYPE_LABELS[item.error_type] }}</span>
          <span class="wk-spacer" />
          <span class="list__time">{{ formatRelative(item.submitted_at) }}</span>
        </header>

        <p class="list__comment">{{ item.comment }}</p>

        <div class="list__meta">
          <span>提交人：{{ item.submitted_by }}</span>
          <span>提交时间：<span class="wk-mono">{{ formatDateTime(item.submitted_at) }}</span></span>
          <span v-if="item.assignee">处理人：{{ item.assignee }}</span>
          <span v-if="item.resolution">处理说明：{{ item.resolution }}</span>
        </div>

        <div class="list__refs">
          <template v-if="item.chunk_ids.length">
            <span class="list__refs-label">关联知识条目</span>
            <el-button
              v-for="chunkId in item.chunk_ids"
              :key="chunkId"
              link
              type="primary"
              size="small"
              @click="emit('open-chunk', chunkId)"
            >
              {{ chunkId }}
            </el-button>
          </template>
          <span v-else class="list__refs-none">该条回复未命中知识条目（需检查召回或模型输出）</span>
          <span class="wk-spacer" />
          <el-button link size="small" @click="emit('open-session', item.session_id)">
            查看会话 {{ item.session_id }}
          </el-button>
        </div>

        <footer class="list__foot">
          <template v-if="item.status === 'CLOSED'">
            <span class="list__closed">已关闭，不可再变更</span>
          </template>
          <template v-else>
            <el-select
              v-if="props.assignable && item.status !== 'RESOLVED'"
              :model-value="item.assignee ?? ''"
              placeholder="指派给"
              size="small"
              class="list__assign"
              :disabled="pendingId === item.feedback_id"
              @update:model-value="assign(item, $event as string)"
            >
              <el-option v-for="name in ASSIGNEES" :key="name" :label="name" :value="name" />
            </el-select>
            <span v-if="item.status === 'ASSIGNED'" class="list__hint">
              等待 {{ item.assignee }} 修改知识条目后回填
            </span>
            <span class="wk-spacer" />
            <el-button
              size="small"
              :disabled="pendingId === item.feedback_id"
              @click="close(item)"
            >
              关闭待办
            </el-button>
          </template>
        </footer>
      </li>
    </ul>

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

.todo {
  display: flex;
  flex-direction: column;
  gap: $space-3;
}

.dist {
  padding: $space-3;
  border: 1px solid $ink-150;
  border-radius: $radius-md;
  background: $surface;

  &__head {
    display: flex;
    align-items: baseline;
    gap: $space-2;
    margin-bottom: $space-2;
  }

  &__title {
    margin: 0;
    font-size: $text-sm;
    font-weight: 700;
    color: $ink-800;
  }

  &__hint {
    font-size: 11px;
    color: $ink-500;
  }

  &__list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  &__item {
    display: grid;
    grid-template-columns: 76px 64px 1fr 60px 84px;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
  }

  &__label {
    color: $ink-800;
    font-weight: 600;
  }

  &__count,
  &__value,
  &__history {
    font-size: 11px;
    color: $ink-500;
  }

  &__value {
    text-align: right;
    color: $ink-800;
    font-weight: 600;
  }

  &__bars {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__bar {
    height: 6px;
    border-radius: $radius-pill;
    transition: width 0.3s ease;

    &--current {
      background: $brand-500;
    }

    &--history {
      background: $ink-300;
    }
  }

  &__legend {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-3;
    margin: $space-2 0 0;
    font-size: 11px;
    color: $ink-500;
  }

  &__legend-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;

    &::before {
      content: '';
      width: 10px;
      height: 6px;
      border-radius: $radius-pill;
    }

    &--current::before {
      background: $brand-500;
    }

    &--history::before {
      background: $ink-300;
    }
  }

  &__note {
    flex: 1;
    min-width: 200px;
    color: $ink-400;
  }
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: $space-2;

  &__select {
    width: 150px;
  }

  &__total {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__item {
    display: flex;
    flex-direction: column;
    gap: $space-2;
    padding: $space-3;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    background: $surface;
  }

  &__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
  }

  &__id {
    font-size: 11px;
    color: $ink-400;
  }

  &__type {
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-800;
  }

  &__time {
    font-size: 11px;
    color: $ink-500;
  }

  &__comment {
    margin: 0;
    font-size: $text-sm;
    line-height: 1.7;
    color: $ink-800;
  }

  &__meta {
    display: flex;
    flex-wrap: wrap;
    gap: $space-3;
    font-size: 11px;
    color: $ink-500;
  }

  &__refs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
    padding-top: $space-2;
    border-top: 1px dashed $ink-150;
    font-size: 11px;
  }

  &__refs-label {
    color: $ink-500;
  }

  &__refs-none {
    color: $warning-ink;
  }

  &__foot {
    display: flex;
    align-items: center;
    gap: $space-2;
    padding-top: $space-2;
    border-top: 1px dashed $ink-150;
  }

  &__assign {
    width: 168px;
  }

  &__hint {
    font-size: 11px;
    color: $ink-500;
  }

  &__closed {
    font-size: 11px;
    color: $ink-400;
  }
}

.pager {
  display: flex;
  justify-content: flex-end;
}
</style>
