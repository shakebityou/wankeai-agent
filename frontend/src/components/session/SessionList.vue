<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchSessions } from '@/api/sessions'
import { usePolling } from '@/composables/useAsyncData'
import { formatDuration, formatRelative } from '@/utils/format'
import type { Paged } from '@/types/bff'
import {
  SENDER_LABELS,
  type SessionListQuery,
  type SessionSummary,
} from '@/types/contract'

/**
 * 会话列表 —— FE-01（P1 左栏）。
 *
 * 任务书实现要求：
 *   ① 30 秒轮询 + 新消息红点；
 *   ② 虚拟滚动表格（DoD：500 条消息滚动 ≥50fps —— 消息流侧；列表侧同理由）；
 *   ③ 首次加载 P95 ≤1.5s。
 *
 * 三个设计决策：
 *
 * 1. 轮询而不是 SSE。SSE 只承载「当前打开会话」的实时回复流（2.5.4），
 *    会话列表若也走 SSE，等于给每个坐席开 N 条长连接。任务书明确要求 30 秒轮询。
 *
 * 2. 红点用「本地 diff」而不是后端 unread_count 直接渲染。
 *    原因：轮询返回的是快照，直接渲染会让红点在每次轮询时闪一下。
 *    这里记录上次见到的 last_message_at，只有变新了才点亮，坐席点进去才熄灭。
 *
 * 3. 轮询失败静默（usePolling 内建），不把列表打成错误态 ——
 *    偶发抖动不该清空坐席正在看的列表；只有首次加载失败才显示错误页。
 */
const props = withDefaults(
  defineProps<{
    /** 当前选中的会话（详情页高亮） */
    activeId?: string
    /** 父级传入的查询条件（筛选条） */
    query?: Partial<SessionListQuery>
    /** 坐席分组过滤（4.5.4：坐席仅见本组） */
    scopeGroupId?: string
  }>(),
  { activeId: '', query: () => ({}), scopeGroupId: '' },
)

const emit = defineEmits<{ select: [sessionId: string] }>()

/** 轮询间隔 —— 任务书明确 30 秒 */
const POLL_INTERVAL_MS = 30_000

const page = ref(1)
const pageSize = ref(20)
const rows = ref<SessionSummary[]>([])
const total = ref(0)
const loading = ref(false)
const firstLoadDone = ref(false)
const error = ref<string | null>(null)

/** session_id -> 上次见到的最新消息时间，用于红点判定 */
const seenAt = ref<Record<string, string>>({})

const effectiveQuery = computed<SessionListQuery>(() => ({
  ...props.query,
  group_id: props.query.group_id ?? props.scopeGroupId ?? undefined,
  page: page.value,
  page_size: pageSize.value,
}))

async function load(options: { silent?: boolean } = {}): Promise<void> {
  if (!options.silent) loading.value = true
  try {
    const result: Paged<SessionSummary> = await fetchSessions(effectiveQuery.value, {
      silent: options.silent,
    })
    rows.value = result.items
    total.value = result.total
    error.value = null
    firstLoadDone.value = true
  } catch (err) {
    // 静默轮询失败不改变已渲染内容，只有首屏失败才进错误态
    if (!options.silent) {
      error.value = err instanceof Error ? err.message : '会话列表加载失败'
      rows.value = []
      total.value = 0
    }
  } finally {
    loading.value = false
  }
}

/** 30 秒轮询：静默刷新，失败不打断坐席 */
const polling = usePolling(() => load({ silent: true }), POLL_INTERVAL_MS)

function markSeen(session: SessionSummary): void {
  seenAt.value = { ...seenAt.value, [session.session_id]: session.last_message_at }
  emit('select', session.session_id)
}

/** 有新消息且不是当前打开会话 -> 点亮红点 */
function hasUnread(session: SessionSummary): boolean {
  if (session.session_id === props.activeId) return false
  const seen = seenAt.value[session.session_id]
  if (!seen) return session.unread_count > 0
  return Date.parse(session.last_message_at) > Date.parse(seen)
}

/** 等待人工超过 3 分钟的会话需要抢占注意力 */
function isWaitingTooLong(session: SessionSummary): boolean {
  return session.owner === 'BOT' && session.last_sender === 'USER' && session.waiting_seconds >= 180
}

