<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import SessionFilter from '@/components/session/SessionFilter.vue'
import SessionList from '@/components/session/SessionList.vue'
import MessageStream from '@/components/session/MessageStream.vue'
import TakeoverButton from '@/components/session/TakeoverButton.vue'
import OwnerBadge from '@/components/session/OwnerBadge.vue'
import KbSearchPanel from '@/components/session/KbSearchPanel.vue'
import ContextCard from '@/components/context/ContextCard.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchSessionContext, fetchSessionDetail } from '@/api/sessions'
import { sendChatTurn } from '@/api/chat'
import { useChatStream } from '@/composables/useChatStream'
import { useAsyncData } from '@/composables/useAsyncData'
import { useAuthStore } from '@/stores/auth'
import { canDo } from '@/permission/matrix'
import { formatDuration } from '@/utils/format'
import {
  CHANNEL_LABELS,
  type ChatMessage,
  type HandoffReason,
  type SessionContext,
  type SessionListQuery,
} from '@/types/contract'

/**
 * 会话工作台 —— FE-01 / FE-02 / FE-03 / FE-04 的汇合页。
 *
 * 布局（对齐任务书 4.2 P1 + P2）：
 *   ┌──────────┬─────────────────────┬──────────────┐
 *   │ 会话列表  │ 实时消息流 + 回复框  │ AI 上下文卡片 │
 *   │ FE-01    │ FE-01               │ FE-02 ★      │
 *   │          │                     │ 知识检索 FE-04│
 *   └──────────┴─────────────────────┴──────────────┘
 *
 * 两个路由（session-list / session-detail）复用本组件：
 *   有 sessionId 参数就打开该会话，没有就只显示列表 + 空态。
 *   这样从工单页跳「前往关联会话」也能直接落到同一个页面。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const sessionId = computed(() => String(route.params.sessionId ?? ''))

/** 当前会话列表查询条件（筛选条抛出） */
const listQuery = ref<Partial<SessionListQuery>>({})

/** 坐席只能看本组会话（4.5.4）；主管/管理员不限制 */
const scopeGroupId = computed(() => (auth.role === 'AGENT' ? (auth.user?.group_id ?? '') : ''))

const canTakeover = computed(() => canDo(auth.role, 'takeover.execute'))

// ── 会话详情 ────────────────────────────────────────────────
const detail = useAsyncData(
  (signal) => (sessionId.value ? fetchSessionDetail(sessionId.value, { signal }) : Promise.resolve(null)),
  { immediate: false, initial: null },
)

const session = computed(() => detail.data.value?.session ?? null)
const buyer = computed(() => detail.data.value?.buyer ?? null)
const messages = ref<ChatMessage[]>([])

// ── AI 上下文卡片（FE-02）───────────────────────────────────
const context = useAsyncData<Partial<SessionContext> | null>(
  (signal) => (sessionId.value ? fetchSessionContext(sessionId.value, { signal }) : Promise.resolve(null)),
  { immediate: false, initial: null },
)

// ── 实时消息流（FE-01 SSE）──────────────────────────────────
const stream = useChatStream(sessionId.value, {
  onTurnEnd: (payload) => {
    // 落库以 turn.end 的完整 reply 为准（任务书 2.5.4）
    if (!sessionId.value) return
    messages.value = [
      ...messages.value,
      {
        message_id: `local-bot-${Date.now()}`,
        session_id: sessionId.value,
        sender: 'BOT',
        msg_type: 'TEXT',
        text: payload.reply,
        text_source: 'ORIGINAL',
        image_ref: null,
        image_alt: null,
        created_at: new Date().toISOString(),
        trace_id: null,
        chunk_ids: [],
        decision: null,
        handoff_reason: null,
        latency_ms: payload.latency_ms,
        feedback_marked: false,
      },
    ]
  },
  onHandoff: (payload) => {
    // 转人工：插入卡片并刷新会话归属（机器人可能已交出会话）
    if (!sessionId.value) return
    messages.value = [
      ...messages.value,
      {
        message_id: `local-handoff-${Date.now()}`,
        session_id: sessionId.value,
        sender: 'BOT',
        msg_type: 'TEXT',
        text: `已为您转接人工客服，当前排队 ${payload.queue_position} 位。`,
        text_source: 'ORIGINAL',
        image_ref: null,
        image_alt: null,
        created_at: new Date().toISOString(),
        trace_id: null,
        chunk_ids: [],
        decision: null,
        handoff_reason: payload.reason as HandoffReason,
        latency_ms: null,
        feedback_marked: false,
      },
    ]
    void detail.reload()
  },
})

// ── 加载当前会话 ────────────────────────────────────────────
async function loadSession(id: string): Promise<void> {
  stream.close()
  if (!id) {
    messages.value = []
    return
  }
  await Promise.all([detail.reload(), context.reload()])
  messages.value = detail.data.value?.messages ?? []
  stream.connect()
}

