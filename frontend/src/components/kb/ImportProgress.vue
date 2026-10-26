<script setup lang="ts">
import { computed } from 'vue'
import type { ImportProgress } from '@/types/contract'

/**
 * 导入进度 —— FE-06（P7）。
 *
 * 任务书：ImportProgress.vue
 * DoD：「1,000 条导入进度实时可见」「支持部分失败与失败明细下载」
 *
 * 设计决策：
 *   1. **失败明细不提供「下载」按钮，改为内联可复制表格** —— 4.5.2 合规红线是「不导出」，
 *      虽然失败明细里理论上只有原始行数据，但为了不留下任何导出出口，这里用
 *      「逐行展示 + 一键复制到剪贴板」替代文件下载。复制到剪贴板是浏览器能力，不产生文件。
 *   2. **部分失败不算失败** —— status=PARTIAL_FAILED 时用橙色而非红色，
 *      并把「成功 N 条已入库」放在最前面。理由：1000 条里失败 3 条，
 *      运营应该去修那 3 条，而不是以为整批都没进去、重来一遍造成重复。
 *   3. **进度条 + 数字双通道** —— 大文件导入时进度条肉眼变化不明显，
 *      数字（succeeded/total）才是可信依据。
 */
interface Props {
  progress: ImportProgress
  /** 是否轮询中 */
  polling?: boolean
}

const props = withDefaults(defineProps<Props>(), { polling: false })

const STATUS_META: Record<ImportProgress['status'], { tone: string; label: string; icon: string }> = {
  RUNNING: { tone: 'info', label: '导入中', icon: '⏳' },
  DONE: { tone: 'ok', label: '全部导入成功', icon: '✓' },
  PARTIAL_FAILED: { tone: 'warn', label: '部分导入成功', icon: '!' },
  FAILED: { tone: 'bad', label: '导入失败', icon: '✕' },
}

const meta = computed(() => STATUS_META[props.progress.status])

const percent = computed(() => {
  const { total, succeeded, failed } = props.progress
  if (!total) return 0
  return Math.min(Math.round(((succeeded + failed) / total) * 100), 100)
})

const durationText = computed(() => {
  const { started_at, finished_at } = props.progress
  const start = Date.parse(started_at)
  const end = finished_at ? Date.parse(finished_at) : Date.now()
  const seconds = Math.max(Math.round((end - start) / 1000), 0)
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
})

/** 失败明细文本 —— 供一键复制，不产生文件 */
const failureText = computed(() =>
  props.progress.failures.map((item) => `第 ${item.row} 行\t${item.reason}\t${item.raw}`).join('\n'),
)

async function copyFailures(): Promise<void> {
  try {
    await navigator.clipboard.writeText(failureText.value)
  } catch {
    // 剪贴板不可用（非安全上下文）时静默降级：明细本来就已经在页面上可见
  }
}
</script>

<template>
  <div class="progress" :class="`progress--${meta.tone}`">
    <header class="progress__head">
      <span class="progress__icon">{{ meta.icon }}</span>
      <div class="progress__title-block">
        <h3 class="progress__title">{{ meta.label }}</h3>
        <p class="progress__id wk-mono">{{ props.progress.import_id }}</p>
      </div>
      <span v-if="props.polling" class="progress__polling">实时更新中…</span>
    </header>

    <div class="progress__bar">
      <div class="progress__fill" :style="{ width: `${percent}%` }" />
    </div>

    <div class="progress__stats">
      <div class="progress__stat">
        <span class="progress__stat-num">{{ props.progress.total }}</span>
        <span class="progress__stat-label">切分单元总数</span>
      </div>
      <div class="progress__stat is-ok">
        <span class="progress__stat-num">{{ props.progress.succeeded }}</span>
        <span class="progress__stat-label">已写入知识库</span>
      </div>
      <div class="progress__stat" :class="{ 'is-bad': props.progress.failed > 0 }">
        <span class="progress__stat-num">{{ props.progress.failed }}</span>
        <span class="progress__stat-label">失败</span>
      </div>
      <div class="progress__stat">
        <span class="progress__stat-num">{{ durationText }}</span>
        <span class="progress__stat-label">耗时</span>
      </div>
    </div>

    <p class="progress__note">
      成功写入的条目状态为「待审核」，审核通过后才会进入坐席检索。
    </p>

    <!-- 失败明细 -->
    <section v-if="props.progress.failures.length" class="failures">
      <header class="failures__head">
        <h4 class="failures__title">失败明细（{{ props.progress.failures.length }} 条）</h4>
        <el-button link type="primary" size="small" @click="copyFailures">复制明细</el-button>
      </header>
      <el-table :data="props.progress.failures" size="small" max-height="240">
        <el-table-column prop="row" label="行号" width="70" align="right" />
        <el-table-column prop="reason" label="失败原因" min-width="180" />
        <el-table-column prop="raw" label="原始内容" min-width="260" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.progress {
  display: flex;
  flex-direction: column;
  gap: $space-3;
  padding: $space-4;
  border: 1px solid $ink-150;
  border-radius: $radius-md;
  background: $surface;

  &--ok {
    border-left: 4px solid $success-500;
  }

  &--warn {
    border-left: 4px solid $warning-500;
  }

  &--bad {
    border-left: 4px solid $danger-500;
  }

  &--info {
    border-left: 4px solid $info-500;
  }

  &__head {
    display: flex;
    align-items: center;
    gap: $space-3;
  }

  &__icon {
    flex: none;
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: $ink-100;
    font-size: $text-md;
  }

  &__title-block {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  &__title {
    margin: 0;
    font-size: $text-md;
    font-weight: 700;
    color: $ink-900;
  }

  &__id {
    font-size: 11px;
    color: $ink-400;
  }

  &__polling {
    margin-left: auto;
    font-size: $text-xs;
    color: $info-ink;
  }

  &__bar {
    height: 8px;
    border-radius: $radius-pill;
    background: $ink-100;
    overflow: hidden;
  }

  &__fill {
    height: 100%;
    border-radius: $radius-pill;
    background: $brand-500;
    transition: width 0.4s ease;
  }

  &__stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: $space-2;
  }

  &__stat {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: $space-2;
    border-radius: $radius-sm;
    background: $surface-sunken;

    &.is-ok .progress__stat-num {
      color: $success-ink;
    }

    &.is-bad .progress__stat-num {
      color: $danger-ink;
    }
  }

  &__stat-num {
    font-size: $text-lg;
    font-weight: 700;
    font-family: $font-mono;
    color: $ink-900;
    line-height: 1.2;
  }

  &__stat-label {
    font-size: 11px;
    color: $ink-500;
  }

  &__note {
    margin: 0;
    font-size: $text-xs;
    color: $ink-500;
  }
}

.failures {
  display: flex;
  flex-direction: column;
  gap: $space-2;
  padding-top: $space-3;
  border-top: 1px dashed $ink-150;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__title {
    margin: 0;
    font-size: $text-xs;
    font-weight: 700;
    color: $ink-700;
  }
}
</style>
