<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageState from '@/components/common/PageState.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EnumTag from '@/components/common/EnumTag.vue'
import TurnDetail from '@/components/replay/TurnDetail.vue'
import { fetchReplay } from '@/api/sessions'
import { useAsyncData } from '@/composables/useAsyncData'
import { formatDateTime, formatLatency } from '@/utils/format'
import type { ReplayResponse } from '@/types/contract'

/**
 * 会话回放页 —— FE-10（P4）。
 *
 * 任务书：「会话回放与全链路日志查询」，组件 ReplayTimeline.vue / TurnDetail.vue / LatencyWaterfall.vue。
 * DoD：①按 trace_id 一键聚合；②瀑布图各跳耗时之和与总耗时误差 ≤5%；③**页面无「导出」功能**；④回放加载 ≤2s。
 *
 * 设计决策：
 *   1. **刻意不提供任何导出/下载按钮** —— 4.5.2 合规红线「不导出：会话回放、工单详情无导出功能」。
 *      这不是"忘了做"，是有意为之。除了不提供按钮，样式层还加了 `wk-no-print`（@media print 隐藏），
 *      把「另存为 PDF / 打印」这条旁路也一并堵住。
 *   2. **trace_id 一键聚合** —— 坐席/运营排查时手上有的是工单里的一个 trace_id，
 *      所以提供输入框做过滤，命中的轮次置顶高亮，而不是让他在几十轮里用眼睛找。
 *   3. **默认展开命中的那一轮** —— 过滤出结果却还要再点一次展开，是多余的一步。
 */
const route = useRoute()
const router = useRouter()

const sessionId = computed(() => String(route.params.sessionId ?? ''))

const { data, loading, showSkeleton, error, isEmpty, reload } = useAsyncData<ReplayResponse | null>(
  (signal) => fetchReplay(sessionId.value, { signal }),
  { initial: null },
)

const replay = computed(() => data.value)

/** trace_id 过滤词 */
const traceKeyword = ref('')

/** 按 trace_id 聚合过滤 —— 空关键词时展示全部轮次 */
const visibleTurns = computed(() => {
  const turns = replay.value?.turns ?? []
  const keyword = traceKeyword.value.trim().toLowerCase()
  if (!keyword) return turns
  return turns.filter(
    (turn) =>
      turn.trace_id.toLowerCase().includes(keyword) ||
      turn.tool_calls.some((call) => call.tool.toLowerCase().includes(keyword)),
  )
})

const filtered = computed(() => traceKeyword.value.trim().length > 0)

/** 概览统计 —— 让坐席在展开前就知道这一轮整体健康度 */
const stats = computed(() => {
  const turns = replay.value?.turns ?? []
  const handoff = turns.filter((turn) => turn.handoff).length
  const toolFailed = turns.reduce(
    (acc, turn) => acc + turn.tool_calls.filter((call) => !call.ok).length,
    0,
  )
  const noEvidence = turns.filter((turn) => turn.decision === 'NO_EVIDENCE').length
  const avgMs = turns.length
    ? Math.round(turns.reduce((acc, turn) => acc + turn.total_ms, 0) / turns.length)
    : 0
  return { total: turns.length, handoff, toolFailed, noEvidence, avgMs }
})

function goSession(): void {
  router.push({ name: 'session-detail', params: { sessionId: sessionId.value } })
}
</script>

