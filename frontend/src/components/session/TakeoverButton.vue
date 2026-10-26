<script setup lang="ts">
import { computed, ref } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { releaseSession, takeoverSession } from '@/api/sessions'
import { BFF_CODE, BffError } from '@/types/bff'
import { ElMessage } from 'element-plus'
import type { ReleaseResponse, SessionSummary, TakeoverResponse } from '@/types/contract'

/**
 * 一键接管 / 释放按钮 —— FE-03 ★。
 *
 * 任务书要求：
 *   「接管二次确认 → bot_muted=true 机器人立即停止自动回复」
 *   「并发冲突返回 10005 时提示『该会话已被 XXX 接管』并刷新」
 *   「释放需二次确认」
 *   「接管后输入框切换『坐席回复』模式，发送 sender=AGENT」
 *
 * DoD：转接成功率 ≥99.5%；两坐席同时点只能一个成功；接管后 3 秒内机器人停止回复。
 *
 * 关键设计决策：
 *
 * 1. 冲突（10005）不用全局拦截器兜底，而是本地处理。
 *    全局拦截器只能弹一句通用提示；而这里需要做三件事：提示具体是谁接管了、
 *    立刻刷新会话状态、把按钮切回正确形态。这些只有拿到响应体才能做。
 *    （全局拦截器仍会记录，不冲突。）
 *
 * 2. 按钮文案随归属变化，而不是常驻两个按钮。
 *    未接管 -> 「接管会话」；我接管 -> 「释放回机器人」；他人接管 -> 禁用 + 显示归属人。
 *    这样从 UI 上就杜绝了「别人正在处理，我还去点接管」的误操作。
 *
 * 3. 权限：接管属于坐席/主管/管理员的动作（takeover.execute），
 *    无权限时直接隐藏按钮，而不是点了才报 10003。
 */
interface Props {
  session: SessionSummary
  /** 当前登录坐席 ID */
  currentAgentId: string
  /** 当前登录坐席名 */
  currentAgentName: string
  /** 是否有接管权限（canDo('takeover.execute')） */
  allowed?: boolean
}

const props = withDefaults(defineProps<Props>(), { allowed: true })

const emit = defineEmits<{
  /** 接管成功 */
  takenOver: [payload: TakeoverResponse]
  /** 释放成功 */
  released: [payload: ReleaseResponse]
  /** 需要刷新会话（冲突时） */
  needRefresh: []
}>()

const dialogVisible = ref(false)
const pending = ref(false)

const isAgentOwned = computed(() => props.session.owner === 'AGENT')
const isMine = computed(
  () => isAgentOwned.value && props.session.agent_id === props.currentAgentId,
)
const isOthers = computed(() => isAgentOwned.value && !isMine.value)

/** 他人接管时的占用者名字 */
const occupiedBy = computed(() => props.session.agent_name ?? '其他坐席')

const buttonLabel = computed(() => {
  if (!isAgentOwned.value) return '接管会话'
  if (isMine.value) return '释放回机器人'
  return `已被 ${occupiedBy.value} 接管`
})

const buttonType = computed<'primary' | 'warning'>(() =>
  isMine.value ? 'warning' : 'primary',
)

function openDialog(): void {
  if (isOthers.value) return
  dialogVisible.value = true
}

async function confirm(): Promise<void> {
  pending.value = true
  try {
    if (isAgentOwned.value && isMine.value) {
      const result = await releaseSession(props.session.session_id)
      ElMessage.success('已释放回机器人，机器人将恢复自动回复')
      emit('released', result)
    } else {
      const result = await takeoverSession(props.session.session_id, {
        agent_id: props.currentAgentId,
        ticket_no: props.session.ticket_no ?? undefined,
      })
      ElMessage.success('接管成功，机器人已停止自动回复')
      emit('takenOver', result)
    }
    dialogVisible.value = false
  } catch (caught) {
    // 并发冲突：两坐席同时点，只有一人能成功
    if (caught instanceof BffError && caught.code === BFF_CODE.CONFLICT) {
      ElMessage.warning(caught.message || '该会话已被其他坐席接管')
      dialogVisible.value = false
      emit('needRefresh')
    } else if (caught instanceof BffError) {
      ElMessage.error(caught.message || '操作失败，请稍后重试')
    } else {
      ElMessage.error('操作失败，请稍后重试')
    }
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <div class="takeover">
    <el-button
      v-if="props.allowed"
      :type="buttonType"
      :disabled="isOthers"
      :title="isOthers ? `该会话由 ${occupiedBy} 处理中` : ''"
      size="default"
      @click="openDialog"
    >
      {{ buttonLabel }}
    </el-button>
    <span v-else class="takeover__denied">无接管权限</span>

    <ConfirmDialog
      v-model="dialogVisible"
      :mode="isMine ? 'release' : 'takeover'"
      :buyer-display="props.session.buyer_display"
      :occupied-by="isOthers ? occupiedBy : null"
      :pending="pending"
      @confirm="confirm"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.takeover {
  display: inline-flex;
  align-items: center;

  &__denied {
    font-size: $text-xs;
    color: $ink-500;
  }
}
</style>
