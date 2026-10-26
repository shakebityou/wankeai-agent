<script setup lang="ts">
import { computed, ref } from 'vue'
import ChannelTable from '@/components/config/ChannelTable.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchChannels, updateChannelAuth } from '@/api/config'
import { useAsyncData } from '@/composables/useAsyncData'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'
import { ElMessage } from 'element-plus'
import type { ChannelAuth } from '@/types/contract'

/**
 * 渠道与授权管理 —— FE-11（P11）。
 *
 * 任务书：ChannelTable.vue + AuthStatusTag.vue + ExpiryAlert.vue
 * DoD：「5 渠道全展示到期日准确」「到期前 15 天提醒（mock 时间验证）」
 *      「抖音认证状态可手动更新并留日志」「不展示任何 AppSecret/Token 明文」
 *
 * 设计决策：
 *   **只允许改抖音小店的服务商认证状态** —— 这是五个渠道里唯一一个
 *   「平台审核进度需要人工同步」的字段（认证材料提交后平台 10 个工作日才出结果），
 *   其余授权状态只能由平台回调推进，前端给入口只会造成与服务端不一致。
 */
const auth = useAuthStore()
const editable = computed(() => canDo(auth.role, 'config.write'))

const { data, loading, showSkeleton, error, reload } = useAsyncData<{ channels: ChannelAuth[] } | null>(
  (signal) => fetchChannels({ signal }),
  { initial: null },
)

const channels = computed(() => data.value?.channels ?? [])

const pendingChannel = ref('')

async function onToggleProvider(channel: ChannelAuth, certified: boolean): Promise<void> {
  pendingChannel.value = channel.channel
  try {
    await updateChannelAuth(channel.channel, {
      service_provider_certified: certified,
      service_provider_note: certified
        ? '服务商认证已通过，渠道可正常收发消息'
        : '服务商认证材料已提交，平台审核中',
    })
    ElMessage.success('已更新服务商认证状态，变更已留痕')
    await reload()
  } catch {
    // 错误提示由拦截器统一处理
  } finally {
    pendingChannel.value = ''
  }
}
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">渠道与授权管理</h1>
        <p class="wk-page__subtitle">
          淘宝 / 天猫 / 拼多多 / 抖音小店 / 微信客服的授权状态、权限包与到期日。到期前 15 天会自动提醒。
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="reload">刷新</el-button>
    </header>

    <section class="wk-card">
      <div class="wk-card__body">
        <PageState v-if="showSkeleton" state="loading" title="正在加载渠道授权状态" />
        <PageState
          v-else-if="error"
          state="error"
          title="渠道信息加载失败"
          :detail="error.message"
          retryable
          @retry="reload"
        />
        <PageState
          v-else-if="!channels.length"
          state="empty"
          title="尚未配置任何渠道"
          description="渠道授权状态由服务端同步，此处只做展示。"
        />
        <ChannelTable
          v-else
          :channels="channels"
          :editable="editable"
          :pending-channel="pendingChannel"
          @toggle-provider="onToggleProvider"
        />
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;
</style>
