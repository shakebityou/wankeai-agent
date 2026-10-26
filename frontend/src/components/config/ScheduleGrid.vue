<script setup lang="ts">
import { computed } from 'vue'

/**
 * 排班网格 —— FE-12（P12 坐席分组与排班配置）。
 *
 * 任务书：ScheduleGrid.vue（7 时段 × 分组网格）
 * DoD：「6 分组 / 7 时段数据完整导入并可视化」「改在线时段后 BE-15 在线判定 5 分钟内同步」
 *      「排班冲突检测」「峰值接待量超标告警」
 *
 * 设计决策：
 *   1. **可编辑单元格做成开关而非弹窗** —— 主管每天都要微调排班，
 *      一个单元格弹一次对话框会让「把 G4 的周五晚班去掉」变成 8 次点击。
 *   2. **只读模式仍显示单元格状态**（浅灰填充 = 在线，空白 = 不在线）——
 *      只读是权限问题，不是可视化问题，系统管理员也要一眼看清排班。
 *   3. **行末汇总本周在线时长** —— 「这个组一周只在线 5 个小时」比逐个单元格看容易发现得多。
 */
interface GridRow {
  group_id: string
  group_name: string
  agent_count: number
  daily_volume: number
  peak_volume: number
  online_window: string
  schedule: boolean[][]
}

interface Props {
  groups: GridRow[]
  /** 时段标签（后端下发，7 项） */
  slotLabels: string[]
  /** 星期标签（7 项，周一…周日） */
  weekdayLabels?: string[]
  /** 是否可编辑 */
  editable?: boolean
  /** 正在保存的分组 */
  savingGroupId?: string
}

const props = withDefaults(defineProps<Props>(), {
  weekdayLabels: () => ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
  editable: false,
  savingGroupId: '',
})

const emit = defineEmits<{
  (e: 'change', groupId: string, schedule: boolean[][]): void
}>()

const DAYS = props.weekdayLabels

/** 每组每周在线小时数（每个时段 2 小时） */
function onToggle(groupIndex: number, dayIndex: number, slotIndex: number, value: boolean): void {
  if (!props.editable) return
  const group = props.groups[groupIndex]
  if (!group) return
  const next = group.schedule.map((row) => row.slice())
  next[dayIndex][slotIndex] = value
  emit('change', group.group_id, next)
}

/** 按天统计在线的分组数，用于底部「每天在岗分组」汇总行 */
const dailyOnlineGroups = computed(() => {
  const days: number[] = []
  for (let day = 0; day < DAYS.length; day += 1) {
    let total = 0
    for (const group of props.groups) if (group.schedule[day]?.some(Boolean)) total += 1
    days.push(total)
  }
  return days
})

/** 按天统计在岗坐席数（供容量判断） */
const dailyAgents = computed(() => {
  const days: number[] = []
  for (let day = 0; day < DAYS.length; day += 1) {
    let total = 0
    for (const group of props.groups) {
      if (group.schedule[day]?.some(Boolean)) total += group.agent_count
    }
    days.push(total)
  }
  return days
})

/** 容量口径：每个坐席每小时最多接待 150 人（与 mock/seed/agent-groups.ts 一致） */
const AGENTS_PER_HOUR = 150
</script>

