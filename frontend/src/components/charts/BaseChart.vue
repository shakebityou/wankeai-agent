<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, FunnelChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  DatasetComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'

/**
 * ECharts 通用封装 —— FE-09/FE-12 复用。
 *
 * 为什么不用 echarts 全量包：
 *   全量包 ~1MB，而本项目只需要 折线/柱状/饼/漏斗 四种图 + 基础组件。
 *   按需注册后打包体积降到百 KB 级，对「看板加载 ≤2s」的 DoD 有直接帮助。
 *
 * 为什么不用 vue-echarts：
 *   多一个依赖，而这里需要的只有「option 变化时 setOption」这一件事，
 *   自己写 20 行比引一个包更可控（尤其是 dispose 时机与 resize 监听）。
 */
echarts.use([
  LineChart,
  BarChart,
  PieChart,
  FunnelChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  DatasetComponent,
  CanvasRenderer,
])

interface Props {
  option: EChartsOption
  height?: string
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), { height: '280px', loading: false })

const container = ref<HTMLDivElement | null>(null)
/** 用 shallowRef 避免 ECharts 实例被 Vue 深度代理 —— 深层响应式会破坏图表内部状态 */
const chart = shallowRef<echarts.ECharts | null>(null)
let observer: ResizeObserver | null = null

function render(): void {
  if (!chart.value) return
  // notMerge = true：切换指标时旧 series 必须清掉，否则会残留上一条曲线
  chart.value.setOption(props.option, true)
}

onMounted(() => {
  if (!container.value) return
  chart.value = echarts.init(container.value, undefined, { renderer: 'canvas' })
  render()
  // 侧栏折叠 / 窗口缩放时图表要跟着变宽，否则右半边会留白
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(container.value)
})

watch(() => props.option, render, { deep: true })

watch(
  () => props.loading,
  (value) => {
    if (!chart.value) return
    if (value) chart.value.showLoading('default', { text: '加载中…', color: '#f2653f' })
    else chart.value.hideLoading()
  },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="container" class="chart" :style="{ height: props.height }" />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>
