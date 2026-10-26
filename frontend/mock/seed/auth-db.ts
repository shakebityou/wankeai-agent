/**
 * 账号种子数据 —— 首次启动自动写入，已有数据则跳过。
 *
 * 密码口径（演示环境）：统一 `Wk@2026demo`。
 * 真实上线时这里只保留超管初始密码 + must_change=1，强迫首登改密。
 *
 * 为什么账号里带「分组」：
 *   4.5.4 权限矩阵里坐席只能看「本组会话」。分组不是装饰字段，
 *   它直接决定 /bff/v1/sessions 返回哪些数据，所以账号表必须带上。
 */
import { getDb } from '../db/index.ts'
import { hashPassword } from '../db/password.ts'
import type { GroupRow, UserRow } from '../db/index.ts'

/** 演示统一口令。生产环境必须改成随机初始密码 + 首登强制修改。 */
export const DEMO_PASSWORD = 'Wk@2026demo'

interface SeedUser {
  user_id: string
  username: string
  display_name: string
  role_code: 'AGENT' | 'SUPERVISOR' | 'KB_OPERATOR' | 'ADMIN'
  group_id: string | null
}

export const SEED_GROUPS: GroupRow[] = [
  { group_id: 'G1', group_name: '售前咨询组', leader: '李敏', headcount: 12, channels: 'TAOBAO,TMALL,PDD,DOUYIN,WECHAT' },
  { group_id: 'G2', group_name: '订单物流组', leader: '李敏', headcount: 8, channels: 'TAOBAO,TMALL,PDD,DOUYIN' },
  { group_id: 'G3', group_name: '售后处理组', leader: '李敏', headcount: 10, channels: 'TAOBAO,TMALL,PDD,DOUYIN,WECHAT' },
  { group_id: 'G4', group_name: '营销活动组', leader: '陈静', headcount: 6, channels: 'TAOBAO,TMALL,PDD,DOUYIN' },
  { group_id: 'G5', group_name: '投诉与升级组', leader: '李敏', headcount: 5, channels: 'TAOBAO,TMALL' },
  { group_id: 'G6', group_name: '品牌与信任组', leader: '周琳', headcount: 3, channels: 'TAOBAO,TMALL' },
]

export const SEED_ROLES = [
  { role_code: 'AGENT', role_name: '坐席', landing: '/console/sessions', sort_order: 1 },
  { role_code: 'SUPERVISOR', role_name: '客服主管', landing: '/console/sessions', sort_order: 2 },
  { role_code: 'KB_OPERATOR', role_name: '知识库运营', landing: '/admin/kb', sort_order: 3 },
  { role_code: 'ADMIN', role_name: '系统管理员', landing: '/admin/metrics', sort_order: 4 },
] as const

export const SEED_USERS: SeedUser[] = [
  // 四个业务角色 —— 与前端登录页原来的四张角色卡一一对应
  { user_id: 'U0001', username: 'wangfang', display_name: '王芳', role_code: 'AGENT', group_id: 'G3' },
  { user_id: 'U0002', username: 'limin', display_name: '李敏', role_code: 'SUPERVISOR', group_id: 'G1' },
  { user_id: 'U0003', username: 'zhangtao', display_name: '张涛', role_code: 'KB_OPERATOR', group_id: null },
  { user_id: 'U0004', username: 'chenjing', display_name: '陈静', role_code: 'ADMIN', group_id: null },
  // 额外的坐席账号：用来验证「同角色不同人接管会冲突」
  { user_id: 'U0005', username: 'liuyang', display_name: '刘洋', role_code: 'AGENT', group_id: 'G3' },
  { user_id: 'U0006', username: 'zhaolei', display_name: '赵磊', role_code: 'AGENT', group_id: 'G1' },
  { user_id: 'U0007', username: 'sunyue', display_name: '孙悦', role_code: 'AGENT', group_id: 'G2' },
]

/**
 * 幂等初始化：只在 users 表为空时写入种子。
 *
 * 密码哈希是异步的，所以这个函数返回 Promise；
 * 调用方（handlers 首次请求）用 `await ensureSeeded()` 保证只跑一次。
 */
let seeded: Promise<void> | null = null

export function ensureSeeded(): Promise<void> {
  if (seeded) return seeded
  seeded = runSeed()
  return seeded
}

async function runSeed(): Promise<void> {
  const db = getDb()
  const existing = db.prepare('SELECT COUNT(*) AS n FROM users').get() as { n: number }
  if (existing.n > 0) return

  const now = new Date().toISOString()

  const insertRole = db.prepare(
    'INSERT OR IGNORE INTO roles (role_code, role_name, landing, sort_order) VALUES (?, ?, ?, ?)',
  )
  for (const role of SEED_ROLES) {
    insertRole.run(role.role_code, role.role_name, role.landing, role.sort_order)
  }

  const insertGroup = db.prepare(
    'INSERT OR IGNORE INTO groups (group_id, group_name, leader, headcount, channels) VALUES (?, ?, ?, ?, ?)',
  )
  for (const group of SEED_GROUPS) {
    insertGroup.run(group.group_id, group.group_name, group.leader, group.headcount, group.channels)
  }

  // 同一个口令只算一次哈希会更快，但会让同口令账号的 salt 相同。
  // 密码安全上不可接受，所以每个账号单独哈希。
  const insertUser = db.prepare(
    `INSERT INTO users (
       user_id, username, display_name, role_code, group_id, password_hash,
       status, failed_count, locked_until, must_change,
       last_login_at, last_login_ip, created_at, updated_at, created_by
     ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', 0, NULL, 0, NULL, NULL, ?, ?, 'system')`,
  )

  for (const user of SEED_USERS) {
    const hash = await hashPassword(DEMO_PASSWORD)
    insertUser.run(
      user.user_id,
      user.username,
      user.display_name,
      user.role_code,
      user.group_id,
      hash,
      now,
      now,
    )
  }
}

/** 供测试断言：种子账号的完整列表（不含哈希）。 */
export function listSeedUsernames(): string[] {
  const rows = getDb()
    .prepare('SELECT username FROM users ORDER BY user_id')
    .all() as Array<{ username: string }>
  return rows.map((row) => row.username)
}

export type { UserRow }
