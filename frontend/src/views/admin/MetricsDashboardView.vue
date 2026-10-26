<script setup lang="ts">
import { computed, ref } from 'vue'
import BaseChart from '@/components/charts/BaseChart.vue'
import MetricCard from '@/components/metrics/MetricCard.vue'
import PageState from '@/components/common/PageState.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { fetchMetricsDistribution, fetchMetricsOverview, fetchMetricsTrend } from '@/api/metrics'
import { useAsyncData } from '@/composables/useAsyncData'
import { formatMetricValue } from '@/utils/format'
import {
  type MetricsDistributionResponse,
  type MetricsOverviewResponse,
  type MetricsTrendResponse,
} from '@/types/contract'
import type { EChartsOption } from 'echarts'

/**
 * 指标看板 —— FE-09（P10）。
 *
 * 任务书 12 项验收指标 + DoD：
 *   ①12 项全部可视化且数值与后端一致；②等于目标值算达标；
 *   ③看板加载 ≤2s；④不展示任何含个人信息的明细数据（只展示聚合值）。
 *
 * 设计决策：
 *   1. **未达标置顶** —— 排序在 BFF 做（sortMetricsForDashboard：FAIL→WARNING→OK），
 *      前端不再二次排序，保证「前端看到的顺序」与「后端认为的优先级」永远一致。
 *      理由：主管打开看板的第一屏必须是最需要处理的问题，而不是「按指标编号排列」。
 *   2. **无任何明细下钻** —— 卡片不可点击、不提供「查看明细」入口。
 *      4.5.2 明确要求「只展示聚合值」，任何能点到买家/会话号的路都是合规漏洞。
 *   3. **趋势与分布独立加载** —— 概览失败不该让整页白屏，主管至少还能看到 12 个数字。
 *   4. **趋势支持按日/周切换** —— 任务书要求「趋势按日/周/渠道/意图切换」，
 *      渠道与意图分布用右侧两个图表达，日/周用粒度切换。
 */
const granularity = ref<'DAY' | 'WEEK'>('DAY')

const overview = useAsyncData<MetricsOverviewResponse | null>(
  (signal) => fetchMetricsOverview({ signal }),
  { initial: null },
)

const trend = useAsyncData<MetricsTrendResponse | null>(
  (signal) => fetchMetricsTrend({ granularity: granularity.value, days: 30 }, { signal }),
  { initial: null },
)

const distribution = useAsyncData<MetricsDistributionResponse | null>(
  (signal) => fetchMetricsDistribution({ days: 30 }, { signal }),
  { initial: null },
)

const metrics = computed(() => overview.data.value?.metrics ?? [])

/** 未达标数量 —— 给主管一句话结论 */
const alertCount = computed(() => {
  const list = metrics.value
  return {
    fail: list.filter((m) => m.status === 'FAIL').length,
    warn: list.filter((m) => m.status === 'WARNING').length,
  }
})

const rangeText = computed(() => {
  const range = overview.data.value?.range
  if (!range) return ''
  return `${range.start} 至 ${range.end}`
})

