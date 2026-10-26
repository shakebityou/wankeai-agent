import type {
  CompanyCode,
  LogisticsStatus,
  OrderStatus,
  StockStatus,
} from '@/types/contract'

/**
 * FE-14 视图模型：订单 / 商品 / 物流。
 *
 * 为什么单独定义视图模型而不是直接用接口类型：
 *   任务书 FE-14 明确「复用组件，无独立接口，数据来自 FE-01 / FE-02 响应」。
 *   也就是说这三类卡片的数据是从会话消息与 AI 上下文里**推导**出来的，
 *   并没有一个 GET /bff/v1/orders 接口。既然没有接口契约，
 *   就把它定义成前端内部的视图模型，避免污染 src/types/contract.ts
 *   （contract.ts 只放与 BFF 契约一一对应的类型，4.5.1 要求）。
 *
 * 合规背景（HC-1 / 商家 0.8 节）：
 *   商家不接入平台商品/订单卡片。这些「卡片」只在坐席工作台内部渲染，
 *   发给用户的一律是纯文本（OrderTextCard 的 toPlainText 就是那个转换器）。
 */

/** 订单商品行 */
export interface OrderLineItem {
  /** 商品标题 */
  title: string
  /** 规格（尺寸 / 颜色等） */
  spec?: string
  quantity: number
  /** 单价（元） */
  price?: number
  /** 商品缩略图（同源相对路径，HC-1 禁外链） */
  image_ref?: string | null
}

/** 订单视图模型 */
export interface OrderInfo {
  order_no: string
  status: OrderStatus
  items: OrderLineItem[]
  /** 订单总金额（元） */
  total_amount?: number
  created_at?: string
  /** 快递公司 */
  company?: CompanyCode
  tracking_no?: string
  /** 最新物流描述（原文，不改写） */
  latest_trace?: string
  /** 最新物流时间 */
  latest_trace_at?: string
  /** 预计送达（YYYY-MM-DD） */
  estimated_delivery?: string
}

/** 物流轨迹单条 —— traces[].desc 必须原文展示，不做任何改写（任务书 FE-14） */
export interface LogisticsTrace {
  time: string
  /** 物流公司原文描述，例如「已到达【杭州转运中心】」 */
  desc: string
  status?: LogisticsStatus
  /** 所在城市（若有） */
  location?: string
}

/** 物流视图模型 */
export interface LogisticsInfo {
  company: CompanyCode
  tracking_no: string
  status: LogisticsStatus
  traces: LogisticsTrace[]
  estimated_delivery?: string
  /** 停滞天数，>0 时高亮提示坐席主动跟进 */
  stalled_days?: number
}

/** 商品引用视图模型 */
export interface ProductRef {
  sku_id: string
  title: string
  spec?: string
  material?: string
  stock_status: StockStatus
  /** 预售天数（stock_status=PRESALE 时有值） */
  presale_days?: number
  price?: number
  original_price?: number
  image_ref?: string | null
}

/**
 * 库存状态中文口径 —— FE-14 DoD 要求「不显示英文枚举」。
 *
 * 注意 PRESALE 必须带天数：商家原始数据是「预售(7天) / 预售(15天) / 预售(30天)」，
 * 只显示「预售」会让坐席无法回答「大概多久发」，这是最常见的追问。
 */
export function stockStatusText(product: Pick<ProductRef, 'stock_status' | 'presale_days'>): string {
  switch (product.stock_status) {
    case 'IN_STOCK':
      return '现货'
    case 'PRESALE':
      return product.presale_days ? `预售 ${product.presale_days} 天` : '预售'
    case 'OUT_OF_STOCK':
      return '缺货'
    default:
      return '未知'
  }
}

/** 库存状态 -> 语义色（缺货红、预售橙、现货绿） */
export function stockStatusTone(
  status: StockStatus,
): 'ok' | 'warn' | 'bad' | 'neutral' {
  switch (status) {
    case 'IN_STOCK':
      return 'ok'
    case 'PRESALE':
      return 'warn'
    case 'OUT_OF_STOCK':
      return 'bad'
    default:
      return 'neutral'
  }
}

/** 把商家原始库存串（如「预售(7天)」）解析成枚举 + 天数 */
export function parseStockStatus(raw: string): {
  status: StockStatus
  presale_days?: number
} {
  if (raw.includes('缺货')) return { status: 'OUT_OF_STOCK' }
  if (raw.includes('预售')) {
    const matched = raw.match(/(\d+)\s*天/)
    return { status: 'PRESALE', presale_days: matched ? Number(matched[1]) : undefined }
  }
  if (raw.includes('现货')) return { status: 'IN_STOCK' }
  return { status: 'IN_STOCK' }
}
