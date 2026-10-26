<script setup lang="ts">
import { computed } from 'vue'
import { diffLines, summarizeDiff } from '@/utils/diff'
import EnumTag from '@/components/common/EnumTag.vue'
import type { KbVersion } from '@/types/contract'

/**
 * 差异对比 —— FE-07。
 *
 * 任务书 DoD：「差异对比新增/删除/修改三色标注」
 *
 * 设计决策：
 *   1. **左右并排而非统一 diff** —— 知识条目是给运营改的，
 *      左右对照能直接看到「原来怎么写、现在怎么写」，
 *      而统一 diff 的 +/- 交错在长句修改时反而难读。
 *   2. **相同行默认折叠** —— 版本差异通常只有几行，
 *      500 行里显示 3 行变化才是重点，其余上下文按需展开。
 *   3. **三色与图例同屏** —— 绿=新增、红=删除、黄=修改，
 *      并给出 +N / -N / ~N 统计，让运营在展开前就知道改动量。
 */
interface Props {
  oldVersion: KbVersion
  newVersion: KbVersion
}

const props = defineProps<Props>()

const lines = computed(() => diffLines(props.oldVersion.content, props.newVersion.content))

const summary = computed(() => summarizeDiff(lines.value))

const hasChange = computed(
  () => summary.value.added > 0 || summary.value.removed > 0 || summary.value.modified > 0,
)

/** 相同行折叠：只保留变更行 + 每侧上下各 2 行上下文 */
const visibleLines = computed(() => {
  const all = lines.value
  const keep = new Set<number>()
  all.forEach((line, index) => {
    if (line.kind === 'equal') return
    for (let offset = -2; offset <= 2; offset += 1) {
      const target = index + offset
      if (target >= 0 && target < all.length) keep.add(target)
    }
  })
  return all
    .map((line, index) => ({ line, index, visible: keep.has(index) }))
    .filter((entry) => entry.visible || entry.line.kind !== 'equal')
})

/** 折叠了多少行相同内容 */
const collapsedCount = computed(
  () => lines.value.filter((line) => line.kind === 'equal').length,
)
</script>

