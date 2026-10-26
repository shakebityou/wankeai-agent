<script setup lang="ts">
import { computed } from 'vue'
import BaseChart from '@/components/charts/BaseChart.vue'
import type { AgentGroup } from '@/types/contract'
import type { EChartsOption } from 'echarts'

/**
 * 容量图 —— FE-12（P12）。
 *
 * 任务书：CapacityChart.vue（峰值接待量超标告警）
 * DoD：「峰值接待量超标告警」
 *
 * 设计决策：
 *   1. **同时画「日均接待量」与「峰值接待量」两条** —— 只看峰值会高估日常压力，
 *      只看日均会漏掉大促。两条并排才能看出「平时轻松、大促爆掉」的真实形态。
 *   2. **用图表而非表格** —— 主管看容量是横向比较 6 个分组，
 *      柱状图的长度差比表格里的数字差直观一个量级。
 *   3. **图表上直接标超限分组**（tooltip 里给负载率与容量），
 *      因为「G4 营销活动组峰值 2400 但只有 6 人」这个结论需要一次点击就能看到。
 */
interface Props {
  groups: AgentGroup[]
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
})

/** 单坐席每小时最多接待 150 人，假设峰值集中在 2 小时内 */
const AGENTS_PER_HOUR = 150
const PEAK_HOURS = 2

interface Row {
  group_id: string
  group_name: string
  agent_count: number
  daily_volume: number
  peak_volume: number
  capacity: number
  peakRate: number
  dailyRate: number
  overloaded: boolean
}

const rows = computed<Row[]>(() =>
  props.groups.map((group) => {
    const capacity = group.agent_count * AGENTS_PER_HOUR * PEAK_HOURS
    return {
      group_id: group.group_id,
      group_name: group.group_name,
      agent_count: group.agent_count,
      daily_volume: group.daily_volume,
      peak_volume: group.peak_volume,
      capacity,
      peakRate: capacity ? (group.peak_volume / capacity) * 100 : 0,
      dailyRate: capacity ? (group.daily_volume / (capacity * 12)) * 100 : 0,
      overloaded: group.peak_volume > capacity,
    }
  }),
)

const overloadedCount = computed(() => rows.value.filter((row) => row.overloaded).length)

const option = computed<EChartsOption>(() => ({
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow' },
    formatter: (params: unknown) => {
      const list = Array.isArray(params) ? (params as Array<{ dataIndex?: number }>) : []
      const index = list[0]?.dataIndex ?? 0
      const row = rows.value[index]
      if (!row) return ''
      return [
        `<strong>${row.group_name}</strong>（${row.agent_count} 人）`,
        `日均接待量：${row.daily_volume}`,
        `峰值接待量：${row.peak_volume}`,
        `峰值可承载：${row.capacity}`,
        `峰值负载率：${row.peakRate.toFixed(0)}%`,
        row.overloaded ? '⚠️ 峰值超出可承载容量' : '✓ 容量充足',
      ].join('<br/>')
    },
  },
  legend: {
    data: ['日均接待量', '峰值接待量'],
    top: 0,
    textStyle: { color: '#454b57', fontSize: 11 },
  },
  grid: { left: 46, right: 16, top: 34, bottom: 44 },
  xAxis: {
    type: 'category',
    data: rows.value.map((row) => row.group_name),
    axisLabel: { color: '#5a6070', fontSize: 11, interval: 0, rotate: 18 },
  },
  yAxis: {
    type: 'value',
    name: '人次',
    nameTextStyle: { color: '#767c8c', fontSize: 11 },
    axisLabel: { color: '#767c8c', fontSize: 11 },
    splitLine: { lineStyle: { color: '#f2f0ec' } },
  },
  series: [
    {
      name: '日均接待量',
      type: 'bar',
      barWidth: 12,
      data: rows.value.map((row) => ({
        value: row.daily_volume,
        itemStyle: { color: '#3e8e7e', borderRadius: [3, 3, 0, 0] },
      })),
    },
    {
      name: '峰值接待量',
      type: 'bar',
      barWidth: 12,
      data: rows.value.map((row) => ({
        value: row.peak_volume,
        itemStyle: {
          color: row.overloaded ? '#d94a4a' : '#f2653f',
          borderRadius: [3, 3, 0, 0],
        },
      })),
    },
  ],
}))
</script>

<template>
  <div class="capacity">
    <el-alert
      v-if="overloadedCount"
      type="error"
      show-icon
      :closable="false"
      :title="`${overloadedCount} 个分组的峰值接待量超出当前排班可承载容量，需要增派坐席或延长在线时段`"
    />
    <p v-else class="capacity__ok">✓ 所有分组的峰值接待量均在可承载容量内</p>

    <BaseChart :option="option" :loading="props.loading" height="300px" />

    <p class="capacity__foot">
      容量口径：单坐席每小时最多接待 150 人，假设峰值集中在 2 小时内。
      日均接待量按 12 小时服务时长折算。
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.capacity {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__ok {
    margin: 0;
    font-size: $text-xs;
    font-weight: 600;
    color: $success-ink;
  }

  &__foot {
    margin: 0;
    font-size: 11px;
    color: $ink-400;
  }
}
</style>
