<script setup lang="ts">
import { computed } from 'vue'
import EnumTag from '@/components/common/EnumTag.vue'
import { formatDateTime } from '@/utils/format'
import type { KbVersion } from '@/types/contract'

/**
 * 版本时间轴 —— FE-07（P8 版本与生效时间）。
 *
 * 任务书 DoD：「版本历史完整」「回滚后 5 分钟内生效」「差异对比新增/删除/修改三色标注」
 *
 * 设计决策：
 *   1. **倒序排列，当前版本在最上** —— 运营关心的是「最近改了什么」，
 *      而不是从三年前的初始版本往下读。
 *   2. **当前版本不可回滚** —— 回滚到当前版本是空操作，
 *      给它一个禁用按钮比隐藏按钮更清楚（运营会知道「这里本来就该有个按钮」）。
 *   3. **回滚必须二次确认且说明生效延迟** —— 「回滚后 5 分钟内生效」是任务书的明确口径，
 *      必须在确认框里写出来，否则运营会以为点了立刻生效，发现没变就去重复点。
 *   4. **生效区间与审核状态同屏** —— 回滚会把审核状态重置为待审核，
 *      这是影响「能不能被坐席检索到」的关键后果，必须提前告知。
 */
interface Props {
  versions: KbVersion[]
  currentVersion: number
  /** 回滚中 */
  rolling?: boolean
}

const props = withDefaults(defineProps<Props>(), { rolling: false })

const emit = defineEmits<{
  (e: 'compare', oldVersion: number, newVersion: number): void
  (e: 'rollback', version: number): void
}>()

/** 倒序：最新版本在最上 */
const ordered = computed(() => [...props.versions].sort((a, b) => b.version - a.version))

/** 相邻版本配对，用于「与上一版对比」 */
const pairs = computed(() => {
  const list = ordered.value
  return list.map((version, index) => ({
    version,
    previous: list[index + 1] ?? null,
    isCurrent: version.version === props.currentVersion,
  }))
})

function effectiveText(version: KbVersion): string {
  const from = version.effective_from ?? '立即生效'
  const to = version.effective_to ?? '长期有效'
  return `${from} ~ ${to}`
}
</script>

<template>
  <ol class="timeline">
    <li v-for="item in pairs" :key="item.version.version" class="timeline__item" :class="{ 'is-current': item.isCurrent }">
      <span class="timeline__dot" />

      <div class="timeline__body">
        <header class="timeline__head">
          <span class="timeline__version wk-mono">v{{ item.version.version }}</span>
          <span v-if="item.isCurrent" class="wk-pill wk-pill--brand">当前版本</span>
          <EnumTag kind="review-status" :value="item.version.review_status" />
          <span class="wk-spacer" />
          <span class="timeline__time wk-mono">{{ formatDateTime(item.version.updated_at) }}</span>
        </header>

        <p class="timeline__note">{{ item.version.change_note }}</p>

        <div class="timeline__meta">
          <span>修改人：{{ item.version.updated_by }}</span>
          <span>生效区间：<span class="wk-mono">{{ effectiveText(item.version) }}</span></span>
        </div>

        <div class="timeline__actions">
          <el-button
            v-if="item.previous"
            link
            type="primary"
            size="small"
            @click="emit('compare', item.previous.version, item.version.version)"
          >
            与 v{{ item.previous.version }} 对比
          </el-button>
          <span v-else class="timeline__initial">初始版本，无可对比的上一版</span>

          <el-button
            v-if="!item.isCurrent"
            link
            type="warning"
            size="small"
            :disabled="props.rolling"
            @click="emit('rollback', item.version.version)"
          >
            回滚到 v{{ item.version.version }}
          </el-button>
          <span v-else class="timeline__current-hint">当前版本无需回滚</span>
        </div>
      </div>
    </li>
  </ol>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.timeline {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;

  &__item {
    position: relative;
    display: flex;
    gap: $space-3;
    padding-bottom: $space-4;

    &:not(:last-child)::before {
      content: '';
      position: absolute;
      left: 5px;
      top: 14px;
      bottom: 0;
      width: 1px;
      background: $ink-200;
    }

    &.is-current .timeline__dot {
      background: $brand-500;
      box-shadow: 0 0 0 3px $brand-50;
    }
  }

  &__dot {
    flex: none;
    width: 11px;
    height: 11px;
    margin-top: 4px;
    border-radius: 50%;
    background: $ink-300;
    z-index: 1;
  }

  &__body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  &__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: $space-2;
  }

  &__version {
    font-size: $text-sm;
    font-weight: 700;
    color: $ink-900;
  }

  &__time {
    font-size: 11px;
    color: $ink-500;
  }

  &__note {
    margin: 0;
    font-size: $text-sm;
    color: $ink-800;
  }

  &__meta {
    display: flex;
    flex-wrap: wrap;
    gap: $space-3;
    font-size: 11px;
    color: $ink-500;
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: $space-3;
    margin-top: 2px;
  }

  &__initial,
  &__current-hint {
    font-size: 11px;
    color: $ink-400;
  }
}
</style>
