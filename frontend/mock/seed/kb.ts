/**
 * 知识库 Mock —— FE-05 / FE-06 / FE-07 的数据源。
 *
 * 基于商家真实材料：
 *   - 74 张商品知识卡（商品主数据表 + 核心卖点 + 知识文本）
 *   - 126 条 FAQ（标准问 + 相似问 + 答案 + 类目 + 权威来源）
 * 切分长度区间严格按任务书 FE-06：商品 200–400 / 政策 100–300 / FAQ 50–200 / 长文档 300–500。
 */
import type { Channel, KbChunk, KbType, ReviewStatus } from '../../src/types/contract'
import type { SeededRandom } from './random'
import { faqs, products } from './data-loader'

export interface MockChunk extends KbChunk {
  /** 该条目的历史版本（FE-07） */
  history: MockVersion[]
}

export interface MockVersion {
  version: number
  chunk_id: string
  title: string
  content: string
  review_status: ReviewStatus
  effective_from: string | null
  effective_to: string | null
  updated_by: string
  updated_at: string
  change_note: string
}

const CATEGORY_CHANNEL: Channel | 'ALL' = 'ALL'

/** 商品类目 -> 知识库 category */
function productCategory(kbCategory: string): string {
  return `商品知识·${kbCategory}`
}

/**
 * 构建知识库条目种子。
 * 注意：这里刻意不使用随机数 —— 知识条目是运营资产，内容与版本必须可复现，
 * 随机只用在「哪些条目处于哪个审核态」这类由 index 决定的位置上。
 * 参数保留 `_random` 是为了与其它 build* 种子保持统一签名。
 */