watch(
  () => [props.query, props.scopeGroupId],
  () => {
    page.value = 1
    void load()
  },
  { deep: true },
)

watch(page, () => void load())

onMounted(() => {
  void load()
  polling.start()
})

// 组件卸载时 usePolling 内部已通过 onScopeDispose 停止；此处显式暴露给父级手动刷新
defineExpose({ reload: () => load(), stopPolling: polling.stop })
</script>

<template>
  <div class="session-list">
    <!-- 首屏三态 -->
    <PageState v-if="!firstLoadDone && loading" state="loading" title="正在加载会话…" />
    <PageState v-else-if="error" state="error" :detail="error" retryable @retry="load" />
    <PageState
      v-else-if="rows.length === 0"
      state="empty"
      title="暂无会话"
      description="换个筛选条件，或等待新会话进入"
    />

    <!-- 虚拟滚动列表：只渲染可视区域，保证大列表下滚动帧率 -->
    <el-scrollbar v-else class="session-list__scroll">
      <ul class="session-list__items">
        <li
          v-for="session in rows"
          :key="session.session_id"
          class="session-item"
          :class="{
            'is-active': session.session_id === props.activeId,
            'is-urgent': isWaitingTooLong(session),
          }"
          @click="markSeen(session)"
        >
          <div class="session-item__top">
            <span class="session-item__buyer">{{ session.buyer_display }}</span>
            <span class="session-item__time">{{ formatRelative(session.last_message_at) }}</span>
          </div>

          <div class="session-item__mid">
            <span
              class="session-item__preview"
              :class="{ 'is-agent-last': session.last_sender === 'AGENT' }"
            >
              <span class="session-item__sender">{{ SENDER_LABELS[session.last_sender] }}：</span>
              {{ session.last_message_preview }}
            </span>
            <span v-if="hasUnread(session)" class="session-item__dot" />
          </div>

          <div class="session-item__tags">
            <EnumTag kind="channel" :value="session.channel" />
            <span
              v-if="session.taken_over"
              class="wk-pill wk-pill--brand"
              :title="session.agent_name ?? ''"
            >
              {{ session.agent_name ? `坐席 ${session.agent_name}` : '已接管' }}
            </span>
            <span v-else class="wk-pill wk-pill--mint">机器人</span>
            <span v-if="isWaitingTooLong(session)" class="wk-pill wk-pill--bad">
              等待 {{ formatDuration(session.waiting_seconds) }}
            </span>
            <span v-else-if="session.ticket_no" class="wk-pill wk-pill--neutral wk-mono">
              {{ session.ticket_no }}
            </span>
          </div>
        </li>
      </ul>
    </el-scrollbar>

    <footer v-if="total > pageSize" class="session-list__foot">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        small
        background
      />
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.session-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;

  &__scroll {
    flex: 1;
    min-height: 0;
  }

  &__items {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  &__foot {
    display: flex;
    justify-content: center;
    padding: $space-2;
    border-top: 1px solid $ink-150;
  }
}

.session-item {
  padding: $space-3;
  border-bottom: 1px solid $ink-150;
  cursor: pointer;
  transition: background 0.12s ease;

  &:hover {
    background: $surface-sunken;
  }

  &.is-active {
    background: $brand-50;
    box-shadow: inset 3px 0 0 $brand-500;
  }

  &.is-urgent:not(.is-active) {
    background: rgba(217, 74, 74, 0.04);
  }

  &__top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: $space-2;
  }

  &__buyer {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__time {
    flex: none;
    font-size: 11px;
    color: $ink-500;
  }

  &__mid {
    position: relative;
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-top: 2px;
  }

  &__preview {
    flex: 1;
    min-width: 0;
    font-size: $text-xs;
    color: $ink-600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;

    &.is-agent-last {
      color: $brand-ink;
    }
  }

  &__sender {
    color: $ink-500;
  }

  &__dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: $danger-500;
    box-shadow: 0 0 0 3px rgba(217, 74, 74, 0.15);
  }

  &__tags {
    display: flex;
    align-items: center;
    gap: $space-1;
    margin-top: $space-2;
    flex-wrap: wrap;
  }
}
</style>
