<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import PageState from '@/components/common/PageState.vue'
import { searchKb } from '@/api/kb'
import { KB_TYPE_LABELS, type KbSearchHit, type KbSearchResponse } from '@/types/contract'

/**
 * 知识检索侧栏 —— FE-04。
 *
 * 任务书要求：
 *   「300ms 防抖实时检索」
 *   「展示命中分 / category / authority 溯源」
 *   「按 category 筛选」
 *   「一键填入回复框，不改原文」
 *   DoD：检索 P95 ≤500ms；只展示 review_status=APPROVED 条目。
 *
 * 关键设计决策：
 *
 * 1. 「只展示 APPROVED」不在前端过滤，而是**信任 BFF 的过滤**并做二次断言。
 *    原因：HC-3 要求未审核条目绝不进入坐席可见范围，这是数据层面的约束；
 *    前端过滤只是兜底 —— 若 BFF 漏了，前端仍会拦下并告警，但绝不能依赖前端。
 *    因此这里既请求 BFF（它自己会过滤），又在渲染前过滤一遍。
 *
 * 2. 一键填入只 emit 原文，不做任何加工。
 *    任务书明确「不改原文」。坐席可能需要按场景微调，任何自动改写
 *    （加称呼、加表情）都会让坐席失去对内容的掌控，且可能引入未审核口径。
 *
 * 3. 无依据（no_evidence）要显式展示，而不是显示空列表。
 *    任务书指标里有「无依据拒答率 100%」，坐席必须知道「知识库没有依据」，
 *    这样才能判断该转人工还是该补知识，而不是以为检索坏了。
 */
const props = defineProps<{
  /** 会话 ID，用于埋点与反馈关联 */
  sessionId?: string
  /** 是否紧凑（侧栏） */
  dense?: boolean
}>()

const emit = defineEmits<{
  /** 一键填入回复框（原文，不加工） */
  fill: [text: string]
  /** 点击条目查看详情 */
  open: [chunkId: string]
}>()

const keyword = ref('')
const category = ref('')
const hits = ref<KbSearchHit[]>([])
const latencyMs = ref<number | null>(null)
const noEvidence = ref(false)
const loading = ref(false)
const searched = ref(false)
const error = ref<string | null>(null)

const DEBOUNCE_MS = 300

let timer: number | null = null
let requestSeq = 0

/** 从命中结果里提取 category 选项（BFF 未提供分类字典接口，故从结果里归纳） */
const categoryOptions = computed(() => {
  const set = new Set<string>()
  for (const hit of hits.value) {
    if (hit.category) set.add(hit.category)
  }
  return [...set]
})

/** 二次断言：只展示已通过审核的条目（兜底，主过滤在 BFF） */
const visibleHits = computed(() => hits.value.filter((hit) => hit.review_status === 'APPROVED'))

async function runSearch(): Promise<void> {
  const query = keyword.value.trim()
  if (!query) {
    hits.value = []
    noEvidence.value = false
    searched.value = false
    latencyMs.value = null
    return
  }

  const seq = ++requestSeq
  loading.value = true
  error.value = null
  try {
    const result: KbSearchResponse = await searchKb({
      query,
      category: category.value || undefined,
      session_id: props.sessionId,
      top_k: 8,
    })
    // 丢弃过期响应，避免快速输入时旧结果覆盖新结果
    if (seq !== requestSeq) return
    hits.value = result.hits
    latencyMs.value = result.latency_ms
    noEvidence.value = result.no_evidence
    searched.value = true
  } catch (caught) {
    if (seq !== requestSeq) return
    error.value = caught instanceof Error ? caught.message : '知识检索失败'
    hits.value = []
    noEvidence.value = false
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function onInput(): void {
  if (timer !== null) window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    timer = null
    void runSearch()
  }, DEBOUNCE_MS)
}

watch(category, () => void runSearch())

/** 命中分 -> 语义色：≥0.8 高可信、≥0.6 中等、其余偏低 */
function scoreTone(score: number): 'ok' | 'warn' | 'bad' {
  if (score >= 0.8) return 'ok'
  if (score >= 0.6) return 'warn'
  return 'bad'
}

/** 匹配方式中文口径 */
const MATCHED_BY_LABELS: Record<KbSearchHit['matched_by'], string> = {
  vector: '语义匹配',
  keyword: '关键词命中',
  sku: 'SKU 精确',
  faq: 'FAQ 标题',
}

function fill(hit: KbSearchHit): void {
  // 原文填入，不做任何加工（任务书：「一键填入不改原文」）
  emit('fill', hit.content)
}
</script>

