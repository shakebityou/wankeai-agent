<script setup lang="ts">
import { computed } from 'vue'
import ContextCard from '@/components/context/ContextCard.vue'
import EnumTag from '@/components/common/EnumTag.vue'
import PageState from '@/components/common/PageState.vue'
import { useAsyncData } from '@/composables/useAsyncData'
import { fetchTicketDetail } from '@/api/kb'
import { formatDuration, formatRelative, formatDateTime } from '@/utils/format'
import type { TicketDetail } from '@/types/contract'

/**
 * 工单详情 —— FE-02（P5）。
 *
 * 任务书合规红线（4.5.2）：
 *   「不导出：会话回放、工单详情无导出功能」
 * 因此这个页面**刻意不提供任何导出/下载按钮**，并且整页挂 `wk-no-print`
 * 之外的关键区块也都在打印时隐藏（见 styles/index.scss 的 @media print）。
 *
 * 与 AI 上下文卡片的关系：
 *   工单里的 `context_snapshot` 是「转人工那一刻」的上下文快照，
 *   它同样可能缺字段（Mock 里 context_snapshot 为 null 就代表快照生成失败）。
 *   所以这里复用 ContextCard 的七要素守卫，而不是另写一套展示逻辑 ——
 *   快照缺字段时坐席同样会看到「上下文生成异常」而不是空白。
 */
const props = defineProps<{ ticketNo: string }>()

const { data, loading, showSkeleton, error, reload } = useAsyncData<TicketDetail | null>(
  (signal) => fetchTicketDetail(props.ticketNo, { signal }),
  { initial: null },
)

const ticket = computed(() => data.value)
</script>

