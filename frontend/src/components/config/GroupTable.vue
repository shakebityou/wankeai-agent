<script setup lang="ts">
import { computed } from 'vue'
import type { AgentGroup } from '@/types/contract'

/**
 * 排班冲突 —— 与 mock/seed/agent-groups.ts 的 detectConflicts() 输出同构。
 * 这里在本文件内定义而非从 mock 目录导入：mock 不在 src 下，
 * 组件不应依赖 mock 侧的类型路径（BFF 接入后此结构由接口下发）。
 */
interface ScheduleConflict {
  group_id: string
  group_name: string
  weekday: number
  slot: number
  type: 'NO_AGENT' | 'OVER_CAPACITY'
  detail: string
}

/**
 * 坐席分组表 —— FE-12（P12）。
 *
 * 任务书：GroupTable.vue
 * 依据：商家材料包 data\客服坐席分组表.csv（6 分组）
 *       + 客服组织结构表.xlsx（4 Sheet）
 * DoD：「6 分组 / 7 时段数据完整导入并可视化」
 *
 * 设计决策：
 *   1. **冲突与容量告警不占一整块页面** —— 做成表格顶部的告警条，
 *      因为主管看分组表时的真实问题是「这排班能不能接住今天的量」，
 *      而不是「我想看几个冲突数字」。
 *   2. **峰值接待量与在岗人数的比值用色阶** —— 负载率超 100% 意味着
 *      峰值时段坐席接不过来，这是排班唯一真正需要盯的数字。
 */
interface Props {
  groups: AgentGroup[]
  /** 服务窗口原文（「09:00–22:00」），mock 侧多带的字段 */
  windows?: Record<string, string>
  /** 排班冲突 */
  conflicts?: ScheduleConflict[]
  /** 是否允许编辑 */
  editable?: boolean
  /** 正在保存的分组 */
  savingGroupId?: string
}

const props = withDefaults(defineProps<Props>(), {
  windows: () => ({}),
  conflicts: () => [],
  editable: false,
  savingGroupId: '',
})

const emit = defineEmits<{
  (e: 'edit', group: AgentGroup): void
}>()

/** 单坐席每小时最多接待 150 人 */
const AGENTS_PER_HOUR = 150

/**
 * 每个分组的峰值负载率。
 * 口径：峰值接待量 / (坐席人数 × 每坐席每小时接待量 × 峰值小时数估算)
 * 这里用更保守的口径：峰值接待量 ÷（坐席人数 × 150 × 2）—— 假设峰值集中在 2 小时内。
 */
function loadRate(group: AgentGroup): number {
  const capacity = group.agent_count * AGENTS_PER_HOUR * 2
  if (capacity <= 0) return 0
  return (group.peak_volume / capacity) * 100
}

function loadTone(rate: number): 'ok' | 'warn' | 'bad' {
  if (rate > 100) return 'bad'
  if (rate > 80) return 'warn'
  return 'ok'
}

const rows = computed(() =>
  props.groups.map((group) => ({
    group,
    rate: loadRate(group),
    window: props.windows[group.group_id] ?? '—',
    onlineSlots: group.schedule.flat().filter(Boolean).length,
    conflicts: props.conflicts.filter((conflict) => conflict.group_id === group.group_id),
  })),
)

const conflictTotal = computed(() => props.conflicts.length)
const overloaded = computed(() => rows.value.filter((row) => loadTone(row.rate) === 'bad'))
</script>