<template>
  <div class="diff">
    <header class="diff__head">
      <div class="diff__versions">
        <span class="diff__version">
          v{{ props.oldVersion.version }}
          <EnumTag kind="review-status" :value="props.oldVersion.review_status" />
        </span>
        <span class="diff__arrow">→</span>
        <span class="diff__version">
          v{{ props.newVersion.version }}
          <EnumTag kind="review-status" :value="props.newVersion.review_status" />
        </span>
      </div>
      <div class="diff__stats">
        <span class="diff__stat diff__stat--add">+{{ summary.added }}</span>
        <span class="diff__stat diff__stat--remove">-{{ summary.removed }}</span>
        <span class="diff__stat diff__stat--modify">~{{ summary.modified }}</span>
      </div>
    </header>

    <div class="diff__legend">
      <span class="diff__legend-item diff__legend-item--add">新增</span>
      <span class="diff__legend-item diff__legend-item--remove">删除</span>
      <span class="diff__legend-item diff__legend-item--modify">修改</span>
      <span v-if="collapsedCount" class="diff__collapsed">已折叠 {{ collapsedCount }} 行相同内容</span>
    </div>

    <p v-if="!hasChange" class="diff__empty">两个版本的正文内容完全一致（可能只修改了元数据或生效时间）</p>

    <div v-else class="diff__body">
      <!-- 旧版本 -->
      <section class="diff__pane">
        <h4 class="diff__pane-title">v{{ props.oldVersion.version }} · {{ props.oldVersion.updated_by }}</h4>
        <ol class="diff__lines">
          <li
            v-for="entry in visibleLines"
            :key="`old-${entry.index}`"
            class="diff__line"
            :class="{
              'is-remove': entry.line.kind === 'remove',
              'is-modify': entry.line.kind === 'modify',
              'is-empty': entry.line.kind === 'add',
            }"
          >
            <span class="diff__ln wk-mono">{{ entry.line.oldLine ?? '' }}</span>
            <pre class="diff__text">{{ entry.line.kind === 'add' ? '' : entry.line.oldText }}</pre>
          </li>
        </ol>
      </section>

      <!-- 新版本 -->
      <section class="diff__pane">
        <h4 class="diff__pane-title">v{{ props.newVersion.version }} · {{ props.newVersion.updated_by }}</h4>
        <ol class="diff__lines">
          <li
            v-for="entry in visibleLines"
            :key="`new-${entry.index}`"
            class="diff__line"
            :class="{
              'is-add': entry.line.kind === 'add',
              'is-modify': entry.line.kind === 'modify',
              'is-empty': entry.line.kind === 'remove',
            }"
          >
            <span class="diff__ln wk-mono">{{ entry.line.newLine ?? '' }}</span>
            <pre class="diff__text">{{ entry.line.kind === 'remove' ? '' : entry.line.newText }}</pre>
          </li>
        </ol>
      </section>
    </div>

    <footer class="diff__foot">
      <div class="diff__note">
        <span class="diff__note-label">变更说明</span>
        <span>{{ props.newVersion.change_note }}</span>
      </div>
    </footer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.diff {
  display: flex;
  flex-direction: column;
  gap: $space-2;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: $space-3;
  }

  &__versions {
    display: flex;
    align-items: center;
    gap: $space-2;
  }

  &__version {
    display: inline-flex;
    align-items: center;
    gap: $space-1;
    font-size: $text-xs;
    font-weight: 600;
    color: $ink-800;
  }

  &__arrow {
    color: $ink-400;
  }

  &__stats {
    display: flex;
    gap: $space-2;
    font-family: $font-mono;
    font-size: $text-xs;
  }

  &__stat {
    padding: 0 6px;
    border-radius: $radius-xs;

    &--add {
      background: $success-bg;
      color: $success-ink;
    }

    &--remove {
      background: $danger-bg;
      color: $danger-ink;
    }

    &--modify {
      background: $warning-bg;
      color: $warning-ink;
    }
  }

  &__legend {
    display: flex;
    align-items: center;
    gap: $space-2;
    font-size: 11px;
  }

  &__legend-item {
    padding: 0 6px;
    border-radius: $radius-xs;

    &--add {
      background: $success-bg;
      color: $success-ink;
    }

    &--remove {
      background: $danger-bg;
      color: $danger-ink;
    }

    &--modify {
      background: $warning-bg;
      color: $warning-ink;
    }
  }

  &__collapsed {
    margin-left: auto;
    color: $ink-400;
  }

  &__empty {
    margin: 0;
    padding: $space-4;
    text-align: center;
    font-size: $text-xs;
    color: $ink-400;
    background: $surface-sunken;
    border-radius: $radius-sm;
  }

  &__body {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: $space-2;

    @media (max-width: 1100px) {
      grid-template-columns: 1fr;
    }
  }

  &__pane {
    min-width: 0;
    border: 1px solid $ink-150;
    border-radius: $radius-sm;
    overflow: hidden;
  }

  &__pane-title {
    margin: 0;
    padding: $space-1 $space-2;
    background: $surface-sunken;
    border-bottom: 1px solid $ink-150;
    font-size: 11px;
    font-weight: 600;
    color: $ink-600;
  }

  &__lines {
    margin: 0;
    padding: 0;
    list-style: none;
    max-height: 460px;
    overflow-y: auto;
  }

  &__line {
    display: flex;
    align-items: flex-start;
    gap: $space-2;
    padding: 1px $space-2;

    &.is-add {
      background: $success-bg;
    }

    &.is-remove {
      background: $danger-bg;
    }

    &.is-modify {
      background: $warning-bg;
    }

    &.is-empty {
      background: $surface-sunken;

      .diff__text {
        color: transparent;
      }
    }
  }

  &__ln {
    flex: none;
    width: 30px;
    text-align: right;
    font-size: 10px;
    color: $ink-400;
    user-select: none;
  }

  &__text {
    margin: 0;
    flex: 1;
    min-width: 0;
    font-family: $font-sans;
    font-size: 11px;
    line-height: 1.7;
    color: $ink-800;
    white-space: pre-wrap;
    word-break: break-word;
  }

  &__foot {
    padding-top: $space-2;
    border-top: 1px dashed $ink-150;
  }

  &__note {
    display: flex;
    align-items: baseline;
    gap: $space-2;
    font-size: $text-xs;
    color: $ink-700;
  }

  &__note-label {
    flex: none;
    color: $ink-500;
  }
}
</style>