export function buildKbChunks(_random: SeededRandom): MockChunk[] {
  const chunks: MockChunk[] = []

  // ── 1. 商品知识卡（74 张）→ 一物一单元，200–400 字 ──
  products().forEach((product, index) => {
    const content = product.knowledge_text || buildProductText(product)
    const chunkId = `KB-PROD-${String(index + 1).padStart(3, '0')}`
    const updatedAt = product.updated_at
      ? `${product.updated_at}T09:00:00.000Z`
      : new Date(Date.now() - index * 3_600_000).toISOString()

    chunks.push(
      makeChunk({
        chunk_id: chunkId,
        title: `${product.name}（${product.sku_id}）商品知识`,
        content,
        kb_type: 'PRODUCT',
        category: productCategory(product.category),
        sku_ids: [product.sku_id],
        authority: '商品主数据表·知识文本字段',
        review_status: index % 13 === 7 ? 'PENDING' : 'APPROVED',
        reviewer: '客服主管·李敏',
        channel: CATEGORY_CHANNEL,
        version: 1,
        created_by: '知识库运营·张涛',
        updated_by: '知识库运营·张涛',
        created_at: updatedAt,
        updated_at: updatedAt,
        history: [],
      }),
    )
  })

  // ── 2. FAQ（126 条）→ 50–200 字 ──
  faqs().forEach((faq, index) => {
    const chunkId = `KB-FAQ-${String(index + 1).padStart(3, '0')}`
    const content = `${faq.standard_q}\n\n${faq.answer}`
    const similar = faq.similar_q.length > 0 ? `\n\n相似问：${faq.similar_q.join('；')}` : ''
    const updatedAt = new Date(Date.now() - (index + 100) * 3_600_000).toISOString()

    chunks.push(
      makeChunk({
        chunk_id: chunkId,
        title: faq.standard_q,
        content: `${content}${similar}`,
        kb_type: 'FAQ',
        category: faq.category,
        sku_ids: [],
        authority: faq.authority,
        review_status: 'APPROVED',
        reviewer: faq.reviewer || '客服主管·李敏',
        channel: CATEGORY_CHANNEL,
        version: 1,
        created_by: '知识库运营·张涛',
        updated_by: '知识库运营·张涛',
        created_at: updatedAt,
        updated_at: updatedAt,
        history: [],
      }),
    )
  })

  // ── 3. 政策与 SOP（从商家 docs 提炼的代表性条目）→ 100–300 字 ──
  const policySeeds: Array<{
    title: string
    content: string
    category: string
    authority: string
  }> = [
    {
      title: '七天无理由退货政策',
      content:
        '自签收次日起 7 个自然日内，商品不影响二次销售的前提下支持无理由退货。退回运费由买家承担；若为商品质量问题（含破损、错发、漏发），退换运费由商家承担。定制款、盲盒已拆封、贴身用品不支持无理由退货。申请入口：我的-退款/售后。',
      category: '退换货政策',
      authority: '售后政策 3.1 条',
    },
    {
      title: '质量问题运费承担口径',
      content:
        '经核实为商品质量问题（破损、污渍、开线、填充物外漏等），退换货运费全部由商家承担。买家需提供 2–3 张清晰瑕疵照片，客服确认后即可发起退换，无需寄回前先垫付运费，可走「上门取件」。',
      category: '退换货政策',
      authority: '售后政策 4.2 条',
    },
    {
      title: '发货时效与物流时效说明',
      content:
        '现货商品在付款后 48 小时内发出（大促期间延长至 72 小时）。默认合作快递为中通、圆通、顺丰，不支持指定快递。发货后江浙沪皖 1–2 天、其他地区 2–4 天送达，偏远地区 4–7 天。物流停滞超过 3 天可由客服发起催件。',
      category: '物流时效',
      authority: '物流发货SOP 2.1 条',
    },
    {
      title: '大促优惠券叠加规则',
      content:
        '店铺满减券与平台跨店满减可叠加使用，单笔订单最多叠加 2 张店铺券。会员专享券不可与新人首单券叠加。优惠券不找零、不兑现、逾期作废。具体以活动页面「优惠说明」为准。',
      category: '价格优惠',
      authority: '营销活动规则 1.2 条',
    },
    {
      title: '品牌授权与正品保障说明',
      content:
        '玩可爱所有在售商品均为品牌直营或官方授权渠道，可提供加盖公章的授权说明与质检报告。IP 联名款均持有对应 IP 方的正式授权链路文件，支持平台维权与线下查询。',
      category: '品牌资质',
      authority: '品牌与IP资料 2.1 条',
    },
  ]

  policySeeds.forEach((seed, index) => {
    const chunkId = `KB-POLICY-${String(index + 1).padStart(3, '0')}`
    const updatedAt = new Date(Date.now() - (index + 300) * 3_600_000).toISOString()
    chunks.push(
      makeChunk({
        chunk_id: chunkId,
        title: seed.title,
        content: seed.content,
        kb_type: 'POLICY',
        category: seed.category,
        sku_ids: [],
        authority: seed.authority,
        review_status: 'APPROVED',
        reviewer: '客服主管·李敏',
        channel: CATEGORY_CHANNEL,
        version: 1,
        created_by: '知识库运营·张涛',
        updated_by: '知识库运营·张涛',
        created_at: updatedAt,
        updated_at: updatedAt,
        history: [],
      }),
    )
  })

  // ── 4. 长文档（切分示例：300–500 字，用于 FE-06 预览演示）──
  const longDocSeeds = [
    {
      title: '毛绒玩偶洗护保养完全指南（第 1 段：面料识别）',
      content:
        '玩可爱的毛绒玩偶按面料分为三大类，洗护方式差异明显。第一类为水晶超柔、仿兔毛、牛奶绒面料，手感细腻、绒毛较长，填充物为 PP 棉，这类款式（如奶油小熊、云朵兔子）支持机洗，需装入洗衣袋后选 30℃轻柔模式，不可漂白，低温烘干即可。第二类为短毛绒、羊羔绒面料，绒毛短密、回弹性好，这类款式（如抹茶恐龙、柴犬豆豆）建议手洗，用 30℃以下温水加中性洗涤剂轻揉，不可机洗、不可漂白、避免阳光暴晒，否则会导致面料发硬掉色。第三类为搪胶潮玩与盲盒手办，主体为 PVC/ABS 材质，仅需用湿布擦拭表面灰尘，严禁浸泡、机洗或使用酒精类溶剂，以免漆面脱落。无论哪一类，洗护前都请先查看缝在玩偶内侧的洗标，以洗标为准。',
      category: '洗护保养',
      authority: '商品主数据表·洗护方式字段汇总',
    },
    {
      title: '毛绒玩偶洗护保养完全指南（第 2 段：晾干与复形）',
      content:
        '清洗完成后的晾干与复形环节直接决定玩偶能否恢复蓬松。第一步，用干毛巾包裹玩偶轻压吸走多余水分，切忌用力拧绞，拧绞会让填充棉结块并拉扯缝线。第二步，将玩偶平铺在阴凉通风处自然晾干，期间翻面一次确保内部干透，避免阳光直射与暖气直烘，高温会让绒毛卷曲发硬。第三步，完全干透后用手掌反复轻拍并揉捏填充部位，让 PP 棉重新蓬松；若绒毛有打结，可用宽齿梳顺毛方向轻轻梳开。第四步，若玩偶带有刺绣眼睛或塑料配件，晾干时避开高温区域，防止胶水失效脱落。整套流程通常需要 24–48 小时完全干透，请勿在未干透时收纳，否则容易滋生霉味。',
      category: '洗护保养',
      authority: '商品主数据表·洗护方式字段汇总',
    },
  ]

  longDocSeeds.forEach((seed, index) => {
    const chunkId = `KB-DOC-${String(index + 1).padStart(3, '0')}`
    const updatedAt = new Date(Date.now() - (index + 400) * 3_600_000).toISOString()
    chunks.push(
      makeChunk({
        chunk_id: chunkId,
        title: seed.title,
        content: seed.content,
        kb_type: 'LONG_DOC',
        category: seed.category,
        sku_ids: [],
        authority: seed.authority,
        review_status: index === 1 ? 'PENDING' : 'APPROVED',
        reviewer: '客服主管·李敏',
        channel: CATEGORY_CHANNEL,
        version: 1,
        created_by: '知识库运营·张涛',
        updated_by: '知识库运营·张涛',
        created_at: updatedAt,
        updated_at: updatedAt,
        history: [],
      }),
    )
  })

  // ── 为部分条目补充历史版本（FE-07 版本时间线）──
  chunks.forEach((chunk, index) => {
    if (index % 9 !== 3) return
    chunk.version = 3
    chunk.history = [
      makeVersion(chunk, 1, '初始创建：从商家材料包导入', -60),
      makeVersion(chunk, 2, '修正洗护口径：补充「以洗标为准」', -30),
      makeVersion(chunk, 3, '根据纠错反馈更新：明确机洗温度与烘干条件', -5),
    ]
  })

  return chunks
}

