import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

/**
 * Mock 数据加载器 —— 运行时从 mock/data/*.json 读取商家真实数据。
 *
 * 为什么用 fs 读而不用 `import x from './data/x.json'`：
 *   1. products.json 90KB、faqs.json 89KB，TS 的 resolveJsonModule 会为它们
 *      推导出极深的字面量类型，拖慢 vue-tsc 与 IDE；
 *   2. 这些数据只被 Node 侧的 Mock 中间件使用，不进入浏览器产物，
 *      不需要被打包器静态分析。
 * 因此这里显式声明形状，运行时读盘。
 */

const here = dirname(fileURLToPath(import.meta.url))
// 数据在 mock/data/，而本文件在 mock/seed/ —— 需要往上一级。
// （写成 join(here, 'data') 会指向不存在的 mock/seed/data/，
//   表现是 Mock 接口 500、前端列表报「Mock 处理异常：ENOENT」。）
const DATA_DIR = join(here, '..', 'data')

function load<T>(file: string): T[] {
  const raw = readFileSync(join(DATA_DIR, file), 'utf-8')
  return JSON.parse(raw) as T[]
}

// ── 商家真实数据的形状（来自 scripts/extract-merchant-data.py）──

export interface MerchantProduct {
  sku_id: string
  name: string
  category: string
  size: string
  material: string
  filling: string
  age_range: string
  care: string
  packaging: string
  launch_date: string
  is_ip: string
  list_price: string
  price: string
  stock_status: string
  selling_points: string
  knowledge_text: string
  updated_at: string
}

export interface MerchantFaq {
  chunk_id: string
  standard_q: string
  similar_q: string[]
  answer: string
  category: string
  authority: string
  review_status: string
  reviewer: string
}

export interface MerchantGroup {
  group_id: string
  group_name: string
  service_scope: string
  agent_count: number
  supervisor: string
  online_window: string
  channels_raw: string
  daily_volume: number
  peak_volume: number
  note: string
}

export interface MerchantHistorySession {
  会话ID: string
  渠道: string
  时间: string
  用户问题: string
  客服回复: string
  意图类目: string
  是否转人工: string
  用户情绪: string
}

export interface MerchantContact {
  编号: string
  角色: string
  姓名: string
  职务: string
  所属部门: string
  联系方式: string
  备用联系方式: string
  职责范围: string
  可对接事项: string
  响应时段: string
  SLA: string
}

let cacheProducts: MerchantProduct[] | null = null
let cacheFaqs: MerchantFaq[] | null = null
let cacheGroups: MerchantGroup[] | null = null
let cacheHistory: MerchantHistorySession[] | null = null
let cacheContacts: MerchantContact[] | null = null

export function products(): MerchantProduct[] {
  if (!cacheProducts) cacheProducts = load<MerchantProduct>('products.json')
  return cacheProducts
}

export function faqs(): MerchantFaq[] {
  if (!cacheFaqs) cacheFaqs = load<MerchantFaq>('faqs.json')
  return cacheFaqs
}

export function groups(): MerchantGroup[] {
  if (!cacheGroups) cacheGroups = load<MerchantGroup>('agent-groups.json')
  return cacheGroups
}

export function historySessions(): MerchantHistorySession[] {
  if (!cacheHistory) cacheHistory = load<MerchantHistorySession>('history-sessions.json')
  return cacheHistory
}

export function contacts(): MerchantContact[] {
  if (!cacheContacts) cacheContacts = load<MerchantContact>('contacts.json')
  return cacheContacts
}
