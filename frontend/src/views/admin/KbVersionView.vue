<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import VersionTimeline from '@/components/kb/VersionTimeline.vue'
import DiffViewer from '@/components/kb/DiffViewer.vue'
import PageState from '@/components/common/PageState.vue'
import { fetchKbHistory, rollbackKbChunk } from '@/api/kb'
import { useAsyncData } from '@/composables/useAsyncData'
import { ElMessage } from 'element-plus'
import type { KbHistoryResponse, KbVersion } from '@/types/contract'

/**
 * 版本与生效时间管理 —— FE-07（P8）。
 *
 * 任务书：VersionTimeline.vue + DiffViewer.vue + RollbackButton.vue
 * DoD：「版本历史完整」「回滚后 5 分钟内生效」「差异对比新增/删除/修改三色标注」
 *
 * 设计决策：
 *   1. **默认展示「当前版本 vs 上一版本」的差异** —— 运营点进版本页最想看的是
 *      「最近改了什么」，而不是先看到一条版本列表再自己选。
 *   2. **回滚在时间轴上就地确认（el-popconfirm）而非新页面** —— 回滚是低频但危险的操作，
 *      就地确认既保留上下文，又比二次弹窗轻。
 *   3. **生效时间在时间轴上直接标出** —— FE-07 的名字就是「版本与生效时间」，
 *      版本号和生效日期必须一起看，否则运营会以为「v2 生效」等于「立刻生效」。
 */
const route = useRoute()
const chunkId = computed(() => String(route.params.chunkId ?? ''))

const { data, showSkeleton, error, isEmpty, reload } = useAsyncData<KbHistoryResponse | null>(
  (signal) => fetchKbHistory(chunkId.value, { signal }),
  { initial: null },
)

watch(chunkId, () => reload())

/** 当前对比的两个版本 */
const leftVersion = ref<KbVersion | null>(null)
const rightVersion = ref<KbVersion | null>(null)

const versions = computed(() => data.value?.versions ?? [])

/** 默认对比：最新两个版本 */
watch(
  versions,
  (list) => {
    if (!list.length) return
    const sorted = [...list].sort((a, b) => b.version - a.version)
    leftVersion.value = sorted[1] ?? sorted[0]
    rightVersion.value = sorted[0]
  },
  { immediate: true },
)

function onCompare(oldVersion: number, newVersion: number): void {
  const old = versions.value.find((item) => item.version === oldVersion) ?? null
  const next = versions.value.find((item) => item.version === newVersion) ?? null
  if (!old || !next) return
  leftVersion.value = old
  rightVersion.value = next
}

async function onRollback(version: number): Promise<void> {
  try {
    await rollbackKbChunk(chunkId.value, version)
    ElMessage.success(`已回滚到 v${version}，将在 5 分钟内生效`)
    await reload()
  } catch {
    // 错误提示由拦截器统一处理
  }
}
</script>

<template>
  <div class="wk-page">
    <header class="wk-page__header">
      <div>
        <h1 class="wk-page__title">版本与生效时间</h1>
        <p class="wk-page__subtitle">
          <span class="wk-mono">{{ chunkId }}</span>
          —— 每次内容变更都会生成一个新版本，回滚同样会生成新版本而不是直接覆盖。
        </p>
      </div>
      <el-button size="small" @click="reload">刷新</el-button>
    </header>

    <PageState v-if="showSkeleton" state="loading" title="正在加载版本历史" />
    <PageState
      v-else-if="error"
      state="error"
      title="版本历史加载失败"
      :detail="error.message"
      retryable
      @retry="reload"
    />
    <PageState
      v-else-if="isEmpty"
      state="empty"
      title="该条目还没有版本历史"
      description="首次创建后即可查看版本记录。"
    />

    <template v-else>
      <div class="versions__layout">
        <section class="wk-card versions__timeline">
          <div class="wk-card__header">
            <span class="wk-card__title">版本时间轴</span>
            <span class="wk-card__hint">当前 v{{ data?.current_version }}</span>
          </div>
          <div class="wk-card__body">
            <VersionTimeline
              :versions="versions"
              :current-version="data?.current_version ?? 1"
              :rolling="false"
              @compare="onCompare"
              @rollback="onRollback"
            />
          </div>
        </section>

        <section class="wk-card versions__diff">
          <div class="wk-card__header">
            <span class="wk-card__title">差异对比</span>
            <span class="wk-card__hint">
              v{{ leftVersion?.version ?? '-' }} → v{{ rightVersion?.version ?? '-' }}
            </span>
          </div>
          <div class="wk-card__body">
            <DiffViewer v-if="leftVersion && rightVersion" :old-version="leftVersion" :new-version="rightVersion" />
            <p v-else class="versions__hint">选择两个版本以对比差异。</p>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.versions {
  &__layout {
    display: grid;
    grid-template-columns: 340px 1fr;
    gap: $space-3;
    align-items: start;
  }

  &__timeline,
  &__diff {
    min-width: 0;
  }

  &__hint {
    margin: 0;
    font-size: $text-xs;
    color: $ink-500;
  }
}
</style>
