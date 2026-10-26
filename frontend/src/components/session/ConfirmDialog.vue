<script setup lang="ts">
import { computed } from 'vue'

/**
 * 二次确认对话框 —— FE-03。
 *
 * 任务书：「接管二次确认」「释放需二次确认」。
 *
 * 为什么接管一定要二次确认：
 *   接管会立刻让机器人闭嘴（bot_muted=true）。如果坐席误点，
 *   买家会突然收不到任何回复，直到坐席发现自己已经接管 —— 这是真实事故。
 *   释放同理：释放后机器人重新接管，坐席若还在打字，就会双回复。
 *
 * 因此这个对话框不只是「确定/取消」，而是把**后果**写清楚：
 *   - 接管：机器人将立即停止自动回复，您将成为该会话唯一回复方；
 *   - 释放：机器人将恢复自动回复，您的输入框会退出坐席模式。
 */
interface Props {
  modelValue: boolean
  /** 场景：接管 / 释放 */
  mode: 'takeover' | 'release'
  /** 会话买家标识（脱敏后） */
  buyerDisplay?: string
  /** 接管时若该会话已被他人接管，展示占用者名字 */
  occupiedBy?: string | null
  /** 确认中（按钮 loading） */
  pending?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  buyerDisplay: '',
  occupiedBy: null,
  pending: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: []
}>()

const isTakeover = computed(() => props.mode === 'takeover')

const title = computed(() => (isTakeover.value ? '确认接管该会话？' : '确认释放回机器人？'))

const consequences = computed<string[]>(() =>
  isTakeover.value
    ? [
        '机器人将立即停止自动回复（bot_muted = true）',
        '您将成为该会话的唯一回复方，消息以「坐席」身份发送',
        '买家不会收到任何接管提示，请直接继续沟通',
      ]
    : [
        '机器人将恢复自动回复，可能立刻回复买家',
        '您的输入框会退出坐席模式，无法再以坐席身份发送',
        '若仍有未解决诉求，建议先处理完再释放',
      ],
)

function close(): void {
  if (props.pending) return
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="props.modelValue"
    :title="title"
    width="440px"
    :close-on-click-modal="!props.pending"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div class="confirm">
      <p v-if="props.buyerDisplay" class="confirm__target">
        会话买家：<strong>{{ props.buyerDisplay }}</strong>
      </p>

      <!-- 并发冲突预警：已被他人接管时，坐席应当直接取消而不是硬接管 -->
      <el-alert
        v-if="isTakeover && props.occupiedBy"
        type="warning"
        :closable="false"
        show-icon
        class="confirm__alert"
      >
        该会话当前已被 <strong>{{ props.occupiedBy }}</strong> 接管。
        继续操作会触发并发冲突（10005），建议刷新后确认归属。
      </el-alert>

      <p class="confirm__lead">此操作将会：</p>
      <ul class="confirm__list">
        <li v-for="(item, index) in consequences" :key="index">{{ item }}</li>
      </ul>
    </div>

    <template #footer>
      <el-button :disabled="props.pending" @click="close">取消</el-button>
      <el-button
        :type="isTakeover ? 'primary' : 'warning'"
        :loading="props.pending"
        @click="emit('confirm')"
      >
        {{ isTakeover ? '确认接管' : '确认释放' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.confirm {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__target {
    margin: 0;
    font-size: $text-sm;
    color: $ink-700;
  }

  &__alert {
    margin: 0;
  }

  &__lead {
    margin: 0;
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-700;
  }

  &__list {
    margin: 0;
    padding-left: $space-5;
    display: flex;
    flex-direction: column;
    gap: $space-1;
    font-size: $text-sm;
    line-height: 1.7;
    color: $ink-600;
  }
}
</style>
