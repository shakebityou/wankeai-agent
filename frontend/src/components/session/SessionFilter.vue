<script setup lang="ts">
import { reactive, watch } from 'vue'
import {
  CHANNELS,
  CHANNEL_LABELS,
  type Channel,
  type SessionListQuery,
} from '@/types/contract'

/**
 * 会话筛选条 —— FE-01。
 *
 * 任务书接口参数：筛选渠道 / 状态 / 是否已接管 / 时间范围。
 *
 * 设计决策 —— 「状态」为什么用四段式而不是三个独立开关：
 *   坐席的实际工作流是「先看谁在等我」，而不是「筛选 owner=BOT 且 last_sender=USER」。
 *   所以把最容易用错的组合固化成一个语义化选项 WAITING（等待人工），
 *   让「只看待接手会话」变成一次点击。这也是 BFF 侧 status 参数的原始设计。
 *
 * 防抖：关键词输入 300ms 防抖后才向上抛出，避免每敲一个字打一次接口。
 */
const props = withDefaults(
  defineProps<{
    /** 是否显示分组筛选（主管以上才有意义） */
    showGroup?: boolean
    /** 分组选项 */
    groups?: Array<{ group_id: string; group_name: string }>
  }>(),
  { showGroup: false, groups: () => [] },
)

const emit = defineEmits<{ change: [query: Partial<SessionListQuery>] }>()

const filters = reactive({
  channel: '' as Channel | '',
  status: 'ALL' as NonNullable<SessionListQuery['status']>,
  taken_over: null as boolean | null,
  keyword: '',
  group_id: '',
  range: [] as string[],
})

const STATUS_OPTIONS: Array<{ value: NonNullable<SessionListQuery['status']>; label: string }> = [
  { value: 'ALL', label: '全部会话' },
  { value: 'WAITING', label: '等待人工' },
  { value: 'BOT', label: '机器人接待中' },
  { value: 'AGENT', label: '人工接待中' },
]

let debounceTimer: number | null = null

function emitChange(): void {
  emit('change', {
    channel: filters.channel || undefined,
    status: filters.status,
    taken_over: filters.taken_over,
    keyword: filters.keyword.trim() || undefined,
    group_id: filters.group_id || undefined,
    start_time: filters.range[0] || undefined,
    end_time: filters.range[1] || undefined,
  })
}

function onKeywordInput(): void {
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => {
    debounceTimer = null
    emitChange()
  }, 300)
}

function reset(): void {
  filters.channel = ''
  filters.status = 'ALL'
  filters.taken_over = null
  filters.keyword = ''
  filters.group_id = ''
  filters.range = []
  emitChange()
}

// 下拉类筛选立即生效；关键词走防抖
watch(
  () => [filters.channel, filters.status, filters.taken_over, filters.group_id, filters.range],
  () => emitChange(),
)

// 初始抛一次，让父级拿到默认查询条件
emitChange()
</script>

<template>
  <div class="session-filter">
    <el-select
      v-model="filters.status"
      size="small"
      class="session-filter__item"
    >
      <el-option
        v-for="option in STATUS_OPTIONS"
        :key="option.value"
        :label="option.label"
        :value="option.value"
      />
    </el-select>

    <el-select
      v-model="filters.channel"
      placeholder="全部渠道"
      clearable
      size="small"
      class="session-filter__item"
    >
      <el-option
        v-for="channel in CHANNELS"
        :key="channel"
        :label="CHANNEL_LABELS[channel]"
        :value="channel"
      />
    </el-select>

    <el-select
      v-if="props.showGroup"
      v-model="filters.group_id"
      placeholder="全部分组"
      clearable
      size="small"
      class="session-filter__item"
    >
      <el-option
        v-for="group in props.groups"
        :key="group.group_id"
        :label="group.group_name"
        :value="group.group_id"
      />
    </el-select>

    <el-select
      v-model="filters.taken_over"
      placeholder="是否已接管"
      clearable
      size="small"
      class="session-filter__item session-filter__item--narrow"
    >
      <el-option label="已接管" :value="true" />
      <el-option label="未接管" :value="false" />
    </el-select>

    <el-date-picker
      v-model="filters.range"
      type="datetimerange"
      size="small"
      range-separator="至"
      start-placeholder="开始时间"
      end-placeholder="结束时间"
      value-format="YYYY-MM-DDTHH:mm:ss"
      class="session-filter__range"
    />

    <el-input
      v-model="filters.keyword"
      placeholder="买家 / 会话号 / 关键词"
      clearable
      size="small"
      class="session-filter__keyword"
      @input="onKeywordInput"
      @keyup.enter="emitChange"
      @clear="emitChange"
    />

    <el-button size="small" text @click="reset">重置</el-button>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.session-filter {
  display: flex;
  align-items: center;
  gap: $space-2;
  flex-wrap: wrap;

  &__item {
    width: 128px;

    &--narrow {
      width: 112px;
    }
  }

  &__range {
    width: 340px;
  }

  &__keyword {
    width: 190px;
  }
}
</style>
