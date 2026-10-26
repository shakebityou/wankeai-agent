/**
 * 确定性伪随机 —— Mock 数据必须「每次刷新都一样」。
 *
 * 为什么不用 Math.random：
 *   1. 坐席台刷新后会话顺序、指标数值乱跳，无法做视觉走查与截图对比；
 *   2. Playwright E2E 需要稳定的断言目标；
 *   3. 演示时前后两次说法不一致会直接失去可信度。
 * 因此用 xorshift32 + 固定种子，序列完全可复现。
 */
export class SeededRandom {
  private state: number

  constructor(seed: number) {
    this.state = seed >>> 0 || 0x9e3779b9
  }

  /** [0,1) */
  next(): number {
    let x = this.state
    x ^= x << 13
    x >>>= 0
    x ^= x >>> 17
    x ^= x << 5
    x >>>= 0
    this.state = x
    return x / 0x100000000
  }

  /** [min,max] 整数 */
  int(min: number, max: number): number {
    return Math.floor(this.next() * (max - min + 1)) + min
  }

  /** 数组随机取一项 */
  pick<T>(items: readonly T[]): T {
    return items[Math.floor(this.next() * items.length)] as T
  }

  /** 加权取一项：weights 与 items 等长 */
  weighted<T>(items: readonly T[], weights: readonly number[]): T {
    const total = weights.reduce((sum, w) => sum + w, 0)
    let roll = this.next() * total
    for (let i = 0; i < items.length; i += 1) {
      roll -= weights[i] ?? 0
      if (roll <= 0) return items[i] as T
    }
    return items[items.length - 1] as T
  }

  bool(trueProbability = 0.5): boolean {
    return this.next() < trueProbability
  }

  /** 从数组中不重复取 n 项 */
  sample<T>(items: readonly T[], n: number): T[] {
    const pool = [...items]
    const out: T[] = []
    const count = Math.min(n, pool.length)
    for (let i = 0; i < count; i += 1) {
      const idx = Math.floor(this.next() * pool.length)
      out.push(pool.splice(idx, 1)[0] as T)
    }
    return out
  }
}
