<script setup lang="ts">
import { computed, ref } from 'vue'
import type { HandoffRule, HandoffRuleTestResult } from '@/types/contract'

/**
 * 规则试算 —— FE-13。
 *
 * 任务书：RuleTestPanel.vue（规则试算）
 * DoD：「试算结果与后端一致（抽样 50 条）」
 *
 * 设计决策：
 *   1. **试算走后端，不在前端模拟规则** —— 前端只有规则参数，没有会话全量数据
 *      （买家消息、情绪识别结果、检索分数都不在前端）。本地模拟等于用假数据得出假结论。
 *   2. **抽样 50 条固定显示样本量** —— 任务书明确口径，前端把这个数字亮出来，
 *      联调时才能和后端逐条核对。
 *   3. **命中明细默认只展示前 20 条 + 「展开全部」** —— 50 条全展开会淹没汇总信息，
 *      而主管真正要看的是「分布对不对」。
 *   4. **diff_count 显眼** —— 试算的目的是「改完参数后和线上规则差多少」，
 *      如果 diff 为 0 说明参数调整没有改变任何判定结果，这次调整可能是无效的。
 */
interface Props {
  rules: HandoffRule[]
  /** 当前生效的规则快照（用于对比 diff） */
  sampleSize?: number
  testing?: boolean
  result?: HandoffRuleTestResult | null
}

const props = withDefaults(defineProps<Props>(), {
  sampleSize: 50,
  testing: false,
  result: null,
})

const emit = defineEmits<{
  (e: 'test', sampleSize: number): void
}>()

const size = ref<number>(props.sampleSize)
const expanded = ref(false)

const details = computed(() => props.result?.details ?? [])
const visibleDetails = computed(() => (expanded.value ? details.value : details.value.slice(0, 20)))

const perRule = computed(() => props.result?.per_rule ?? [])

const ruleName = (ruleId: string): string =>
  props.rules.find((rule) => rule.rule_id === ruleId)?.name ?? ruleId

function run(): void {
  emit('test', size.value)
}
</script>

<template>
  <div class="test">
    <header class="test__head">
      <div class="test__intro">
        <span class="test__title">规则试算</span>
        <span class="test__hint">
          按当前规则参数对最近的历史会话重新判定转人工结果。试算在后端执行，前端不做本地模拟。
        </span>
      </div>
      <div class="test__actions">
        <el-input-number
          v-model="size"
          :min="10"
          :max="200"
          :step="10"
          size="small"
          controls-position="right"
          class="test__size"
        />
        <el-button type="primary" size="small" :loading="props.testing" @click="run">
          开始试算
        </el-button>
      </div>
    </header>

    <template v-if="props.result">
      <div class="test__summary">
        <div class="test__stat">
          <span class="test__stat-value wk-mono">{{ props.result.sample_size }}</span>
          <span class="test__stat-label">抽样会话数</span>
        </div>
        <div class="test__stat">
          <span class="test__stat-value wk-mono">{{ props.result.matched }}</span>
          <span class="test__stat-label">命中规则数</span>
        </div>
        <div class="test__stat" :class="props.result.diff_count > 0 ? 'test__stat--diff' : ''">
          <span class="test__stat-value wk-mono">{{ props.result.diff_count }}</span>
          <span class="test__stat-label">与线上判定差异</span>
        </div>
      </div>

      <p v-if="props.result.diff_count === 0" class="test__zero">
        当前参数下的判定结果与线上完全一致 —— 本次调整未改变任何会话的转人工结论。
      </p>
      <p v-else class="test__nonzero">
        有 {{ props.result.diff_count }} 条会话的判定结果发生变化，请确认这些变化符合预期后再保存。
      </p>

      <div class="test__per-rule">
        <div v-for="entry in perRule" :key="entry.rule_id" class="per-rule">
          <span class="per-rule__name">{{ entry.name }}</span>
          <span class="per-rule__id wk-mono">{{ entry.rule_id }}</span>
          <span class="per-rule__bar">
            <span
              class="per-rule__fill"
              :style="{
                width: `${
                  props.result.matched > 0 ? (entry.hits / props.result.matched) * 100 : 0
                }%`,
              }"
            />
          </span>
          <span class="per-rule__hits wk-mono">{{ entry.hits }}</span>
        </div>
      </div>

      <div class="test__details">
        <div class="test__details-head">
          <span>命中明细</span>
          <el-button v-if="details.length > 20" link size="small" @click="expanded = !expanded">
            {{ expanded ? '收起' : `展开全部 ${details.length} 条` }}
          </el-button>
        </div>
        <el-table :data="visibleDetails" size="small" max-height="320">
          <el-table-column prop="session_id" label="会话" width="170">
            <template #default="{ row }">
              <span class="wk-mono">{{ (row as typeof details[number]).session_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="命中规则" min-width="150">
            <template #default="{ row }">
              <span v-if="(row as typeof details[number]).matched_rule">
                {{ ruleName((row as typeof details[number]).matched_rule as string) }}
              </span>
              <span v-else class="test__no-rule">未命中（机器人继续处理）</span>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="判定说明" min-width="200" show-overflow-tooltip />
        </el-table>
      </div>
    </template>

    <p v-else class="test__empty">
      尚未试算。修改参数后先试算，确认命中分布符合预期再保存。
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.test {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: $space-3;
  }

  &__intro {
    flex: 1;
    min-width: 260px;
  }

  &__title {
    display: block;
    font-size: $text-sm;
    font-weight: 700;
    color: $ink-800;
  }

  &__hint {
    display: block;
    margin-top: 2px;
    font-size: 11px;
    line-height: 1.6;
    color: $ink-500;
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__size {
    width: 108px;
  }

  &__summary {
    display: flex;
    flex-wrap: wrap;
    gap: $space-4;
    padding: $space-3;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    background: $surface-sunken;
  }

  &__stat {
    display: flex;
    flex-direction: column;
    gap: 1px;

    &--diff &-value {
      color: $warning-ink;
    }
  }

  &__stat-value {
    font-size: $text-lg;
    font-weight: 700;
    color: $ink-900;
  }

  &__stat-label {
    font-size: 10px;
    color: $ink-500;
  }

  &__zero {
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-xs;
    background: $info-bg;
    font-size: $text-xs;
    color: $info-ink;
  }

  &__nonzero {
    margin: 0;
    padding: $space-2 $space-3;
    border-radius: $radius-xs;
    background: $warning-bg;
    font-size: $text-xs;
    color: $warning-ink;
  }

  &__per-rule {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  &__details-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-700;
  }

  &__no-rule {
    font-size: $text-xs;
    color: $ink-400;
  }

  &__empty {
    margin: 0;
    padding: $space-4;
    border: 1px dashed $ink-200;
    border-radius: $radius-sm;
    font-size: $text-xs;
    text-align: center;
    color: $ink-400;
  }
}

.per-rule {
  display: grid;
  grid-template-columns: 150px 118px 1fr 44px;
  align-items: center;
  gap: $space-2;
  font-size: $text-xs;

  &__name {
    color: $ink-800;
    font-weight: 600;
  }

  &__id {
    font-size: 10px;
    color: $ink-400;
  }

  &__bar {
    height: 8px;
    border-radius: $radius-pill;
    background: $ink-150;
    overflow: hidden;
  }

  &__fill {
    display: block;
    height: 100%;
    border-radius: $radius-pill;
    background: $brand-500;
  }

  &__hits {
    text-align: right;
    font-weight: 600;
    color: $ink-800;
  }
}
</style>