/**
 * 补齐 KbChunk 的派生字段（char_count）与可选项默认值。
 * effective_from / effective_to / review_comment 允许调用方省略，
 * 避免每处构造都重复写 null。
 */
function makeChunk(
  input: Omit<MockChunk, 'char_count' | 'history' | 'effective_from' | 'effective_to' | 'review_comment'> &
    Partial<Pick<MockChunk, 'effective_from' | 'effective_to' | 'review_comment'>> & {
      history: MockVersion[]
    },
): MockChunk {
  return {
    ...input,
    effective_from: input.effective_from ?? input.created_at,
    effective_to: input.effective_to ?? null,
    review_comment: input.review_comment ?? null,
    char_count: input.content.replace(/\s/g, '').length,
  }
}

function makeVersion(chunk: MockChunk, version: number, note: string, daysAgo: number): MockVersion {
  const at = new Date(Date.now() + daysAgo * 86_400_000).toISOString()
  return {
    version,
    chunk_id: chunk.chunk_id,
    title: chunk.title,
    // 历史版本内容做可见差异，便于 DiffViewer 演示三色标注
    content:
      version === chunk.version
        ? chunk.content
        : version === 1
          ? chunk.content.slice(0, Math.floor(chunk.content.length * 0.72))
          : chunk.content.replace('以洗标为准', '以商品页面说明为准'),
    review_status: version === chunk.version ? chunk.review_status : 'APPROVED',
    effective_from: at,
    effective_to: version === chunk.version ? null : new Date(Date.now() + (daysAgo + 30) * 86_400_000).toISOString(),
    updated_by: '知识库运营·张涛',
    updated_at: at,
    change_note: note,
  }
}

function buildProductText(product: ReturnType<typeof products>[number]): string {
  return `${product.name}（SKU：${product.sku_id}）属于${product.category}，尺寸 ${product.size}，面料为${product.material}，填充物为${product.filling}，适用年龄${product.age_range}，洗护方式为${product.care}，包装形式为${product.packaging}，上市时间 ${product.launch_date}，是否 IP 联名：${product.is_ip}。吊牌价 ${product.list_price} 元，当前售价 ${product.price} 元，库存状态为${product.stock_status}。${product.selling_points}`
}

/** 按知识类型推断切分单元（FE-06 预览用） */
export function splitIntoChunks(
  content: string,
  kbType: KbType,
  overlapRatio: number,
): Array<{ index: number; text: string; charCount: number }> {
  const range = CHUNK_RANGES[kbType]
  const target = Math.floor((range.min + range.max) / 2)
  const overlap = Math.floor(target * overlapRatio)
  const clean = content.replace(/\r\n/g, '\n').trim()

  // 优先按自然段聚合，避免把句子切碎
  const paragraphs = clean.split(/\n{2,}/).filter(Boolean)
  const units: Array<{ index: number; text: string; charCount: number }> = []
  let buffer = ''
  let index = 0

  const flush = (): void => {
    if (!buffer.trim()) return
    index += 1
    units.push({
      index,
      text: buffer.trim(),
      charCount: buffer.replace(/\s/g, '').length,
    })
    // 保留重叠部分
    buffer = overlap > 0 ? buffer.slice(-overlap) : ''
  }

  for (const paragraph of paragraphs) {
    if (buffer.length + paragraph.length + 2 > range.max && buffer.length >= range.min) {
      flush()
    }
    buffer += (buffer ? '\n\n' : '') + paragraph

    // 单段就超长：按句子硬切
    while (buffer.replace(/\s/g, '').length > range.max) {
      const cutAt = findSentenceBoundary(buffer, target)
      index += 1
      units.push({
        index,
        text: buffer.slice(0, cutAt).trim(),
        charCount: buffer.slice(0, cutAt).replace(/\s/g, '').length,
      })
      buffer = buffer.slice(Math.max(0, cutAt - overlap))
    }
  }
  flush()

  return units
}

function findSentenceBoundary(text: string, target: number): number {
  const window = text.slice(target, Math.min(text.length, target + 80))
  const match = window.match(/[。！？；\n]/)
  if (match && match.index !== undefined) return target + match.index + 1
  return Math.min(text.length, target)
}

export const CHUNK_RANGES: Record<KbType, { min: number; max: number }> = {
  PRODUCT: { min: 200, max: 400 },
  POLICY: { min: 100, max: 300 },
  FAQ: { min: 50, max: 200 },
  LONG_DOC: { min: 300, max: 500 },
}

export const CHUNK_OVERLAP = 0.15
