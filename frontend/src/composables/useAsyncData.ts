import { computed, onScopeDispose, ref, shallowRef, type Ref } from 'vue'
import { BffError } from '@/types/bff'

/**
 * 异步数据加载统一封装 —— 任务书 4.5.1「所有异步有 loading，超 1s 显示骨架屏」。
 *
 * 提供三态（loading / error / data）与「骨架屏」判定，
 * 页面不再各写一套 try/finally + loading 变量。
 */

/** 超过该时长仍无响应 -> 展示骨架屏（而非转圈） */
export const SKELETON_THRESHOLD_MS = 1000

export interface AsyncState<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  /** 超过 1s 仍未返回，页面应渲染骨架屏 */
  showSkeleton: Ref<boolean>
  error: Ref<BffError | null>
  /** 首次加载完成（无论成功失败） */
  settled: Ref<boolean>
  isEmpty: Ref<boolean>
  reload: () => Promise<void>
  /** 手动注入数据（如接管后本地更新，避免整页重拉） */
  setData: (next: T | null) => void
}

export interface UseAsyncOptions<T> {
  /** 立即执行 */
  immediate?: boolean
  /** 初始数据 */
  initial?: T | null
  /** 空态判定：默认按数组长度 / null 判断 */
  isEmpty?: (data: T) => boolean
}

export function useAsyncData<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  options: UseAsyncOptions<T> = {},
): AsyncState<T> {
  const data = shallowRef<T | null>(options.initial ?? null)
  const loading = ref(false)
  const showSkeleton = ref(false)
  const error = ref<BffError | null>(null)
  const settled = ref(false)

  let controller: AbortController | null = null
  let skeletonTimer: number | null = null

  const isEmpty = computed(() => {
    if (data.value === null || data.value === undefined) return true
    if (options.isEmpty) return options.isEmpty(data.value)
    if (Array.isArray(data.value)) return data.value.length === 0
    return false
  })

  async function reload(): Promise<void> {
    controller?.abort()
    controller = new AbortController()
    const { signal } = controller

    loading.value = true
    error.value = null
    clearSkeletonTimer()
    // 超过 1s 才切骨架屏，避免快速响应时的闪烁
    skeletonTimer = window.setTimeout(() => {
      if (loading.value) showSkeleton.value = true
    }, SKELETON_THRESHOLD_MS)

    try {
      const result = await loader(signal)
      if (signal.aborted) return
      data.value = result
    } catch (caught) {
      if (signal.aborted) return
      error.value =
        caught instanceof BffError
          ? caught
          : new BffError(40001, caught instanceof Error ? caught.message : '加载失败', '', 0)
      // 加载失败不保留上一次的脏数据，避免坐席误读过期内容
      data.value = null
    } finally {
      if (!signal.aborted) {
        loading.value = false
        showSkeleton.value = false
        settled.value = true
      }
      clearSkeletonTimer()
    }
  }

  function clearSkeletonTimer(): void {
    if (skeletonTimer !== null) {
      window.clearTimeout(skeletonTimer)
      skeletonTimer = null
    }
  }

  function setData(next: T | null): void {
    data.value = next
    settled.value = true
  }

  onScopeDispose(() => {
    controller?.abort()
    clearSkeletonTimer()
  })

  if (options.immediate !== false) {
    void reload()
  }

  return {
    data: data as Ref<T | null>,
    loading,
    showSkeleton,
    error,
    settled,
    isEmpty,
    reload,
    setData,
  }
}

/** 轮询封装 —— 任务书 FE-01「30 秒轮询」与 4.5.3「不阻塞 UI」 */
export function usePolling(task: () => Promise<void>, intervalMs: number): {
  start: () => void
  stop: () => void
  running: Ref<boolean>
} {
  const running = ref(false)
  let timer: number | null = null

  async function tick(): Promise<void> {
    if (!running.value) return
    try {
      await task()
    } catch {
      // 轮询失败静默：不打断坐席，下一轮自动恢复
    }
    if (running.value) {
      timer = window.setTimeout(() => void tick(), intervalMs)
    }
  }

  function start(): void {
    if (running.value) return
    running.value = true
    timer = window.setTimeout(() => void tick(), intervalMs)
  }

  function stop(): void {
    running.value = false
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  onScopeDispose(stop)

  return { start, stop, running }
}
