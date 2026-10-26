<script setup lang="ts">
/**
 * 三态占位组件 —— 任务书 4.6 DoD「UI 走查通过（含空态/错误态/加载态三态）」。
 *
 * 统一三态的原因：坐席台有 5 个列表 + 8 个后台页，如果每页各写一套
 * 「转圈 / 无数据 / 报错」文案与留白，视觉会立刻散掉。
 */
interface Props {
  /** 状态类型 */
  state: 'loading' | 'empty' | 'error'
  /** 标题，留空则按 state 取默认文案 */
  title?: string
  /** 补充说明 */
  description?: string
  /** 错误详情（BFF 返回的 message） */
  detail?: string
  /** 重试回调，error 态显示按钮 */
  retryable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  description: '',
  detail: '',
  retryable: false,
})

const emit = defineEmits<{ retry: [] }>()

const PRESET = {
  loading: { icon: '⏳', title: '正在加载…', desc: '' },
  empty: { icon: '🗂️', title: '暂无数据', desc: '换个筛选条件试试' },
  error: { icon: '⚠️', title: '加载失败', desc: '请检查网络后重试' },
} as const
</script>

<template>
  <div class="wk-state">
    <span class="wk-state__icon">{{ PRESET[props.state].icon }}</span>
    <span class="wk-state__title">{{ props.title || PRESET[props.state].title }}</span>
    <span v-if="props.description || PRESET[props.state].desc" class="wk-state__desc">
      {{ props.description || PRESET[props.state].desc }}
    </span>
    <span v-if="props.detail" class="state-detail">{{ props.detail }}</span>
    <el-button
      v-if="props.state === 'error' && props.retryable"
      type="primary"
      plain
      size="small"
      @click="emit('retry')"
    >
      重新加载
    </el-button>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.state-detail {
  max-width: 520px;
  padding: $space-2 $space-3;
  border-radius: $radius-sm;
  background: $danger-bg;
  color: $danger-ink;
  font-family: $font-mono;
  font-size: $text-xs;
  word-break: break-all;
}
</style>
