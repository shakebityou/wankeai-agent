/**
 * 密码哈希 —— Node 内置 scrypt，不引任何第三方加密库。
 *
 * 为什么用 scrypt 而不是 bcrypt：
 *   bcrypt 是原生模块，Windows 上要编译，装包就炸；scrypt 在 node:crypto 里，
 *   同样是密码哈希的标准算法（RFC 7914），参数可调，强度足够。
 *
 * 为什么用 scrypt 而不是 SHA-256：
 *   密码哈希的核心指标是「算得慢」。SHA-256 每秒能算几十亿次，
 *   一张彩虹表就能反查常见密码；scrypt 故意占用大量内存（N=16384 约 16MB），
 *   让离线爆破的性价比低到没有意义。
 *
 * 存储格式：scrypt$16384$8$1$<saltBase64>$<hashBase64>
 *   前缀式自描述 —— 将来调参数时，老哈希仍能用自己记录的参数验证。
 */
import { createHash, randomBytes, scrypt as scryptCb, timingSafeEqual } from 'node:crypto'

/** CPU/内存代价。16384 是 2^14，单次约 16MB 内存，服务端登录场景足够。 */
const SCRYPT_N = 16384
const SCRYPT_R = 8
const SCRYPT_P = 1
const KEY_LEN = 64
const SALT_LEN = 16

function scryptAsync(password: string, salt: Buffer, keyLen: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scryptCb(
      password.normalize('NFKC'),
      salt,
      keyLen,
      { N: SCRYPT_N, r: SCRYPT_R, p: SCRYPT_P, maxmem: 64 * 1024 * 1024 },
      (error, derived) => {
        if (error) reject(error)
        else resolve(derived as Buffer)
      },
    )
  })
}

/** 生成 `scrypt$N$r$p$salt$hash` 形式的哈希串。 */
export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(SALT_LEN)
  const derived = await scryptAsync(password, salt, KEY_LEN)
  return [
    'scrypt',
    SCRYPT_N,
    SCRYPT_R,
    SCRYPT_P,
    salt.toString('base64'),
    derived.toString('base64'),
  ].join('$')
}

/**
 * 校验密码。
 *
 * 用 timingSafeEqual 而不是 === 比较：
 *   === 会在第一个不同字节处提前返回，攻击者能通过响应时间逐位猜出哈希。
 *   timingSafeEqual 恒定时间，堵住这条路。
 * 解析失败一律返回 false —— 库里的脏数据不该变成登录漏洞。
 */
export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split('$')
  if (parts.length !== 6 || parts[0] !== 'scrypt') return false

  const n = Number(parts[1])
  const r = Number(parts[2])
  const p = Number(parts[3])
  if (!Number.isFinite(n) || !Number.isFinite(r) || !Number.isFinite(p)) return false

  const salt = Buffer.from(parts[4]!, 'base64')
  const expected = Buffer.from(parts[5]!, 'base64')
  if (salt.length === 0 || expected.length === 0) return false

  const actual = await new Promise<Buffer>((resolve, reject) => {
    scryptCb(
      password.normalize('NFKC'),
      salt,
      expected.length,
      { N: n, r, p, maxmem: 64 * 1024 * 1024 },
      (error, derived) => {
        if (error) reject(error)
        else resolve(derived as Buffer)
      },
    )
  })

  if (actual.length !== expected.length) return false
  return timingSafeEqual(actual, expected)
}

/** 登录态 token 的哈希：库里只存哈希，泄露库文件也无法直接冒用会话。 */
export function hashToken(token: string): string {
  // token 本身已是 32 字节随机数，无需慢哈希；sha256 只为防止明文入库
  return createHash('sha256').update(token).digest('hex')
}