<template>
  <div class="ticket-detail">
    <PageState v-if="showSkeleton" state="loading" title="正在加载工单详情…" />
    <PageState
      v-else-if="error"
      state="error"
      :detail="error.message"
      retryable
      @retry="reload"
    />
    <PageState v-else-if="!ticket" state="empty" title="工单不存在或已归档" />

    <template v-else>
      <!-- 头部：工单号 + 状态 -->
      <header class="ticket-detail__head wk-card">
        <div class="ticket-detail__head-main">
          <span class="ticket-detail__no wk-mono">{{ ticket.ticket_no }}</span>
          <EnumTag kind="ticket-status" :value="ticket.status" dot />
          <EnumTag kind="priority" :value="ticket.priority" />
          <EnumTag kind="handoff-reason" :value="ticket.reason" />
          <span v-if="ticket.sla_breached" class="wk-pill wk-pill--bad">SLA 已超时</span>
        </div>
        <p class="ticket-detail__subject">{{ ticket.subject }}</p>
      </header>

      <div class="ticket-detail__grid">
        <!-- 左栏：工单信息 + 时间线 -->
        <div class="ticket-detail__col">
          <section class="wk-card">
            <header class="wk-card__header">
              <span class="wk-card__title">工单信息</span>
            </header>
            <dl class="meta">
              <div class="meta__row">
                <dt>买家</dt>
                <dd>{{ ticket.buyer_display }}</dd>
              </div>
              <div class="meta__row">
                <dt>渠道</dt>
                <dd><EnumTag kind="channel" :value="ticket.channel" /></dd>
              </div>
              <div class="meta__row">
                <dt>所属分组</dt>
                <dd>{{ ticket.group_name }}</dd>
              </div>
              <div class="meta__row">
                <dt>处理人</dt>
                <dd>
                  <span v-if="ticket.assignee">{{ ticket.assignee }}</span>
                  <span v-else class="wk-dim">未指派</span>
                </dd>
              </div>
              <div class="meta__row">
                <dt>等待时长</dt>
                <dd :class="{ 'is-breached': ticket.sla_breached }">
                  {{ formatDuration(ticket.waiting_seconds) }}
                </dd>
              </div>
              <div class="meta__row">
                <dt>创建时间</dt>
                <dd>{{ formatDateTime(ticket.created_at) }}</dd>
              </div>
              <div class="meta__row">
                <dt>最近更新</dt>
                <dd>{{ formatRelative(ticket.updated_at) }}</dd>
              </div>
              <div class="meta__row">
                <dt>关联会话</dt>
                <dd>
                  <!-- HC-1：只展示会话号，不渲染为可点击链接 -->
                  <span class="wk-mono">{{ ticket.session_id }}</span>
                </dd>
              </div>
            </dl>
          </section>

          <section class="wk-card">
            <header class="wk-card__header">
              <span class="wk-card__title">处理时间线</span>
              <span class="wk-card__hint">{{ ticket.timeline.length }} 个节点</span>
            </header>
            <ol class="timeline">
              <li v-for="(node, index) in ticket.timeline" :key="index" class="timeline__item">
                <span class="timeline__dot" />
                <div class="timeline__body">
                  <div class="timeline__head">
                    <span class="timeline__action">{{ node.action }}</span>
                    <span class="timeline__at">{{ formatDateTime(node.at) }}</span>
                  </div>
                  <span class="timeline__actor">{{ node.actor }}</span>
                  <p v-if="node.detail" class="timeline__detail">{{ node.detail }}</p>
                </div>
              </li>
            </ol>
          </section>

          <section v-if="ticket.related_chunk_ids.length" class="wk-card">
            <header class="wk-card__header">
              <span class="wk-card__title">关联知识条目</span>
              <span class="wk-card__hint">用于纠错定位</span>
            </header>
            <div class="chunk-refs">
              <span
                v-for="chunkId in ticket.related_chunk_ids"
                :key="chunkId"
                class="wk-pill wk-pill--neutral wk-mono"
              >
                {{ chunkId }}
              </span>
            </div>
          </section>
        </div>

        <!-- 右栏：AI 上下文快照（复用七要素守卫） -->
        <div class="ticket-detail__col ticket-detail__col--aside">
          <section class="wk-card ticket-detail__snapshot">
            <header class="wk-card__header">
              <span class="wk-card__title">转人工时刻的 AI 上下文</span>
              <span class="wk-card__hint">快照</span>
            </header>
            <ContextCard
              :context="ticket.context_snapshot"
              :session-id="ticket.session_id"
              :loading="loading"
              :error="error"
              @retry="reload"
            />
          </section>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.ticket-detail {
  display: flex;
  flex-direction: column;
  gap: $space-4;
  min-height: 0;

  &__head {
    display: flex;
    flex-direction: column;
    gap: $space-2;
    padding: $space-4;
  }

  &__head-main {
    display: flex;
    align-items: center;
    gap: $space-2;
    flex-wrap: wrap;
  }

  &__no {
    font-size: $text-lg;
    font-weight: 700;
    color: $ink-900;
  }

  &__subject {
    margin: 0;
    font-size: $text-base;
    line-height: 1.6;
    color: $ink-700;
  }

  &__grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 380px;
    gap: $space-4;
    align-items: start;
  }

  &__col {
    display: flex;
    flex-direction: column;
    gap: $space-4;
    min-width: 0;
  }

  &__snapshot {
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - #{$topbar-height} - 140px);
    overflow-y: auto;
  }
}

.meta {
  margin: 0;
  padding: $space-4;
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__row {
    display: flex;
    align-items: center;
    gap: $space-3;
    font-size: $text-sm;

    dt {
      flex: none;
      width: 76px;
      color: $ink-500;
      font-size: $text-xs;
    }

    dd {
      margin: 0;
      color: $ink-800;
      min-width: 0;
    }

    .is-breached {
      color: $danger-ink;
      font-weight: 600;
    }
  }
}

.timeline {
  margin: 0;
  padding: $space-4;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__item {
    display: flex;
    gap: $space-3;
    position: relative;

    &:not(:last-child)::before {
      content: '';
      position: absolute;
      left: 4px;
      top: 14px;
      bottom: -12px;
      width: 1px;
      background: $ink-150;
    }
  }

  &__dot {
    flex: none;
    width: 9px;
    height: 9px;
    margin-top: 4px;
    border-radius: 50%;
    background: $brand-500;
    z-index: 1;
  }

  &__body {
    flex: 1;
    min-width: 0;
  }

  &__head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: $space-2;
  }

  &__action {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-800;
  }

  &__at {
    font-size: 11px;
    color: $ink-500;
  }

  &__actor {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__detail {
    margin: $space-1 0 0;
    font-size: $text-xs;
    line-height: 1.6;
    color: $ink-600;
  }
}

.chunk-refs {
  display: flex;
  flex-wrap: wrap;
  gap: $space-1;
  padding: $space-4;
}
</style>