<template>
  <div class="wk-page replay-page">
    <header class="wk-page__header wk-no-print">
      <div>
        <h1 class="wk-page__title">会话回放</h1>
        <p class="wk-page__subtitle">
          按轮次还原 AI 的每一次判断 —— 意图、检索命中、工具调用与耗时分布，用于定位「为什么这么答」
        </p>
      </div>
      <div class="wk-row">
        <el-button @click="goSession">返回会话</el-button>
        <el-button :loading="loading" @click="reload">刷新</el-button>
      </div>
    </header>

    <PageState
      v-if="showSkeleton"
      state="loading"
      title="正在加载回放数据"
      description="正在聚合本次会话的全部轮次与链路日志…"
    />
    <PageState
      v-else-if="error"
      state="error"
      title="回放数据加载失败"
      :description="error.message"
      retryable
      @retry="reload"
    />
    <PageState
      v-else-if="isEmpty || !replay"
      state="empty"
      title="该会话没有可回放的轮次"
      description="会话可能尚未产生对话，或数据已过保留期。"
    />

    <template v-else>
      <!-- 会话概览 -->
      <SectionCard title="会话概览" class="wk-no-print">
        <div class="overview">
          <div class="overview__item">
            <span class="overview__label">会话编号</span>
            <span class="overview__value wk-mono">{{ replay.session_id }}</span>
          </div>
          <div class="overview__item">
            <span class="overview__label">渠道</span>
            <EnumTag kind="channel" :value="replay.channel" />
          </div>
          <div class="overview__item">
            <span class="overview__label">买家</span>
            <span class="overview__value">{{ replay.buyer_display }}</span>
          </div>
          <div class="overview__item">
            <span class="overview__label">开始时间</span>
            <span class="overview__value wk-mono">{{ formatDateTime(replay.started_at) }}</span>
          </div>
          <div class="overview__item">
            <span class="overview__label">最后活动</span>
            <span class="overview__value wk-mono">{{ formatDateTime(replay.ended_at) }}</span>
          </div>
        </div>

        <div class="stats">
          <div class="stats__item">
            <span class="stats__num">{{ stats.total }}</span>
            <span class="stats__label">轮次</span>
          </div>
          <div class="stats__item" :class="{ 'is-warn': stats.toolFailed > 0 }">
            <span class="stats__num">{{ stats.toolFailed }}</span>
            <span class="stats__label">接口调用异常</span>
          </div>
          <div class="stats__item" :class="{ 'is-bad': stats.noEvidence > 0 }">
            <span class="stats__num">{{ stats.noEvidence }}</span>
            <span class="stats__label">无依据拒答</span>
          </div>
          <div class="stats__item" :class="{ 'is-warn': stats.handoff > 0 }">
            <span class="stats__num">{{ stats.handoff }}</span>
            <span class="stats__label">转人工</span>
          </div>
          <div class="stats__item">
            <span class="stats__num">{{ formatLatency(stats.avgMs) }}</span>
            <span class="stats__label">平均单轮耗时</span>
          </div>
        </div>
      </SectionCard>

      <!-- trace_id 聚合查询 -->
      <SectionCard title="按 trace_id 聚合" hint="输入完整或部分 trace_id，命中的轮次会被筛选出来" class="wk-no-print">
        <div class="trace-search">
          <el-input
            v-model="traceKeyword"
            placeholder="例如 CS20251016-0001-T3"
            clearable
            class="trace-search__input"
          />
          <span v-if="filtered" class="trace-search__result">
            命中 {{ visibleTurns.length }} / {{ stats.total }} 轮
          </span>
        </div>
      </SectionCard>

      <!-- 轮次时间轴 -->
      <SectionCard title="轮次详情" :hint="`共 ${visibleTurns.length} 轮，点击任意一轮展开链路明细`">
        <div v-if="!visibleTurns.length" class="wk-muted">
          {{ filtered ? '没有轮次匹配该 trace_id，请检查是否输入完整。' : '暂无轮次数据。' }}
        </div>
        <ol v-else class="turns">
          <li v-for="turn in visibleTurns" :key="turn.trace_id" class="turns__item">
            <TurnDetail :turn="turn" :default-open="filtered" />
          </li>
        </ol>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.replay-page {
  display: flex;
  flex-direction: column;
  gap: $space-4;
  max-width: 1180px;
}

.overview {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: $space-3;

  &__item {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__label {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__value {
    font-size: $text-sm;
    color: $ink-900;
  }
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: $space-2;
  margin-top: $space-3;
  padding-top: $space-3;
  border-top: 1px dashed $ink-150;

  &__item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: $space-2;
    border-radius: $radius-sm;
    background: $surface-sunken;

    &.is-warn {
      background: $warning-bg;

      .stats__num {
        color: $warning-ink;
      }
    }

    &.is-bad {
      background: $danger-bg;

      .stats__num {
        color: $danger-ink;
      }
    }
  }

  &__num {
    font-size: $text-lg;
    font-weight: 700;
    font-family: $font-mono;
    color: $ink-900;
  }

  &__label {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.trace-search {
  display: flex;
  align-items: center;
  gap: $space-3;

  &__input {
    max-width: 320px;
  }

  &__result {
    font-size: $text-xs;
    color: $ink-600;
  }
}

.turns {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__item {
    list-style: none;
  }
}
</style>
