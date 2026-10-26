<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * 规则列表 —— FE-13（P13 转人工触发规则配置）。
 *
 * 任务书：RuleList.vue（6 条规则）
 * 6 条：显式请求 / 情绪 / 未解决 / 高风险 / 检索失败 / 系统异常
 * DoD：「6 条全部可配置、变更 5 分钟内生效」
 *      「两项『不得挽留』开关为只读锁定」「试算结果与后端一致（抽样 50 条）」
 *
 * 设计决策：
 *   1. **「显式请求」与「高风险意图」的开关锁定为只读** —— 任务书明确这两项
 *      **不允许降级到挽留分支**。这里不是禁用提交，而是从 UI 上让「关掉它」这个动作
 *      无法发生：买家明确要求人工或触发高风险意图时还去做挽留，是业务事故。
 *   2. **每条规则显示「本次会话命中 N 次」** —— 规则好不好用要看命中分布，
 *      一条从未命中的规则要么是阈值太严，要么是根本不需要存在。
 *   3. **参数就地编辑，不弹窗** —— 主管调参是高频动作（「连续 N 轮负面」的 N 从 2 调到 3），
 *      每次弹窗会把一次调整变成四次点击。
 */
interface RuleParam {
  key: string
  label: string
  value: number | string | boolean
  type: 'number' | 'select' | 'boolean'
  min?: number
  max?: number
  unit?: string
  options?: Array<{ label: string; value: string | number | boolean }>
}

interface HandoffRule {
  rule_id: string
  code: string
  name: string
  description: string
  enabled: boolean
  priority: number
  params: RuleParam[]
  no_retention_locked: boolean
}

interface Props {
  rules: HandoffRule[]
  /** 试算得到的每条规则命中数 */
  hitCounts?: Record<string, number>
  /** 是否允许编辑 */
  editable?: boolean
  /** 正在保存 */
  saving?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  hitCounts: () => ({}),
  editable: false,
  saving: false,
})

const emit = defineEmits<{
  (e: 'toggle', ruleId: string, enabled: boolean): void
  (e: 'param', ruleId: string, key: string, value: number | string | boolean): void
  (e: 'move', ruleId: string, direction: -1 | 1): void
}>()

/** 本地参数草稿，避免每次输入都直接触发保存 */
const drafts = ref<Record<string, Record<string, number | string | boolean>>>({})

function draftOf(rule: HandoffRule, param: RuleParam): number | string | boolean {
  const draft = drafts.value[rule.rule_id]?.[param.key]
  return draft !== undefined ? draft : param.value
}

function commitParam(rule: HandoffRule, param: RuleParam, value: number | string | boolean): void {
  const bucket = drafts.value[rule.rule_id] ?? {}
  bucket[param.key] = value
  drafts.value[rule.rule_id] = bucket
  emit('param', rule.rule_id, param.key, value)
}

function isLocked(rule: HandoffRule): boolean {
  return rule.no_retention_locked === true
}

const ordered = computed(() => [...props.rules].sort((a, b) => a.priority - b.priority))
</script>

