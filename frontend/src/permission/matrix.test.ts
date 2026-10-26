import { describe, expect, it } from 'vitest'
import { canAccess, canDo, isReadOnlyConfig } from '@/permission/matrix'
import type { UserRole } from '@/permission/matrix'

/**
 * 单测：权限矩阵 —— 任务书 4.5.2「角色权限矩阵」。
 *
 * 四角色（坐席 / 客服主管 / 知识库运营 / 系统管理员）的边界是安全边界，
 * 不是体验细节：坐席若能写知识库，意味着未经审核的内容可能被直接改进向量库。
 *
 * 注意：本文件的角色字面量与 `matrix.ts` 内部定义一致
 * （'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN'）。
 */

const ROLES: UserRole[] = ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN']

describe('canAccess（路由级权限）', () => {
  it('坐席能进会话列表与工单列表', () => {
    expect(canAccess('AGENT', 'console.sessions')).toBe(true)
    expect(canAccess('AGENT', 'console.tickets')).toBe(true)
  })

  it('坐席不能进配置类页面', () => {
    expect(canAccess('AGENT', 'admin.config')).toBe(false)
  })

  it('知识库运营不能进配置页面', () => {
    expect(canAccess('KB_OPERATOR', 'admin.config')).toBe(false)
  })

  it('客服主管能进配置页（只读）', () => {
    expect(canAccess('SUPERVISOR', 'admin.config')).toBe(true)
  })

  it('系统管理员能进全部路由权限', () => {
    const permissions = [
      'console.sessions',
      'console.tickets',
      'admin.kb',
      'admin.kb.write',
      'admin.feedback',
      'admin.metrics',
      'admin.config',
    ] as const
    for (const permission of permissions) {
      expect(canAccess('ADMIN', permission)).toBe(true)
    }
  })

  it('纠错待办与指标看板对所有角色开放（各自范围不同）', () => {
    for (const role of ROLES) {
      expect(canAccess(role, 'admin.feedback')).toBe(true)
      expect(canAccess(role, 'admin.metrics')).toBe(true)
    }
  })
})

describe('canDo（操作级权限）', () => {
  it('接管会话：坐席/主管/管理员可以，知识库运营不行', () => {
    expect(canDo('AGENT', 'takeover.execute')).toBe(true)
    expect(canDo('SUPERVISOR', 'takeover.execute')).toBe(true)
    expect(canDo('ADMIN', 'takeover.execute')).toBe(true)
    expect(canDo('KB_OPERATOR', 'takeover.execute')).toBe(false)
  })

  it('知识写入：仅知识库运营与系统管理员', () => {
    expect(canDo('KB_OPERATOR', 'kb.write')).toBe(true)
    expect(canDo('ADMIN', 'kb.write')).toBe(true)
    expect(canDo('SUPERVISOR', 'kb.write')).toBe(false)
    expect(canDo('AGENT', 'kb.write')).toBe(false)
  })

  it('知识审核：仅主管与系统管理员（与写入分离，避免自审）', () => {
    expect(canDo('SUPERVISOR', 'kb.review')).toBe(true)
    expect(canDo('ADMIN', 'kb.review')).toBe(true)
    expect(canDo('KB_OPERATOR', 'kb.review')).toBe(false)
  })

  it('纠错指派：仅主管与系统管理员', () => {
    expect(canDo('SUPERVISOR', 'feedback.assign')).toBe(true)
    expect(canDo('ADMIN', 'feedback.assign')).toBe(true)
    expect(canDo('AGENT', 'feedback.assign')).toBe(false)
    expect(canDo('KB_OPERATOR', 'feedback.assign')).toBe(false)
  })

  it('纠错处理：仅知识库运营与系统管理员', () => {
    expect(canDo('KB_OPERATOR', 'feedback.handle')).toBe(true)
    expect(canDo('ADMIN', 'feedback.handle')).toBe(true)
    expect(canDo('SUPERVISOR', 'feedback.handle')).toBe(false)
  })

  it('配置写入：仅系统管理员', () => {
    expect(canDo('ADMIN', 'config.write')).toBe(true)
    expect(canDo('SUPERVISOR', 'config.write')).toBe(false)
    expect(canDo('KB_OPERATOR', 'config.write')).toBe(false)
    expect(canDo('AGENT', 'config.write')).toBe(false)
  })

  it('纠错提交：所有角色都能提交（坐席是一线发现者）', () => {
    for (const role of ROLES) {
      expect(canDo(role, 'feedback.submit')).toBe(true)
    }
  })

  it('全量指标：仅主管与系统管理员', () => {
    expect(canDo('SUPERVISOR', 'metrics.full')).toBe(true)
    expect(canDo('ADMIN', 'metrics.full')).toBe(true)
    expect(canDo('AGENT', 'metrics.full')).toBe(false)
    expect(canDo('KB_OPERATOR', 'metrics.full')).toBe(false)
  })

  it('知识读取：所有角色都可以（坐席靠侧栏检索）', () => {
    for (const role of ROLES) {
      expect(canDo(role, 'kb.read')).toBe(true)
    }
  })
})

describe('isReadOnlyConfig（配置只读判定）', () => {
  it('主管对配置是只读', () => {
    expect(isReadOnlyConfig('SUPERVISOR')).toBe(true)
  })

  it('系统管理员不是只读', () => {
    expect(isReadOnlyConfig('ADMIN')).toBe(false)
  })

  it('坐席与知识库运营同样按只读处理（他们本就不能进配置页）', () => {
    expect(isReadOnlyConfig('AGENT')).toBe(false)
    expect(isReadOnlyConfig('KB_OPERATOR')).toBe(false)
  })
})