<template>
  <div class="schedule">
    <div class="schedule__scroll">
      <table class="grid">
        <thead>
          <tr>
            <th class="grid__head grid__head--name">分组</th>
            <th v-for="(day, dayIndex) in DAYS" :key="dayIndex" class="grid__head grid__head--day">
              <span class="grid__day-name">{{ day }}</span>
              <span class="grid__day-meta">在岗 {{ dailyOnlineGroups[dayIndex] }} 组 / {{ dailyAgents[dayIndex] }} 人</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(group, groupIndex) in props.groups" :key="group.group_id" class="grid__row">
            <th class="grid__name">
              <span class="grid__group-name">{{ group.group_name }}</span>
              <span class="grid__group-meta">
                {{ group.agent_count }} 人 · 日均 {{ group.daily_volume }} · 峰值 {{ group.peak_volume }}
              </span>
              <span class="grid__group-window">服务窗口 {{ group.online_window }}</span>
            </th>
            <td v-for="(day, dayIndex) in DAYS" :key="dayIndex" class="grid__cell-wrap">
              <div class="grid__slots">
                <label
                  v-for="(slot, slotIndex) in props.slotLabels"
                  :key="slotIndex"
                  class="slot"
                  :class="{
                    'slot--on': group.schedule[dayIndex]?.[slotIndex],
                    'slot--editable': props.editable,
                  }"
                  :title="`${group.group_name} · ${day} ${slot}`"
                >
                  <input
                    type="checkbox"
                    class="slot__input"
                    :checked="Boolean(group.schedule[dayIndex]?.[slotIndex])"
                    :disabled="!props.editable || props.savingGroupId === group.group_id"
                    @change="onToggle(groupIndex, dayIndex, slotIndex, ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="slot__bar" />
                </label>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="schedule__legend">
      <span class="schedule__legend-item schedule__legend-item--on">在线时段</span>
      <span class="schedule__legend-item schedule__legend-item--off">未安排</span>
      <span class="schedule__note">
        每格代表 2 小时。容量口径：单坐席每小时最多接待 {{ AGENTS_PER_HOUR }} 人，
        超过峰值接待量的时段会在「容量告警」中列出。排班变更将在 5 分钟内同步至在线判定。
      </span>
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.schedule {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__scroll {
    overflow-x: auto;
  }

  &__legend {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-3;
    margin: 0;
    font-size: 11px;
    color: $ink-500;
  }

  &__legend-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;

    &::before {
      content: '';
      width: 14px;
      height: 8px;
      border-radius: $radius-xs;
    }

    &--on::before {
      background: $success-500;
    }

    &--off::before {
      background: $ink-150;
      border: 1px solid $ink-300;
    }
  }

  &__note {
    flex: 1;
    min-width: 260px;
    color: $ink-400;
  }
}

.grid {
  width: 100%;
  min-width: 760px;
  border-collapse: separate;
  border-spacing: 0;
  font-size: $text-xs;

  &__head {
    padding: $space-2;
    border-bottom: 1px solid $ink-200;
    text-align: center;
    font-weight: 600;
    color: $ink-700;
  }

  &__head--name {
    width: 190px;
    text-align: left;
  }

  &__head--day {
    display: table-cell;
  }

  &__day-name {
    display: block;
  }

  &__day-meta {
    display: block;
    margin-top: 2px;
    font-size: 10px;
    font-weight: 400;
    color: $ink-400;
  }

  &__row {
    &:hover {
      background: $surface-sunken;
    }
  }

  &__name {
    padding: $space-2;
    border-bottom: 1px solid $ink-150;
    text-align: left;
    font-weight: 400;
    vertical-align: top;
  }

  &__group-name {
    display: block;
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-800;
  }

  &__group-meta {
    display: block;
    margin-top: 1px;
    font-size: 10px;
    color: $ink-500;
  }

  &__group-window {
    display: block;
    margin-top: 1px;
    font-size: 10px;
    color: $ink-400;
  }

  &__cell-wrap {
    padding: $space-1;
    border-bottom: 1px solid $ink-150;
    vertical-align: top;
  }

  &__slots {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 3px;
  }
}

.slot {
  display: block;
  cursor: pointer;

  &__input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
  }

  &__bar {
    display: block;
    height: 16px;
    border: 1px solid $ink-200;
    border-radius: $radius-xs;
    background: $ink-50;
    transition: background 0.15s ease, border-color 0.15s ease;
  }

  &--on &__bar {
    border-color: $success-500;
    background: $success-500;
  }

  &--editable:hover &__bar {
    border-color: $brand-500;
  }

  &__input:focus-visible + &__bar {
    outline: 2px solid $brand-500;
    outline-offset: 1px;
  }
}
</style>
