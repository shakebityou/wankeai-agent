<script setup lang="ts">
import { computed } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { AUTH_STATUS_LABELS, type ChannelAuth } from '@/types/contract'
import { formatDate } from '@/utils/format'

/**
 * 渠道授权表格 —— FE-11（P11 渠道与授权管理）。
 *
 * 任务书：ChannelTable.vue（5 渠道：淘宝/天猫/拼多多/抖音/微信）+ AuthStatusTag.vue + ExpiryAlert.vue
 * DoD：「5 渠道全展示到期日准确」「到期前 15 天提醒（mock 时间验证）」
 *      「抖音认证状态可手动更新并留日志」「**不展示任何 AppSecret/Token 明文**」
 *
 * 设计决策：
 *   1. **只展示 callback_url_masked，且密钥段固定为 `***`** —— 后端下发的就是掩码串。
 *      组件不做任何「拼接完整 URL」的尝试，也不提供「查看完整值」的入口。
 *      这是 4.5.2 合规红线的核心，UI 上不留任何缝。
 *   2. **到期提醒用绝对天数而非相对文案** —— 「还剩 9 天」比「即将到期」可行动得多，
 *      因为抖音小店的服务商认证是关键路径（10 工作日），运营需要拿天数倒推排期。
 *   3. **抖音认证状态就地可改** —— 任务书明确要求「可手动更新并留日志」，
 *      这是唯一一个前端能改的渠道字段。
 *   4. **过期渠道整行标红** —— 微信客服已过期 -12 天意味着该渠道实际上收不到消息，
 *      这是 P0 级别的问题，必须在表格里一眼看到。
 */
interface Props {
  channels: ChannelAuth[]
  /** 是否允许修改（ADMIN） */
  editable?: boolean
  /** 正在提交的渠道 */
  pendingChannel?: string
}

const props = withDefaults(defineProps<Props>(), { editable: false, pendingChannel: '' })

const emit = defineEmits<{
  (e: 'toggle-provider', channel: ChannelAuth, certified: boolean): void
}>()

/** 到期提醒窗口（任务书：到期前 15 天自动提醒） */
const EXPIRY_WINDOW_DAYS = 15

function expiryTone(channel: ChannelAuth): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (channel.days_to_expiry === null) return 'neutral'
  if (channel.days_to_expiry < 0) return 'bad'
  if (channel.days_to_expiry <= EXPIRY_WINDOW_DAYS) return 'warn'
  return 'ok'
}

function expiryText(channel: ChannelAuth): string {
  if (channel.expires_at === null) return '未设置'
  const days = channel.days_to_expiry
  if (days === null) return channel.expires_at
  if (days < 0) return `${channel.expires_at}（已过期 ${Math.abs(days)} 天）`
  if (days === 0) return `${channel.expires_at}（今天到期）`
  return `${channel.expires_at}（还剩 ${days} 天）`
}

/** 需要提醒的渠道（在 15 天窗口内或已过期） */
const alerting = computed(() =>
  props.channels.filter(
    (channel) =>
      channel.days_to_expiry !== null &&
      channel.days_to_expiry <= EXPIRY_WINDOW_DAYS,
  ),
)

/** 缺少回调配置或 IP 白名单的渠道 —— 这些是接入不完整的信号 */
const misconfigured = computed(() =>
  props.channels.filter((channel) => !channel.callback_configured || !channel.ip_whitelist_configured),
)
</script>