<template>
  <div class="groups">
    <el-alert
      v-if="conflictTotal"
      type="warning"
      show-icon
      :closable="false"
      :title="`检测到 ${conflictTotal} 处排班冲突，请调整后再保存`"
    >
      <ul class="groups__conflict-list">
        <li v-for="(conflict, index) in props.conflicts.slice(0, 6)" :key="index">
          {{ conflict.group_name }} · {{ ['周一','周二','周三','周四','周五','周六','周日'][conflict.weekday] }}
          {{ ['08:00–10:00','10:00–12:00','12:00–14:00','14:00–16:00','16:00–18:00','18:00–20:00','20:00–22:00'][conflict.slot] }}
          —— {{ conflict.type === 'NO_AGENT' ? '该时段无人在线' : '容量超限' }}（{{ conflict.detail }}）
        </li>
      </ul>
    </el-alert>

    <el-alert
      v-if="overloaded.length"
      type="error"
      show-icon
      :closable="false"
      :title="`${overloaded.length} 个分组的峰值接待量超过当前排班可承载容量`"
    >
      <ul class="groups__conflict-list">
        <li v-for="row in overloaded" :key="row.group.group_id">
          {{ row.group.group_name }}：峰值 {{ row.group.peak_volume }} 人 / 可承载
          {{ row.group.agent_count * 150 * 2 }} 人（负载率 {{ row.rate.toFixed(0) }}%）
        </li>
      </ul>
    </el-alert>

    <el-table :data="rows" size="small" class="groups__table">
      <el-table-column prop="group.group_id" label="编号" width="72" />

      <el-table-column label="分组" min-width="150">
        <template #default="{ row }">
          <div class="name">
            <span class="name__text">{{ (row as typeof rows[number]).group.group_name }}</span>
            <span class="name__scope">{{ (row as typeof rows[number]).group.service_scope }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="坐席" width="60">
        <template #default="{ row }">
          <span class="wk-mono">{{ (row as typeof rows[number]).group.agent_count }} 人</span>
        </template>
      </el-table-column>

      <el-table-column label="主管" width="80">
        <template #default="{ row }">
          {{ (row as typeof rows[number]).group.supervisor }}
        </template>
      </el-table-column>

      <el-table-column label="服务窗口" width="130">
        <template #default="{ row }">
          <span class="window wk-mono">{{ (row as typeof rows[number]).window }}</span>
        </template>
      </el-table-column>

      <el-table-column label="在线时段" width="84">
        <template #default="{ row }">
          <span class="slots">{{ (row as typeof rows[number]).onlineSlots }} / 49</span>
        </template>
      </el-table-column>

      <el-table-column label="服务渠道" min-width="170">
        <template #default="{ row }">
          <div class="channels">
            <span v-for="channel in (row as typeof rows[number]).group.channels" :key="channel" class="channels__item">
              {{ channel }}
            </span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="日均 / 峰值" width="118">
        <template #default="{ row }">
          <div class="volume">
            <span>{{ (row as typeof rows[number]).group.daily_volume }}</span>
            <span class="volume__peak">{{ (row as typeof rows[number]).group.peak_volume }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="峰值负载率" width="150">
        <template #default="{ row }">
          <div class="load" :class="`load--${loadTone((row as typeof rows[number]).rate)}`">
            <div class="load__bar">
              <span
                class="load__fill"
                :style="{ width: `${Math.min((row as typeof rows[number]).rate, 100)}%` }"
              />
            </div>
            <span class="load__text wk-mono">{{ (row as typeof rows[number]).rate.toFixed(0) }}%</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="备注" min-width="130">
        <template #default="{ row }">
          <span class="note">{{ (row as typeof rows[number]).group.note || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column v-if="props.editable" label="操作" width="72" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            size="small"
            :disabled="props.savingGroupId === (row as typeof rows[number]).group.group_id"
            @click="emit('edit', (row as typeof rows[number]).group)"
          >
            排班
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.groups {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__conflict-list {
    margin: $space-1 0 0;
    padding-left: $space-4;
    font-size: $text-xs;
    line-height: 1.7;
    color: $ink-700;
  }
}

.name {
  display: flex;
  flex-direction: column;
  gap: 1px;

  &__text {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-800;
  }

  &__scope {
    font-size: 10px;
    color: $ink-500;
  }
}

.window {
  font-size: 11px;
  color: $ink-700;
}

.slots {
  font-size: 11px;
  color: $ink-500;
}

.channels {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;

  &__item {
    padding: 0 5px;
    border-radius: $radius-xs;
    background: $ink-100;
    font-size: 10px;
    color: $ink-700;
  }
}

.volume {
  display: flex;
  flex-direction: column;
  font-size: 11px;
  color: $ink-700;

  &__peak {
    color: $ink-500;
  }
}

.load {
  display: flex;
  align-items: center;
  gap: $space-2;

  &__bar {
    flex: 1;
    height: 6px;
    border-radius: $radius-pill;
    background: $ink-150;
    overflow: hidden;
  }

  &__fill {
    display: block;
    height: 100%;
    border-radius: $radius-pill;
    transition: width 0.3s ease;
  }

  &__text {
    width: 38px;
    font-size: 11px;
    text-align: right;
    color: $ink-600;
  }

  &--ok &__fill {
    background: $success-500;
  }

  &--warn &__fill {
    background: $warning-500;
  }

  &--warn &__text {
    color: $warning-ink;
    font-weight: 600;
  }

  &--bad &__fill {
    background: $danger-500;
  }

  &--bad &__text {
    color: $danger-ink;
    font-weight: 700;
  }
}

.note {
  font-size: 11px;
  color: $ink-500;
}
</style>
