import { describe, expect, it } from 'vitest'
import {
  maskPhone,
  maskName,
  maskOrderNo,
  maskAddress,
  scrubText,
  formatDateTime,
  formatRelative,
  formatDuration,
  formatLatency,
  formatMetricValue,
} from '@/utils/format'

/**
 * 单测：脱敏与格式化 —— 任务书 4.5.2 合规红线「脱敏展示」。
 *
 * 这四个 mask 函数是 HC-2（数据不出境、最小必要）在前端的唯一落点：
 * 坐席在接管前看到的会话列表、买家信息必须已经是脱敏结果。
 * 一旦某个 mask 被误改（比如把前 3 后 4 改成全显示），
 * 隐私泄露会直接穿透到 UI，且没有任何后端兜底。
 */

describe('maskPhone', () => {
  it('标准手机号脱敏为 138****5678', () => {
    expect(maskPhone('13812345678')).toBe('138****5678')
  })

  it('空值返回空串', () => {
    expect(maskPhone(null)).toBe('')
    expect(maskPhone('')).toBe('')
  })

  it('非手机号原样返回', () => {
    expect(maskPhone('座机 0571-8888888')).toBe('座机 0571-8888888')
  })
})

describe('maskName', () => {
  it('张小明 → 张**', () => {
    expect(maskName('张小明')).toBe('张**')
  })

  it('两字姓名保留姓', () => {
    expect(maskName('李敏')).toBe('李*')
  })

  it('单字姓名原样返回（无信息可掩）', () => {
    expect(maskName('王')).toBe('王')
  })
})

describe('maskOrderNo', () => {
  it('订单号保留前 4 后 4', () => {
    expect(maskOrderNo('2025011500012345')).toBe('2025****2345')
  })

  it('短订单号原样返回', () => {
    expect(maskOrderNo('123')).toBe('123')
  })
})

describe('maskAddress', () => {
  it('保留省市，截断详细地址', () => {
    const masked = maskAddress('浙江省杭州市余杭区文一西路 969 号 3 号楼 502')
    expect(masked).toContain('浙江省')
    expect(masked).not.toContain('502')
  })
})

describe('scrubText', () => {
  it('把文本里的手机号替换为脱敏形式', () => {
    const out = scrubText('买家电话 13812345678，请尽快联系')
    expect(out).toContain('138****5678')
    expect(out).not.toContain('13812345678')
  })

  it('不含敏感信息的文本原样返回', () => {
    expect(scrubText('请问什么时候发货？')).toBe('请问什么时候发货？')
  })
})

describe('formatDateTime', () => {
  it('格式化为 MM-DD HH:mm（紧凑格式，年份由列表列头承担）', () => {
    const out = formatDateTime('2026-01-15T10:30:00Z')
    expect(out).toMatch(/01-15 \d{2}:\d{2}/)
  })

  it('空值返回占位', () => {
    expect(formatDateTime(null)).toBe('—')
  })

  it('非法日期返回占位', () => {
    expect(formatDateTime('not-a-date')).toBe('—')
  })
})

describe('formatRelative', () => {
  it('刚刚', () => {
    expect(formatRelative(new Date(Date.now() - 30 * 1000).toISOString())).toBe('刚刚')
  })

  it('N 分钟前', () => {
    expect(formatRelative(new Date(Date.now() - 5 * 60 * 1000).toISOString())).toBe('5 分钟前')
  })

  it('N 小时前', () => {
    expect(formatRelative(new Date(Date.now() - 3 * 3600 * 1000).toISOString())).toBe('3 小时前')
  })
})

describe('formatDuration', () => {
  it('秒级', () => {
    expect(formatDuration(45)).toBe('45 秒')
  })

  it('分级', () => {
    expect(formatDuration(90)).toBe('1 分 30 秒')
  })
})

describe('formatLatency', () => {
  it('毫秒级保留 0 位小数', () => {
    expect(formatLatency(320)).toBe('320ms')
  })

  it('超过 1 秒转为秒', () => {
    expect(formatLatency(1500)).toBe('1.50s')
  })
})

describe('formatMetricValue', () => {
  it('百分比带单位', () => {
    expect(formatMetricValue(96.1, '%')).toBe('96.1%')
  })

  it('秒带单位', () => {
    expect(formatMetricValue(1.8, 's')).toBe('1.80s')
  })

  it('毫秒带单位', () => {
    expect(formatMetricValue(420, 'ms')).toBe('420ms')
  })
})