watch(
  sessionId,
  (id) => {
    void loadSession(id)
  },
  { immediate: true },
)

onBeforeUnmount(() => stream.close())

function openSession(id: string): void {
  if (id === sessionId.value) return
  void router.push({ name: 'session-detail', params: { sessionId: id } })
}

/** 接管/释放成功后：刷新会话与上下文（归属、bot_muted 都会变） */
async function refreshSession(): Promise<void> {
  await Promise.all([detail.reload(), context.reload()])
  messages.value = detail.data.value?.messages ?? []
}

// ── 回复框 ──────────────────────────────────────────────────
const draft = ref('')
const sending = ref(false)

/** 接管后进入「坐席回复」模式（任务书 FE-03） */
const isAgentMode = computed(() => session.value?.owner === 'AGENT')

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!text || !sessionId.value || sending.value) return

  sending.value = true
  try {
    const result = await sendChatTurn({
      session_id: sessionId.value,
      text,
      msg_type: 'TEXT',
      sender: isAgentMode.value ? 'AGENT' : 'USER',
    })
    messages.value = [
      ...messages.value,
      {
        message_id: result.message_id,
        session_id: sessionId.value,
        sender: isAgentMode.value ? 'AGENT' : 'USER',
        msg_type: 'TEXT',
        text,
        text_source: 'ORIGINAL',
        image_ref: null,
        image_alt: null,
        created_at: result.created_at,
        trace_id: result.trace_id,
        chunk_ids: [],
        decision: null,
        handoff_reason: null,
        latency_ms: null,
        feedback_marked: false,
      },
    ]
    draft.value = ''
    // 机器人接待模式下，发送后应进入「正在输入」等待回复
    if (!isAgentMode.value) stream.beginLocalTurn()
  } catch (caught) {
    ElMessage.error(caught instanceof Error ? caught.message : '消息发送失败')
  } finally {
    sending.value = false
  }
}

/** FE-04：知识检索一键填入（原文，不加工） */
function fillFromKb(text: string): void {
  draft.value = text
}

function openReplay(): void {
  if (!sessionId.value) return
  void router.push({ name: 'session-replay', params: { sessionId: sessionId.value } })
}

function openTicket(): void {
  const ticketNo = session.value?.ticket_no
  if (!ticketNo) return
  void router.push({ name: 'ticket-detail', params: { ticketNo } })
}
</script>