<template>
  <div class="rules">
    <p class="rules__intro">
      优先级从上到下依次判定，命中即转人工。规则变更将在 5 分钟内生效。
    </p>

    <ol class="rules__list">
      <li
        v-for="(rule, index) in ordered"
        :key="rule.rule_id"
        class="rule"
        :class="{ 'rule--locked': isLocked(rule), 'rule--off': !rule.enabled }"
      >
        <div class="rule__head">
          <span class="rule__priority wk-mono">{{ index + 1 }}</span>
          <div class="rule__title">
            <span class="rule__name">{{ rule.name }}</span>
            <span v-if="isLocked(rule)" class="rule__lock">不得挽留 · 锁定</span>
            <span v-if="!rule.enabled" class="rule__off-tag">已停用</span>
          </div>
          <span class="rule__hits">
            试算命中 <b class="wk-mono">{{ props.hitCounts[rule.rule_id] ?? 0 }}</b> 次
          </span>
          <div class="rule__actions">
            <el-button
              v-if="props.editable && index > 0"
              link
              size="small"
              :disabled="props.saving"
              @click="emit('move', rule.rule_id, -1)"
            >
              上移
            </el-button>
            <el-button
              v-if="props.editable && index < ordered.length - 1"
              link
              size="small"
              :disabled="props.saving"
              @click="emit('move', rule.rule_id, 1)"
            >
              下移
            </el-button>
            <el-switch
              :model-value="rule.enabled"
              :disabled="!props.editable || props.saving || isLocked(rule)"
              size="small"
              inline-prompt
              active-text="启用"
              inactive-text="停用"
              @update:model-value="emit('toggle', rule.rule_id, $event as boolean)"
            />
          </div>
        </div>

        <p class="rule__desc">{{ rule.description }}</p>

        <div v-if="rule.params.length" class="rule__params">
          <div v-for="param in rule.params" :key="param.key" class="param">
            <span class="param__label">
              {{ param.label }}
              <em v-if="param.unit" class="param__unit">{{ param.unit }}</em>
            </span>

            <template v-if="param.type === 'number'">
              <el-input-number
                :model-value="Number(draftOf(rule, param))"
                :min="param.min ?? 0"
                :max="param.max ?? 9999"
                :step="1"
                size="small"
                controls-position="right"
                class="param__input"
                :disabled="!props.editable || props.saving"
                @update:model-value="commitParam(rule, param, Number($event))"
              />
            </template>

            <el-select
              v-else-if="param.type === 'select'"
              :model-value="String(draftOf(rule, param))"
              size="small"
              class="param__input"
              :disabled="!props.editable || props.saving"
              @update:model-value="commitParam(rule, param, String($event))"
            >
              <el-option
                v-for="opt in param.options ?? []"
                :key="String(opt.value)"
                :label="opt.label"
                :value="String(opt.value)"
              />
            </el-select>

            <el-switch
              v-else
              :model-value="Boolean(draftOf(rule, param))"
              size="small"
              :disabled="!props.editable || props.saving"
              @update:model-value="commitParam(rule, param, Boolean($event))"
            />
          </div>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.rules {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__intro {
    margin: 0;
    font-size: $text-xs;
    color: $ink-500;
  }

  &__list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: $space-2;
    counter-reset: none;
  }
}

.rule {
  padding: $space-3;
  border: 1px solid $ink-200;
  border-radius: $radius-sm;
  background: $surface;
  transition: border-color 0.15s ease, opacity 0.15s ease;

  &--off {
    opacity: 0.62;
  }

  &--locked {
    border-color: $danger-500;
    background: $danger-bg;
  }

  &__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
  }

  &__priority {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: $radius-pill;
    background: $ink-800;
    font-size: 11px;
    font-weight: 700;
    color: #fff;
  }

  &__title {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__name {
    font-size: $text-sm;
    font-weight: 700;
    color: $ink-900;
  }

  &__lock {
    padding: 0 6px;
    border-radius: $radius-xs;
    background: $danger-500;
    font-size: 10px;
    font-weight: 600;
    color: #fff;
  }

  &__off-tag {
    padding: 0 6px;
    border-radius: $radius-xs;
    background: $ink-200;
    font-size: 10px;
    color: $ink-600;
  }

  &__hits {
    font-size: 11px;
    color: $ink-500;

    b {
      color: $ink-800;
    }
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 2px;
    margin-left: auto;
  }

  &__desc {
    margin: $space-2 0 0;
    font-size: $text-xs;
    line-height: 1.7;
    color: $ink-600;
  }

  &__params {
    display: flex;
    flex-wrap: wrap;
    gap: $space-3;
    margin-top: $space-2;
    padding-top: $space-2;
    border-top: 1px dashed $ink-200;
  }
}

.param {
  display: flex;
  align-items: center;
  gap: $space-2;

  &__label {
    font-size: $text-xs;
    color: $ink-700;
  }

  &__unit {
    font-style: normal;
    color: $ink-400;
  }

  &__input {
    width: 148px;
  }
}
</style>
