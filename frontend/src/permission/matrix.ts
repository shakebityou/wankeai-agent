/**
 * 角色权限矩阵 —— 严格落地任务书 4.5.4。
 *
 * 四种角色：
 *   AGENT          坐席
 *   SUPERVISOR     客服主管
 *   KB_OPERATOR    知识库运营
 *   ADMIN          系统管理员
 *
 * 矩阵原文（4.5.4）：
 *   P1–P5 坐席工作台：坐席✅本组会话、主管✅全部、运营❌、管理员✅全部
 *   P6–P8 知识库管理：坐席只读检索、主管✅审核、运营✅CRUD、管理员✅
 *   P9   纠错待办：坐席✅提交、主管✅指派、运营✅处理、管理员✅
 *   P10  指标看板：坐席个人指标、主管✅全量、运营知识库指标、管理员✅全量
 *   P11–P13 配置：坐席❌、主管只读、运营❌、管理员✅
 *
 * 前端守卫只做「看得见/看不见」的体验控制；
 * 真正的鉴权以 BFF 返回 10003 为准（4.5.1 拦截器统一提示「无权限查看」）。
 */
export type UserRole = 'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN'

export const ROLE_LABELS: Record<UserRole, string> = {
  AGENT: '坐席',
  SUPERVISOR: '客服主管',
  KB_OPERATOR: '知识库运营',
  ADMIN: '系统管理员',
}

export type RoutePermission =
  | 'console.sessions'
  | 'console.tickets'
  | 'admin.kb'
  | 'admin.kb.write'
  | 'admin.feedback'
  | 'admin.metrics'
  | 'admin.config'
  | 'admin.users'

/** 权限 -> 允许的角色。未列出的角色一律拒绝。 */
const MATRIX: Record<RoutePermission, readonly UserRole[]> = {
  // P1–P5 坐席工作台：坐席（本组）/ 主管（全部）/ 管理员（全部）；运营❌
  'console.sessions': ['AGENT', 'SUPERVISOR', 'ADMIN'],
  'console.tickets': ['AGENT', 'SUPERVISOR', 'ADMIN'],

  // P6–P8 知识库管理：坐席只读（列表可看，写操作另判）/ 主管审核 / 运营 CRUD / 管理员
  'admin.kb': ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],
  // 写权限：坐席只读，因此不在此列
  'admin.kb.write': ['SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],

  // P9 纠错待办：四种角色均可进入，但各自操作范围不同（见下方 ACTION_MATRIX）
  'admin.feedback': ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],

  // P10 指标看板：坐席看个人、主管全量、运营知识库指标、管理员全量
  'admin.metrics': ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],

  // P11–P13 配置：坐席❌、主管只读、运营❌、管理员✅
  'admin.config': ['SUPERVISOR', 'ADMIN'],

  // 账号管理：只有系统管理员。账号是权限的源头，
  // 让「能改权限的人」比「拥有权限的人」多一层，否则自我提权只需一步。
  'admin.users': ['ADMIN'],
}

export function canAccess(role: UserRole, permission: RoutePermission): boolean {
  return MATRIX[permission].includes(role)
}

// ─────────────────────────────────────────────────────────────
// 细粒度动作权限：同一页面内不同角色的可操作项不同
// ─────────────────────────────────────────────────────────────

export type ActionPermission =
  | 'kb.read'
  | 'kb.review'
  | 'kb.write'
  | 'feedback.submit'
  | 'feedback.assign'
  | 'feedback.handle'
  | 'metrics.personal'
  | 'metrics.full'
  | 'config.read'
  | 'config.write'
  | 'takeover.execute'
  | 'user.read'
  | 'user.manage'

const ACTION_MATRIX: Record<ActionPermission, readonly UserRole[]> = {
  'kb.read': ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],
  'kb.review': ['SUPERVISOR', 'ADMIN'],
  'kb.write': ['KB_OPERATOR', 'ADMIN'],
  'feedback.submit': ['AGENT', 'SUPERVISOR', 'KB_OPERATOR', 'ADMIN'],
  'feedback.assign': ['SUPERVISOR', 'ADMIN'],
  'feedback.handle': ['KB_OPERATOR', 'ADMIN'],
  'metrics.personal': ['AGENT'],
  'metrics.full': ['SUPERVISOR', 'ADMIN'],
  'config.read': ['SUPERVISOR', 'ADMIN'],
  'config.write': ['ADMIN'],
  'takeover.execute': ['AGENT', 'SUPERVISOR', 'ADMIN'],
  'user.read': ['ADMIN'],
  'user.manage': ['ADMIN'],
}

export function canDo(role: UserRole, action: ActionPermission): boolean {
  return ACTION_MATRIX[action].includes(role)
}

/** 只读角色：进入配置页时所有控件置灰。 */
export function isReadOnlyConfig(role: UserRole): boolean {
  return role === 'SUPERVISOR'
}