/** 趋势折线：每条指标一条线，双 Y 轴处理「百分比 vs 秒」的量纲差异 */
const trendOption = computed<EChartsOption>(() => {
  const points = trend.data.value?.points ?? []
  const ids = trend.data.value?.metric_ids ?? []
  const buckets = points.map((point) => point.bucket)

  const series = ids.map((id) => {
    const def = metrics.value.find((m) => m.metric_id === id)
    return {
      name: def?.name ?? id,
      type: 'line' as const,
      smooth: true,
      showSymbol: false,
      data: points.map((point) => point.values[id] ?? null),
      emphasis: { focus: 'series' as const },
    }
  })

  return {
    grid: { left: 8, right: 12, top: 40, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, type: 'scroll', textStyle: { fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: buckets,
      boundaryGap: false,
      axisLabel: { fontSize: 10, color: '#767c8c' },
      axisLine: { lineStyle: { color: '#e2e0dc' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { fontSize: 10, color: '#767c8c' },
      splitLine: { lineStyle: { color: '#f2f0ec' } },
    },
    series,
  }
})

/** 渠道分布：环形图 + 转人工率 */
const channelOption = computed<EChartsOption>(() => {
  const items = distribution.data.value?.channels ?? []
  return {
    tooltip: {
      trigger: 'item',
      formatter: (params: unknown) => {
        const p = params as { name: string; value: number; percent: number }
        return `${p.name}<br/>会话 ${p.value} 条（${p.percent}%）`
      },
    },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [
      {
        type: 'pie',
        radius: ['46%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        data: items.map((item) => ({ name: item.channel, value: item.sessions })),
      },
    ],
  }
})

/** 意图分布：横向柱状 + 准确率 */
const intentOption = computed<EChartsOption>(() => {
  const items = [...(distribution.data.value?.intents ?? [])].reverse()
  return {
    grid: { left: 8, right: 48, top: 12, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const list = params as Array<{ name: string; value: number }>
        const intent = list[0]?.name ?? ''
        const item = items.find((entry) => entry.intent === intent)
        if (!item) return ''
        return `${item.intent}<br/>会话 ${item.count} 条<br/>解决率 ${item.accuracy.toFixed(1)}%`
      },
    },
    xAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#767c8c' },
      splitLine: { lineStyle: { color: '#f2f0ec' } },
    },
    yAxis: {
      type: 'category',
      data: items.map((item) => item.intent),
      axisLabel: { fontSize: 11, color: '#454b57' },
      axisLine: { lineStyle: { color: '#e2e0dc' } },
    },
    series: [
      {
        type: 'bar',
        barWidth: '52%',
        itemStyle: { color: '#f2653f', borderRadius: [0, 4, 4, 0] },
        label: {
          show: true,
          position: 'right',
          fontSize: 10,
          color: '#767c8c',
          formatter: (params: unknown) => {
            const p = params as { dataIndex: number }
            const item = items[p.dataIndex]
            return item ? `${item.accuracy.toFixed(1)}%` : ''
          },
        },
        data: items.map((item) => item.count),
      },
    ],
  }
})

/** 转人工漏斗 */
const funnelOption = computed<EChartsOption>(() => {
  const stages = distribution.data.value?.funnel ?? []
  return {
    tooltip: {
      trigger: 'item',
      formatter: (params: unknown) => {
        const p = params as { name: string; value: number }
        return `${p.name}<br/>${p.value} 条`
      },
    },
    series: [
      {
        type: 'funnel',
        left: '8%',
        width: '84%',
        top: 8,
        bottom: 8,
        minSize: '32%',
        gap: 3,
        label: {
          position: 'inside',
          fontSize: 11,
          color: '#fff',
          formatter: (params: unknown) => {
            const p = params as { name: string; value: number }
            return `${p.name} ${p.value}`
          },
        },
        itemStyle: { borderColor: '#fff', borderWidth: 1 },
        color: ['#f2653f', '#e0952a', '#4a7fd9', '#3e8e7e', '#2f9e6e'],
        data: stages.map((stage) => ({ name: stage.stage, value: stage.count })),
      },
    ],
  }
})

function switchGranularity(value: 'DAY' | 'WEEK'): void {
  if (granularity.value === value) return
  granularity.value = value
  trend.reload()
}

function reloadAll(): void {
  overview.reload()
  trend.reload()
  distribution.reload()
}
</script>

<template>
  <div class="wk-page metrics-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">指标看板</h1>
        <p class="wk-page__subtitle">
          12 项验收指标的当前值与达标状态，未达标指标自动置顶
          <template v-if="rangeText"> · 统计区间 {{ rangeText }}</template>
        </p>
      </div>
      <div class="wk-row">
        <el-button :loading="overview.loading.value" @click="reloadAll">刷新</el-button>
      </div>
    </header>

    <!-- 结论摘要 -->
    <div v-if="overview.data.value" class="summary">
      <div class="summary__item" :class="alertCount.fail ? 'is-bad' : 'is-ok'">
        <span class="summary__num">{{ alertCount.fail }}</span>
        <span class="summary__label">项未达标</span>
      </div>
      <div class="summary__item" :class="alertCount.warn ? 'is-warn' : 'is-ok'">
        <span class="summary__num">{{ alertCount.warn }}</span>
        <span class="summary__label">项预警</span>
      </div>
      <div class="summary__item">
        <span class="summary__num">{{ metrics.length }}</span>
        <span class="summary__label">项指标总数</span>
      </div>
      <div class="summary__item">
        <span class="summary__num">{{ overview.data.value.sample_size }}</span>
        <span class="summary__label">样本会话数</span>
      </div>
      <div class="summary__note">
        <span v-if="overview.data.value.data_complete" class="wk-pill wk-pill--ok">数据完整</span>
        <span v-else class="wk-pill wk-pill--warn">数据不完整，结论仅供参考</span>
        <span class="wk-muted">本页只展示聚合值，不含任何买家个人信息</span>
      </div>
    </div>

    <PageState
      v-if="overview.showSkeleton.value"
      state="loading"
      title="正在加载指标数据"
      description="正在汇总 12 项验收指标的当前值与基线…"
    />
    <PageState
      v-else-if="overview.error.value"
      state="error"
      title="指标数据加载失败"
      :description="overview.error.value.message"
      retryable
      @retry="reloadAll"
    />
    <PageState
      v-else-if="overview.isEmpty.value"
      state="empty"
      title="暂无指标数据"
      description="统计周期内没有产生会话数据。"
    />

    <template v-else>
      <!-- 12 项指标卡片（顺序由后端决定：未达标置顶） -->
      <section class="cards">
        <MetricCard v-for="metric in metrics" :key="metric.metric_id" :metric="metric" />
      </section>

      <!-- 趋势 -->
      <SectionCard title="指标趋势" hint="按日或按周查看指标变化">
        <template #extra>
          <el-radio-group
            :model-value="granularity"
            size="small"
            @update:model-value="switchGranularity($event as 'DAY' | 'WEEK')"
          >
            <el-radio-button value="DAY">按日</el-radio-button>
            <el-radio-button value="WEEK">按周</el-radio-button>
          </el-radio-group>
        </template>

        <PageState
          v-if="trend.showSkeleton.value"
          state="loading"
          title="正在加载趋势"
        />
        <PageState
          v-else-if="trend.error.value"
          state="error"
          title="趋势数据加载失败"
          :description="trend.error.value.message"
          retryable
          @retry="trend.reload"
        />
        <BaseChart v-else :option="trendOption" height="300px" />
      </SectionCard>

      <!-- 分布 -->
      <div class="grid">
        <SectionCard title="渠道分布" hint="各渠道会话量与占比">
          <PageState
            v-if="distribution.showSkeleton.value"
            state="loading"
            title="正在加载渠道分布"
          />
          <PageState
            v-else-if="distribution.error.value"
            state="error"
            title="渠道分布加载失败"
            :description="distribution.error.value.message"
            retryable
            @retry="distribution.reload"
          />
          <template v-else>
            <BaseChart :option="channelOption" height="260px" />
            <ul class="rate-list">
              <li v-for="item in distribution.data.value?.channels ?? []" :key="item.channel" class="rate-list__item">
                <EnumTag kind="channel" :value="item.channel" />
                <span class="rate-list__value">{{ item.sessions }} 条</span>
                <span class="rate-list__rate">转人工 {{ formatMetricValue(item.handoff_rate, '%') }}</span>
              </li>
            </ul>
          </template>
        </SectionCard>

        <SectionCard title="意图分布" hint="各意图会话量与机器人解决率">
          <PageState
            v-if="distribution.showSkeleton.value"
            state="loading"
            title="正在加载意图分布"
          />
          <PageState
            v-else-if="distribution.error.value"
            state="error"
            title="意图分布加载失败"
            :description="distribution.error.value.message"
            retryable
            @retry="distribution.reload"
          />
          <BaseChart v-else :option="intentOption" height="300px" />
        </SectionCard>
      </div>

      <!-- 转人工漏斗 -->
      <SectionCard title="转人工漏斗" hint="从会话进入到工单关闭的各阶段转化">
        <PageState
          v-if="distribution.showSkeleton.value"
          state="loading"
          title="正在加载漏斗数据"
        />
        <PageState
          v-else-if="distribution.error.value"
          state="error"
          title="漏斗数据加载失败"
          :description="distribution.error.value.message"
          retryable
          @retry="distribution.reload"
        />
        <div v-else class="funnel">
          <BaseChart :option="funnelOption" height="240px" class="funnel__chart" />
          <ul class="funnel__list">
            <li v-for="stage in distribution.data.value?.funnel ?? []" :key="stage.stage" class="funnel__item">
              <span class="funnel__stage">{{ stage.stage }}</span>
              <span class="funnel__count wk-mono">{{ stage.count }}</span>
              <span class="funnel__conv wk-mono">{{ formatMetricValue(stage.conversion, '%') }}</span>
            </li>
          </ul>
        </div>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.metrics-page {
  display: flex;
  flex-direction: column;
  gap: $space-4;
}

.summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: $space-3;
  padding: $space-3 $space-4;
  border: 1px solid $ink-150;
  border-radius: $radius-md;
  background: $surface;

  &__item {
    display: flex;
    flex-direction: column;
    padding-right: $space-3;
    border-right: 1px solid $ink-150;

    &.is-bad .summary__num {
      color: $danger-500;
    }

    &.is-warn .summary__num {
      color: $warning-500;
    }

    &.is-ok .summary__num {
      color: $success-500;
    }
  }

  &__num {
    font-size: $text-xl;
    font-weight: 700;
    font-family: $font-mono;
    color: $ink-900;
    line-height: 1.2;
  }

  &__label {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__note {
    display: flex;
    align-items: center;
    gap: $space-2;
    margin-left: auto;
    font-size: $text-xs;
  }
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: $space-3;
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: $space-3;

  @media (max-width: 1280px) {
    grid-template-columns: 1fr;
  }
}

.rate-list {
  margin: $space-2 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;

  &__item {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: $text-xs;
  }

  &__value {
    color: $ink-700;
  }

  &__rate {
    margin-left: auto;
    font-family: $font-mono;
    color: $ink-500;
  }
}

.funnel {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: $space-3;
  align-items: center;

  @media (max-width: 1100px) {
    grid-template-columns: 1fr;
  }

  &__chart {
    min-width: 0;
  }

  &__list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  &__item {
    display: flex;
    align-items: center;
    gap: $space-2;
    padding: 4px $space-2;
    border-radius: $radius-xs;
    background: $surface-sunken;
    font-size: $text-xs;
  }

  &__stage {
    color: $ink-800;
  }

  &__count {
    margin-left: auto;
    color: $ink-900;
    font-weight: 600;
  }

  &__conv {
    flex: none;
    width: 56px;
    text-align: right;
    color: $ink-500;
  }
}
</style>
