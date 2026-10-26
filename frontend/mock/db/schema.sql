-- ─────────────────────────────────────────────────────────────
-- 玩可爱智能客服 Agent —— 账号与权限库（SQLite）
--
-- 设计要点：
--   1. 角色只有四种，与前端 src/permission/matrix.ts 的 UserRole 一一对应，
--      不在数据库里新造名词 —— 前后端一旦各说各话，权限就一定对不上。
--   2. 密码只存 scrypt 哈希，永不存明文。哈希格式见 mock/seed/auth-db.ts。
--   3. 登录态用 sessions 表承载随机 token，服务端可随时吊销，
--      比 JWT 更适合「禁用账号立刻生效」这种运营诉求。
--   4. 所有写操作落 audit_log，满足 HC-2「可追溯」。
-- ─────────────────────────────────────────────────────────────

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 角色字典：保留表结构，便于将来加角色时不动 users 表
CREATE TABLE IF NOT EXISTS roles (
  role_code   TEXT PRIMARY KEY,
  role_name   TEXT NOT NULL,
  -- 该角色登录后的默认落地页；空表示由前端按权限矩阵决定
  landing     TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 0
);

-- 坐席分组：与商家材料「客服坐席分组表」的 G1–G6 对齐
CREATE TABLE IF NOT EXISTS groups (
  group_id    TEXT PRIMARY KEY,
  group_name  TEXT NOT NULL,
  leader      TEXT,
  headcount   INTEGER NOT NULL DEFAULT 0,
  -- 逗号分隔的渠道代码，坐席只能看本组渠道的会话
  channels    TEXT NOT NULL DEFAULT ''
);

-- 账号主体
CREATE TABLE IF NOT EXISTS users (
  user_id       TEXT PRIMARY KEY,
  -- 登录账号，全局唯一，大小写不敏感（见 idx_users_username_nocase）
  username      TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  role_code     TEXT NOT NULL REFERENCES roles(role_code),
  group_id      TEXT REFERENCES groups(group_id),
  -- scrypt 哈希，格式 scrypt$<N>$<r>$<p>$<saltB64>$<hashB64>
  password_hash TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'DISABLED', 'LOCKED')),
  -- 连续失败次数；达到 5 次置 LOCKED 并写入 locked_until
  failed_count  INTEGER NOT NULL DEFAULT 0,
  locked_until  TEXT,
  must_change   INTEGER NOT NULL DEFAULT 0,
  last_login_at TEXT,
  last_login_ip TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  created_by    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase
  ON users (username COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role_code);

-- 登录态：token 只存哈希，即使库文件泄露也无法直接冒用
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  issued_at   TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  revoked_at  TEXT,
  user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id);

-- 审计日志：登录、登出、改密、账号增删改都记一条
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor_id    TEXT,
  actor_name  TEXT,
  action      TEXT NOT NULL,
  target      TEXT,
  detail      TEXT,
  ip          TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_id);