<template>
  <div class="channels">
    <!-- 到期提醒 -->
    <el-alert
      v-if="alerting.length"
      type="warning"
      show-icon
      :closable="false"
      class="channels__alert"
    >
      <template #title>
        有 {{ alerting.length }} 个渠道将在 {{ EXPIRY_WINDOW_DAYS }} 天内到期或已过期，请尽快续期
      </template>
      <ul class="channels__alert-list">
        <li v-for="channel in alerting" :key="channel.channel">
          <EnumTag kind="channel" :value="channel.channel" />
          <span>{{ AUTH_STATUS_LABELS[channel.auth_status] }}</span>
          <span class="wk-mono">{{ expiryText(channel) }}</span>
        </li>
      </ul>
    </el-alert>

    <el-alert
      v-if="misconfigured.length"
      type="info"
      show-icon
      :closable="false"
      :title="`${misconfigured.length} 个渠道的回调地址或 IP 白名单尚未配置完成，会导致消息无法接收`"
    />

    <el-table :data="props.channels" size="small" class="channels__table">
      <el-table-column label="渠道" width="110">
        <template #default="{ row }">
          <EnumTag kind="channel" :value="(row as ChannelAuth).channel" />
        </template>
      </el-table-column>

      <el-table-column label="授权状态" width="106">
        <template #default="{ row }">
          <span class="status" :class="`status--${expiryTone(row as ChannelAuth)}`">
            {{ AUTH_STATUS_LABELS[(row as ChannelAuth).auth_status] }}
          </span>
        </template>
      </el-table-column>

      <el-table-column label="授权到期日" min-width="200">
        <template #default="{ row }">
          <span class="expiry" :class="`expiry--${expiryTone(row as ChannelAuth)}`">
            {{ expiryText(row as ChannelAuth) }}
          </span>
        </template>
      </el-table-column>

      <el-table-column label="权限包" min-width="200">
        <template #default="{ row }">
          <div class="packages">
            <span v-for="pkg in (row as ChannelAuth).permission_packages" :key="pkg" class="packages__item">
              {{ pkg }}
            </span>
            <span v-if="!(row as ChannelAuth).permission_packages.length" class="packages__none">未申请</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="回调地址" min-width="230">
        <template #default="{ row }">
          <div class="callback">
            <span v-if="(row as ChannelAuth).callback_configured" class="callback__url wk-mono">
              {{ (row as ChannelAuth).callback_url_masked }}
            </span>
            <span v-else class="callback__missing">未配置</span>
            <span class="callback__note">密钥段一律脱敏展示</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="出口 IP 白名单" width="120">
        <template #default="{ row }">
          <span v-if="(row as ChannelAuth).ip_whitelist_configured" class="ip">
            已配置（{{ (row as ChannelAuth).ip_whitelist_count }} 个）
          </span>
          <span v-else class="ip ip--missing">未配置</span>
        </template>
      </el-table-column>

      <el-table-column label="服务商认证" width="170">
        <template #default="{ row }">
          <div v-if="(row as ChannelAuth).service_provider_certified !== null" class="provider">
            <el-switch
              :model-value="(row as ChannelAuth).service_provider_certified === true"
              :disabled="!props.editable || props.pendingChannel === (row as ChannelAuth).channel"
              size="small"
              inline-prompt
              active-text="已认证"
              inactive-text="审核中"
              @update:model-value="emit('toggle-provider', row as ChannelAuth, $event as boolean)"
            />
            <span v-if="(row as ChannelAuth).service_provider_note" class="provider__note">
              {{ (row as ChannelAuth).service_provider_note }}
            </span>
          </div>
          <span v-else class="provider__na">不适用</span>
        </template>
      </el-table-column>

      <el-table-column label="最近更新" width="170">
        <template #default="{ row }">
          <div class="updated">
            <span class="wk-mono">{{ formatDate((row as ChannelAuth).updated_at) }}</span>
            <span class="updated__by">{{ (row as ChannelAuth).updated_by }}</span>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <p class="channels__foot">
      出于安全与合规要求，本页不展示任何 AppSecret / AccessToken 明文，也不提供查看完整凭证的入口。
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.channels {
  display: flex;
  flex-direction: column;
  gap: $space-3;

  &__alert {
    margin: 0;
  }

  &__alert-list {
    margin: $space-1 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: $text-xs;

    li {
      display: flex;
      align-items: center;
      gap: $space-2;
    }
  }

  &__table {
    width: 100%;
  }

  &__foot {
    margin: 0;
    font-size: 11px;
    color: $ink-400;
  }
}

.status {
  font-size: $text-xs;
  font-weight: 600;

  &--ok {
    color: $success-ink;
  }

  &--warn {
    color: $warning-ink;
  }

  &--bad {
    color: $danger-ink;
  }

  &--neutral {
    color: $ink-500;
  }
}

.expiry {
  font-size: $text-xs;
  font-family: $font-mono;

  &--ok {
    color: $ink-700;
  }

  &--warn {
    color: $warning-ink;
    font-weight: 600;
  }

  &--bad {
    color: $danger-ink;
    font-weight: 700;
  }

  &--neutral {
    color: $ink-500;
  }
}

.packages {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;

  &__item {
    padding: 0 6px;
    border-radius: $radius-xs;
    background: $ink-100;
    font-size: 11px;
    color: $ink-700;
  }

  &__none {
    font-size: 11px;
    color: $ink-400;
  }
}

.callback {
  display: flex;
  flex-direction: column;
  gap: 1px;

  &__url {
    font-size: 11px;
    color: $ink-700;
    word-break: break-all;
  }

  &__missing {
    font-size: 11px;
    color: $danger-ink;
  }

  &__note {
    font-size: 10px;
    color: $ink-400;
  }
}

.ip {
  font-size: 11px;
  color: $ink-700;

  &--missing {
    color: $danger-ink;
  }
}

.provider {
  display: flex;
  flex-direction: column;
  gap: 2px;

  &__note {
    font-size: 10px;
    line-height: 1.5;
    color: $ink-500;
  }

  &__na {
    font-size: 11px;
    color: $ink-400;
  }
}

.updated {
  display: flex;
  flex-direction: column;
  gap: 1px;
  font-size: 11px;
  color: $ink-600;

  &__by {
    color: $ink-400;
  }
}
</style>