<template>
  <div class="workbench">
    <!-- 左栏：会话列表（FE-01 P1） -->
    <aside class="workbench__list">
      <SessionFilter
        :show-group="auth.role !== 'AGENT'"
        @change="(query) => (listQuery = query)"
      />
      <SessionList
        :active-id="sessionId"
        :query="listQuery"
        :scope-group-id="scopeGroupId"
        @select="openSession"
      />
    </aside>

    <!-- 中栏：实时消息流 + 回复框（FE-01 P2） -->
    <section class="workbench__main">
      <template v-if="!sessionId">
        <PageState
          state="empty"
          title="请选择左侧会话"
          description="选中后即可查看实时消息、AI 上下文与知识检索"
        />
      </template>

      <template v-else>
        <header v-if="session" class="main-head">
          <div class="main-head__left">
            <span class="main-head__buyer">{{ session.buyer_display }}</span>
            <span class="main-head__channel">{{ CHANNEL_LABELS[session.channel] }}</span>
            <OwnerBadge
              :owner="session.owner"
              :agent-name="session.agent_name"
              :bot-muted="session.bot_muted"
              :current-agent-id="auth.user?.user_id"
              :owner-agent-id="session.agent_id"
              compact
            />
          </div>
          <div class="main-head__right">
            <span v-if="session.waiting_seconds > 0" class="main-head__waiting">
              已等待 {{ formatDuration(session.waiting_seconds) }}
            </span>
            <el-button v-if="session.ticket_no" size="small" text @click="openTicket">
              工单 {{ session.ticket_no }}
            </el-button>
            <el-button size="small" text @click="openReplay">回放</el-button>
            <TakeoverButton
              :session="session"
              :current-agent-id="auth.user?.user_id ?? ''"
              :current-agent-name="auth.user?.display_name ?? ''"
              :allowed="canTakeover"
              @taken-over="refreshSession"
              @released="refreshSession"
              @need-refresh="refreshSession"
            />
          </div>
        </header>

        <MessageStream
          class="workbench__stream"
          :messages="messages"
          :loading="detail.loading.value"
          :error="detail.error.value?.message ?? null"
          :typing="stream.typing.value"
          :stage="stream.stage.value"
          :retrieval-hits="stream.retrievalHits.value"
          :retrieval-top-score="stream.retrievalTopScore.value"
          :intent="stream.intent.value"
          :confidence="stream.confidence.value"
          :streaming-text="stream.streamingText.value"
          :stream-error="stream.streamError.value"
          :feedback-enabled="true"
          @retry-stream="stream.retry"
        />

        <footer class="composer">
          <!-- 接管后切换「坐席回复」模式，这是 FE-03 的可见证据 -->
          <div class="composer__mode" :class="{ 'is-agent': isAgentMode }">
            {{ isAgentMode ? '坐席回复模式 · 以坐席身份发送' : '机器人接待中 · 发送将触发机器人回复' }}
          </div>
          <div class="composer__row">
            <el-input
              v-model="draft"
              type="textarea"
              :rows="3"
              resize="none"
              :placeholder="
                isAgentMode
                  ? '输入回复内容，将以「坐席」身份发送给买家'
                  : '此处可测试机器人回复；若需人工回复，请先接管会话'
              "
              @keydown.enter.exact.prevent="send"
            />
            <el-button
              type="primary"
              :loading="sending"
              :disabled="!draft.trim()"
              class="composer__send"
              @click="send"
            >
              发送
            </el-button>
          </div>
          <div class="composer__hint">
            不生成任何外链或跳转卡片；发送内容中若含链接将被屏蔽为「[链接已屏蔽]」。
          </div>
        </footer>
      </template>
    </section>

    <!-- 右栏：AI 上下文卡片（FE-02 ★）+ 知识检索（FE-04） -->
    <aside class="workbench__side">
      <template v-if="!sessionId">
        <PageState state="empty" title="尚未选择会话" description="AI 上下文与知识检索会在此展示" />
      </template>
      <template v-else>
        <SectionCard title="AI 上下文" hint="接管前必读" class="side__card">
          <ContextCard
            :context="context.data.value"
            :session-id="sessionId"
            :loading="context.loading.value"
            :error="context.error.value?.message ?? null"
            @retry="context.reload"
          />
        </SectionCard>

        <SectionCard title="知识检索" hint="只展示已通过审核的条目" class="side__card side__card--grow">
          <KbSearchPanel :session-id="sessionId" dense @fill="fillFromKb" />
        </SectionCard>

        <SectionCard v-if="buyer" title="买家信息" hint="已脱敏" class="side__card">
          <dl class="buyer">
            <div class="buyer__row">
              <dt>买家</dt>
              <dd>{{ buyer.buyer_display }}</dd>
            </div>
            <div class="buyer__row">
              <dt>渠道</dt>
              <dd>{{ CHANNEL_LABELS[buyer.channel] }}</dd>
            </div>
            <div class="buyer__row">
              <dt>地区</dt>
              <dd>{{ buyer.region }}</dd>
            </div>
            <div class="buyer__row">
              <dt>会员等级</dt>
              <dd>{{ buyer.member_level }}</dd>
            </div>
          </dl>
        </SectionCard>
      </template>
    </aside>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.workbench {
  display: flex;
  height: 100%;
  min-height: 0;
  background: $surface-sunken;

  &__list {
    flex: none;
    width: 320px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-right: 1px solid $ink-150;
    background: $surface;
  }

  &__main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: $surface;
  }

  &__stream {
    flex: 1;
    min-height: 0;
  }

  &__side {
    flex: none;
    width: 380px;
    display: flex;
    flex-direction: column;
    gap: $space-3;
    padding: $space-3;
    min-height: 0;
    overflow-y: auto;
    border-left: 1px solid $ink-150;
    background: $surface-sunken;
  }
}

.main-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: $space-3;
  padding: $space-2 $space-4;
  border-bottom: 1px solid $ink-150;
  background: $surface;

  &__left,
  &__right {
    display: flex;
    align-items: center;
    gap: $space-2;
    min-width: 0;
  }

  &__buyer {
    font-size: $text-md;
    font-weight: 600;
    color: $ink-900;
  }

  &__channel {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__waiting {
    font-size: $text-xs;
    color: $danger-ink;
    font-weight: 600;
  }
}

.composer {
  flex: none;
  padding: $space-3 $space-4 $space-4;
  border-top: 1px solid $ink-150;
  background: $surface;

  &__mode {
    margin-bottom: $space-2;
    padding: 2px 8px;
    display: inline-block;
    border-radius: $radius-pill;
    background: $mint-bg;
    color: $mint-ink;
    font-size: 11px;
    font-weight: 600;

    &.is-agent {
      background: $brand-50;
      color: $brand-ink;
    }
  }

  &__row {
    display: flex;
    align-items: flex-end;
    gap: $space-2;
  }

  &__send {
    flex: none;
    height: 32px;
  }

  &__hint {
    margin-top: $space-2;
    font-size: 11px;
    color: $ink-400;
  }
}

.side {
  &__card {
    flex: none;

    &--grow {
      flex: 1;
      min-height: 220px;
    }
  }
}

.buyer {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: $space-1;

  &__row {
    display: flex;
    gap: $space-2;
    font-size: $text-xs;

    dt {
      flex: none;
      width: 64px;
      color: $ink-500;
    }

    dd {
      margin: 0;
      color: $ink-800;
    }
  }
}
</style>
