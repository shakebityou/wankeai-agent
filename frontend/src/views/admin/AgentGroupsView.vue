<script setup lang="ts">
import { computed, ref } from 'vue'
import GroupTable from '@/components/config/GroupTable.vue'
import ScheduleGrid from '@/components/config/ScheduleGrid.vue'
import CapacityChart from '@/components/config/CapacityChart.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchAgentGroups, saveAgentGroups } from '@/api/config'
import { useAsyncData } from '@/composables/useAsyncData'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'
import { ElMessage } from 'element-plus'
import type { AgentGroup } from '@/types/contract'

/**
 * 坐席分组与排班配置 —— FE-12（P12）。
 *
 * 任务书：GroupTable.vue + ScheduleGrid.vue + CapacityChart.vue
 * DoD：「6 分组 / 7 时段数据完整导入并可视化」
 *      「改在线时段后 BE-15 在线判定 5 分钟内同步」
 *      「排班冲突检测」「峰值接待量超标告警」
 *
 * 设计决策：
 *   1. **「编辑排班」就地展开而不是弹窗** —— 排班是一个 7×7 网格，
 *      弹窗里放网格要么太小要么遮住整个列表；就地展开能一边看容量告警一边改。
 *   2. **保存的是整个 schedule 网格**（7×7 布尔数组），不是「增量改了一个格子」——
 *      后端校验也要求网格形状完整（handlers 里校验 7×7），增量提交会让并发保存互相覆盖。
 *   3. **冲突与容量告警始终可见** —— 它们来自后端，不随本地编辑实时重算，
 *      所以保存后必须重新拉取，用户才能看到最新的告警状态。
 */
const auth = useAuthStore()
const editable = computed(() => canDo(auth.role, 'config.write'))

interface GroupsPayload {
  groups: AgentGroup[]
  slot_labels: string[]
  updated_at: string
}

const { data, loading, showSkeleton, error, reload } = useAsyncData<GroupsPayload | null>(
  (signal) => fetchAgentGroups({ signal }),
  { initial: null },
)

const groups = computed(() => data.value?.groups ?? [])
const slotLabels = computed(() => data.value?.slot_labels ?? [])

/** 服务窗口原文：mock 侧多带的字段，BFF 接入后由接口下发 */
const windows = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const group of groups.value) {
    const record = group as AgentGroup & { online_window?: string }
    if (record.online_window) map[group.group_id] = record.online_window
  }
  return map
})

/** 正在编辑的分组 */
const editingGroupId = ref('')
/** 本地排班草稿 */
const draftSchedules = ref<Record<string, boolean[][]>>({})
const savingGroupId = ref('')

function startEdit(group: AgentGroup): void {
  if (editingGroupId.value === group.group_id) {
    editingGroupId.value = ''
    return
  }
  editingGroupId.value = group.group_id
  draftSchedules.value[group.group_id] = group.schedule.map((row) => row.slice())
}

function onScheduleChange(groupId: string, schedule: boolean[][]): void {
  draftSchedules.value[groupId] = schedule
}

/** 正在编辑的分组对应的网格行（含本地草稿与服务窗口原文） */
const editingRow = computed(() => {
  const group = editingGroup.value
  if (!group) return null
  return {
    group_id: group.group_id,
    group_name: group.group_name,
    agent_count: group.agent_count,
    daily_volume: group.daily_volume,
    peak_volume: group.peak_volume,
    online_window: windows.value[group.group_id] ?? '—',
    schedule: draftSchedules.value[group.group_id] ?? group.schedule,
  }
})

async function saveSchedule(group: AgentGroup): Promise<void> {
  const schedule = draftSchedules.value[group.group_id]
  if (!schedule) return
  savingGroupId.value = group.group_id
  try {
    await saveAgentGroups({ groups: [{ group_id: group.group_id, schedule }] })
    ElMessage.success('排班已保存，将在 5 分钟内同步至在线判定')
    editingGroupId.value = ''
    delete draftSchedules.value[group.group_id]
    await reload()
  } catch {
    // 错误提示由拦截器统一处理
  } finally {
    savingGroupId.value = ''
  }
}

const editingGroup = computed(() => groups.value.find((group) => group.group_id === editingGroupId.value) ?? null)
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">坐席分组与排班</h1>
        <p class="wk-page__subtitle">
          6 个分组 × 7 个时段的在线安排。排班变更将在 5 分钟内同步至在线判定。
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="reload">刷新</el-button>
    </header>

    <PageState v-if="showSkeleton" state="loading" title="正在加载分组与排班" />
    <PageState
      v-else-if="error"
      state="error"
      title="分组信息加载失败"
      :detail="error.message"
      retryable
      @retry="reload"
    />

    <template v-else-if="groups.length">
      <section class="wk-card">
        <div class="wk-card__header">
          <span class="wk-card__title">分组概览</span>
          <span class="wk-card__hint">共 {{ groups.length }} 个分组</span>
        </div>
        <div class="wk-card__body">
          <GroupTable
            :groups="groups"
            :windows="windows"
            :editable="editable"
            :saving-group-id="savingGroupId"
            @edit="startEdit"
          />
        </div>
      </section>

      <section class="wk-card">
        <div class="wk-card__header">
          <span class="wk-card__title">排班网格</span>
          <span v-if="editingGroup" class="wk-card__hint">正在编辑：{{ editingGroup.group_name }}</span>
        </div>
        <div class="wk-card__body">
          <p v-if="!editingGroup" class="agent-groups__hint">
            点击分组操作列的「排班」进入编辑。编辑状态下可直接点击格子切换在线 / 不在线。
          </p>
          <template v-else-if="editingRow">
            <ScheduleGrid
              :groups="[editingRow]"
              :slot-labels="slotLabels"
              :editable="editable"
              :saving-group-id="savingGroupId"
              @change="onScheduleChange"
            />
            <div class="agent-groups__foot">
              <span>保存的是整个 7×7 网格，避免并发保存互相覆盖。</span>
              <span class="wk-spacer" />
              <el-button size="small" @click="editingGroupId = ''">取消</el-button>
              <el-button
                type="primary"
                size="small"
                :loading="savingGroupId === editingGroup.group_id"
                @click="saveSchedule(editingGroup)"
              >
                保存排班
              </el-button>
            </div>
          </template>
        </div>
      </section>

      <section class="wk-card">
        <div class="wk-card__header">
          <span class="wk-card__title">容量告警</span>
          <span class="wk-card__hint">峰值接待量 vs 当前排班可承载容量</span>
        </div>
        <div class="wk-card__body">
          <CapacityChart :groups="groups" :loading="loading" />
        </div>
      </section>
    </template>

    <PageState
      v-else
      state="empty"
      title="尚未配置任何分组"
      description="分组数据来自商家的客服组织结构，导入后即可在此配置排班。"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.agent-groups {
  &__hint {
    margin: 0;
    font-size: $text-xs;
    color: $ink-500;
  }

  &__foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
    margin-top: $space-3;
    padding-top: $space-3;
    border-top: 1px dashed $ink-150;
    font-size: 11px;
    color: $ink-500;
  }
}
</style>