<template>
  <div class="kb-search" :class="{ 'is-dense': props.dense }">
    <div class="kb-search__bar">
      <el-input
        v-model="keyword"
        placeholder="检索知识库（商品 / 政策 / FAQ）"
        clearable
        size="small"
        @input="onInput"
        @keyup.enter="runSearch"
        @clear="runSearch"
      >
        <template #prefix>
          <span class="kb-search__icon">🔍</span>
        </template>
      </el-input>
    </div>

    <div v-if="categoryOptions.length > 1" class="kb-search__cats">
      <span
        class="kb-search__cat"
        :class="{ 'is-active': category === '' }"
        @click="category = ''"
      >
        全部
      </span>
      <span
        v-for="item in categoryOptions"
        :key="item"
        class="kb-search__cat"
        :class="{ 'is-active': category === item }"
        @click="category = item"
      >
        {{ item }}
      </span>
    </div>

    <!-- 检索耗时（DoD：P95 ≤500ms，坐席与测试都能直接看到） -->
    <div v-if="searched && latencyMs !== null" class="kb-search__latency">
      检索耗时 <span class="wk-mono">{{ latencyMs }}ms</span>
      · 命中 {{ visibleHits.length }} 条
    </div>

    <PageState v-if="loading && !searched" state="loading" title="正在检索…" />
    <PageState v-else-if="error" state="error" :detail="error" retryable @retry="runSearch" />

    <!-- 无依据：显式告知，而不是空列表 -->
    <div v-else-if="noEvidence" class="kb-search__no-evidence">
      <span class="kb-search__no-evidence-icon">🚫</span>
      <span class="kb-search__no-evidence-title">知识库中没有依据</span>
      <span class="kb-search__no-evidence-desc">
        未命中可信知识条目。请勿凭记忆作答，建议转人工或先补充知识库。
      </span>
    </div>

    <PageState
      v-else-if="!searched"
      state="empty"
      title="输入关键词开始检索"
      description="命中结果会展示来源与可信度，可一键填入回复框"
    />
    <PageState
      v-else-if="visibleHits.length === 0"
      state="empty"
      title="没有已通过审核的条目"
      description="命中的条目可能尚未审核，不会展示给坐席"
    />

    <ul v-else class="kb-search__hits">
      <li v-for="hit in visibleHits" :key="hit.chunk_id" class="hit">
        <div class="hit__head">
          <span class="hit__title" @click="emit('open', hit.chunk_id)">{{ hit.title }}</span>
          <span class="hit__score" :class="`hit__score--${scoreTone(hit.score)}`">
            {{ hit.score.toFixed(2) }}
          </span>
        </div>

        <p class="hit__snippet">{{ hit.snippet }}</p>

        <div class="hit__meta">
          <EnumTag kind="tone" value="info" :label="KB_TYPE_LABELS[hit.kb_type]" />
          <span v-if="hit.category" class="hit__tag">{{ hit.category }}</span>
          <span class="hit__tag hit__tag--match">{{ MATCHED_BY_LABELS[hit.matched_by] }}</span>
        </div>

        <!-- 溯源：authority 是「这条知识凭什么可信」的依据 -->
        <div v-if="hit.authority" class="hit__authority" :title="hit.authority">
          依据：{{ hit.authority }}
        </div>

        <div class="hit__actions">
          <el-button size="small" type="primary" plain @click="fill(hit)">
            填入回复框
          </el-button>
          <span class="hit__id wk-mono">{{ hit.chunk_id }}</span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.kb-search {
  display: flex;
  flex-direction: column;
  gap: $space-2;
  min-height: 0;

  &.is-dense {
    gap: $space-1;
  }

  &__icon {
    font-size: 12px;
  }

  &__cats {
    display: flex;
    flex-wrap: wrap;
    gap: $space-1;
  }

  &__cat {
    padding: 1px 8px;
    border-radius: $radius-pill;
    background: $ink-100;
    color: $ink-600;
    font-size: 11px;
    cursor: pointer;

    &:hover {
      background: $ink-150;
    }

    &.is-active {
      background: $brand-500;
      color: #fff;
    }
  }

  &__latency {
    font-size: 11px;
    color: $ink-500;
  }

  &__no-evidence {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: $space-1;
    padding: $space-5 $space-3;
    border-radius: $radius-sm;
    background: $warning-bg;
    text-align: center;
  }

  &__no-evidence-icon {
    font-size: 22px;
  }

  &__no-evidence-title {
    font-size: $text-sm;
    font-weight: 600;
    color: $warning-ink;
  }

  &__no-evidence-desc {
    font-size: $text-xs;
    line-height: 1.6;
    color: $ink-600;
  }

  &__hits {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: $space-2;
    overflow-y: auto;
    min-height: 0;
  }
}

.hit {
  padding: $space-2 $space-3;
  border: 1px solid $ink-150;
  border-radius: $radius-sm;
  background: $surface;

  &:hover {
    border-color: $brand-200;
  }

  &__head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: $space-2;
  }

  &__title {
    font-size: $text-sm;
    font-weight: 600;
    color: $ink-900;
    cursor: pointer;

    &:hover {
      color: $brand-ink;
      text-decoration: underline;
    }
  }

  &__score {
    flex: none;
    padding: 0 5px;
    border-radius: $radius-xs;
    font-family: $font-mono;
    font-size: 11px;
    font-weight: 700;

    &--ok {
      background: $success-bg;
      color: $success-ink;
    }

    &--warn {
      background: $warning-bg;
      color: $warning-ink;
    }

    &--bad {
      background: $danger-bg;
      color: $danger-ink;
    }
  }

  &__snippet {
    margin: $space-1 0 0;
    font-size: $text-xs;
    line-height: 1.6;
    color: $ink-600;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  &__meta {
    display: flex;
    align-items: center;
    gap: $space-1;
    margin-top: $space-2;
    flex-wrap: wrap;
  }

  &__tag {
    padding: 0 6px;
    border-radius: $radius-xs;
    background: $ink-100;
    color: $ink-600;
    font-size: 11px;

    &--match {
      background: $info-bg;
      color: $info-ink;
    }
  }

  &__authority {
    margin-top: $space-1;
    font-size: 11px;
    color: $ink-500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: $space-2;
    margin-top: $space-2;
  }

  &__id {
    font-size: 10px;
    color: $ink-400;
  }
}
</style>
