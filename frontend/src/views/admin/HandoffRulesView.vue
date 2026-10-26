<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import RuleList from '@/components/config/RuleList.vue'
import RuleTestPanel from '@/components/config/RuleTestPanel.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchHandoffRules, saveHandoffRules, testHandoffRules } from '@/api/config'
import { useAsyncData } from '@/composables/useAsyncData'
import { useAuthStore } from '@/stores/auth'
import { canDo, isReadOnlyConfig } from '@/permission/matrix'
import { ElMessage } from 'element-plus'
import type { HandoffRule, HandoffRuleTestResult } from '@/types/contract'

/**
 * 转人工触发规则配置 —— FE-13（P13）。
 *
 * 任务书：RuleList.vue + RuleEditor.vue + PriorityOrder.vue + RuleTestPanel.vue
 * DoD：「6 条全部可配置、变更 5 分钟内生效」「两项『不得挽留』开关为只读锁定」
 *      「试算结果与后端一致（抽样 50 条）」「变更留日志」
 *
 * 设计决策：
 *   1. **「不得挽留」锁定不在保存时校验，而是在 UI 上不可关闭** ——
 *      任务书要求这两条规则不允许降级。让按钮可点、保存时报错，会让主管
 *      先产生「我改了」的错觉再被驳回；直接不可点更符合预期。
 *   2. **改完参数先试算再保存** —— 试算面板常驻页面底部而不是收在按钮里，
 *      因为「把连续负面从 2 轮改成 5 轮」会让 12 条会话不再转人工，
 *      这个影响量必须在保存前就看到。
 *   3. **优先级用上移/下移而非拖拽** —— 6 条规则的优先级调整是低频操作，
 *      拖拽库引入一个依赖，而按钮排序对键盘操作与无障碍都更友好。
 */
const auth = useAuthStore()
const editable = computed(() => canDo(auth.role, 'config.write'))
const readOnly = computed(() => isReadOnlyConfig(auth.role))

interface RulesPayload {
  rules: HandoffRule[]
  test_sample_size: number
}

const { data, loading, showSkeleton, error, reload } = useAsyncData<RulesPayload | null>(
  (signal) => fetchHandoffRules({ signal }),
  { initial: null },
)

const rules = computed(() => data.value?.rules ?? [])
const sampleSize = computed(() => data.value?.test_sample_size ?? 50)

/** 本地编辑副本 */
const draft = ref<HandoffRule[]>([])

watch(
  rules,
  (list) => {
    draft.value = list.map((rule) => ({
      ...rule,
      params: rule.params.map((param) => ({ ...param })),
    }))
  },
  { immediate: true },
)

const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(rules.value))

const saving = ref(false)

function onToggle(ruleId: string, enabled: boolean): void {
  const rule = draft.value.find((item) => item.rule_id === ruleId)
  if (!rule) return
  rule.enabled = enabled
}

function onParam(ruleId: string, key: string, value: number | string | boolean): void {
  const rule = draft.value.find((item) => item.rule_id === ruleId)
  if (!rule) return
  const param = rule.params.find((item) => item.key === key)
  if (!param) return
  param.value = value
}

function onMove(ruleId: string, direction: -1 | 1): void {
  const index = draft.value.findIndex((item) => item.rule_id === ruleId)
  const target = index + direction
  if (index < 0 || target < 0 || target >= draft.value.length) return
  const current = draft.value[index]
  const other = draft.value[target]
  const currentPriority = current.priority
  current.priority = other.priority
  other.priority = currentPriority
  draft.value = [...draft.value].sort((a, b) => a.priority - b.priority)
}

async function save(): Promise<void> {
  saving.value = true
  try {
    await saveHandoffRules({
      rules: draft.value.map((rule) => ({
        rule_id: rule.rule_id,
        enabled: rule.enabled,
        priority: rule.priority,
        params: rule.params.map((param) => ({ key: param.key, value: param.value })),
      })),
    })
    ElMessage.success('规则已保存，将在 5 分钟内生效')
    await reload()
  } catch {
    // 错误提示由拦截器统一处理
  } finally {
    saving.value = false
  }
}

const testing = ref(false)
const testResult = ref<HandoffRuleTestResult | null>(null)
const testError = ref('')

async function onTest(size: number): Promise<void> {
  testing.value = true
  testError.value = ''
  try {
    testResult.value = await testHandoffRules({ rules: draft.value, sample_size: size })
  } catch (err) {
    testError.value = err instanceof Error ? err.message : String(err)
    testResult.value = null
  } finally {
    testing.value = false
  }
}

/** 试算命中数 */
const hitCounts = computed<Record<string, number>>(() => {
  const map: Record<string, number> = {}
  for (const entry of testResult.value?.per_rule ?? []) map[entry.rule_id] = entry.hits
  return map
})
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">转人工触发规则</h1>
        <p class="wk-page__subtitle">
          6 条规则按优先级依次判定，命中即转人工。规则变更将在 5 分钟内生效。
        </p>
      </div>
      <div class="rules__actions">
        <el-button size="small" :loading="loading" @click="reload">重置</el-button>
        <el-button
          v-if="editable"
          type="primary"
          size="small"
          :disabled="!dirty"
          :loading="saving"
          @click="save"
        >
          保存规则
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="readOnly"
      type="info"
      show-icon
      :closable="false"
      title="当前身份对系统配置为只读，可查看与试算，但不能保存修改"
      class="rules__notice"
    />

    <PageState v-if="showSkeleton" state="loading" title="正在加载转人工规则" />
    <PageState
      v-else-if="error"
      state="error"
      title="规则加载失败"
      :detail="error.message"
      retryable
      @retry="reload"
    />

    <template v-else-if="draft.length">
      <section class="wk-card">
        <div class="wk-card__header">
          <span class="wk-card__title">规则与优先级</span>
          <span v-if="dirty" class="wk-card__hint rules__dirty">有未保存的修改</span>
        </div>
        <div class="wk-card__body">
          <RuleList
            :rules="draft"
            :hit-counts="hitCounts"
            :editable="editable"
            :saving="saving"
            @toggle="onToggle"
            @param="onParam"
            @move="onMove"
          />
        </div>
      </section>

      <section class="wk-card">
        <div class="wk-card__header">
          <span class="wk-card__title">规则试算</span>
        </div>
        <div class="wk-card__body">
          <p v-if="testError" class="rules__error">{{ testError }}</p>
          <RuleTestPanel
            :rules="draft"
            :sample-size="sampleSize"
            :testing="testing"
            :result="testResult"
            @test="onTest"
          />
        </div>
      </section>
    </template>

    <PageState v-else state="empty" title="尚未配置任何规则" description="规则由服务端初始化，缺失请联系技术支持。" />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.rules {
  &__actions {
    display: flex;
    gap: $space-2;
  }

  &__notice {
    margin-bottom: $space-3;
  }

  &__dirty {
    color: $warning-ink;
    font-weight: 600;
  }

  &__error {
    margin: 0 0 $space-2;
    padding: $space-2;
    border-radius: $radius-xs;
    background: $danger-bg;
    font-size: $text-xs;
    color: $danger-ink;
  }
}
</style>
