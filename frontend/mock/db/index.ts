/**
 * SQLite 连接与初始化。
 *
 * 为什么用 node:sqlite 而不是 better-sqlite3：
 *   后者是原生模块，Windows 上要现场编译，装包失败率高；
 *   node:sqlite 是 Node 22+ 内置的同步 API，零依赖零编译，
 *   同步接口正好适合「登录校验」这种短事务。
 *
 * 库文件落在 mock/db/wankeai.db，随仓库外的 .gitignore 排除 ——
 * 账号密码属于运行态数据，不该进版本库。
 */
import { DatabaseSync } from 'node:sqlite'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const DB_DIR = here
const DB_FILE = join(DB_DIR, 'wankeai.db')
const SCHEMA_FILE = join(here, 'schema.sql')

let db: DatabaseSync | null = null

export function getDb(): DatabaseSync {
  if (db) return db

  if (!existsSync(DB_DIR)) mkdirSync(DB_DIR, { recursive: true })

  const instance = new DatabaseSync(DB_FILE)
  // 外键约束在 SQLite 里默认关闭，必须每条连接单独开
  instance.exec('PRAGMA foreign_keys = ON')
  instance.exec(readFileSync(SCHEMA_FILE, 'utf-8'))
  db = instance
  return db
}

/** 测试或重置用：关闭当前连接，下次 getDb() 重新打开。 */
export function closeDb(): void {
  if (db) {
    db.close()
    db = null
  }
}

// ─────────────────────────────────────────────────────────────
// 行类型：与 schema.sql 一一对应
// ─────────────────────────────────────────────────────────────

export type UserStatus = 'ACTIVE' | 'DISABLED' | 'LOCKED'

export interface UserRow {
  user_id: string
  username: string
  display_name: string
  role_code: string
  group_id: string | null
  password_hash: string
  status: UserStatus
  failed_count: number
  locked_until: string | null
  must_change: number
  last_login_at: string | null
  last_login_ip: string | null
  created_at: string
  updated_at: string
  created_by: string | null
}

export interface GroupRow {
  group_id: string
  group_name: string
  leader: string | null
  headcount: number
  channels: string
}

export interface AuthSessionRow {
  token_hash: string
  user_id: string
  issued_at: string
  expires_at: string
  revoked_at: string | null
  user_agent: string | null
}

export interface AuditRow {
  id: number
  at: string
  actor_id: string | null
  actor_name: string | null
  action: string
  target: string | null
  detail: string | null
  ip: string | null
}

/** 写审计日志：HC-2 要求账号相关操作可追溯。 */
export function writeAudit(entry: {
  actor_id?: string | null
  actor_name?: string | null
  action: string
  target?: string | null
  detail?: unknown
  ip?: string | null
}): void {
  getDb()
    .prepare(
      `INSERT INTO audit_log (at, actor_id, actor_name, action, target, detail, ip)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      new Date().toISOString(),
      entry.actor_id ?? null,
      entry.actor_name ?? null,
      entry.action,
      entry.target ?? null,
      entry.detail === undefined ? null : JSON.stringify(entry.detail),
      entry.ip ?? null,
    )
}
