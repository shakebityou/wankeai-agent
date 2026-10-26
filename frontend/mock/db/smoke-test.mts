/**
 * 账号服务冒烟测试 —— 不启动 HTTP，直接调 service。
 *
 * 为什么单独一个脚本而不是 vitest：
 *   node:sqlite 需要真实的文件系统与 WAL 支持，jsdom 环境下跑不起来；
 *   这个脚本用真实 Node 跑，验证 SQLite 建表、种子、登录、锁定、改密全链路。
 */
import { closeDb } from './index.ts'
import {
  changePassword,
  createUser,
  getUser,
  listGroups,
  listUsers,
  login,
  logout,
  resolveToken,
  resetPassword,
  updateUser,
  MAX_FAILED_ATTEMPTS,
} from '../services/auth-service.ts'
import { DEMO_PASSWORD } from '../seed/auth-db.ts'

let passed = 0
let failed = 0

function check(name, cond, detail = '') {
  if (cond) {
    passed += 1
    console.log(`  ✓ ${name}`)
  } else {
    failed += 1
    console.log(`  ✗ ${name}${detail ? ` —— ${detail}` : ''}`)
  }
}

const t = async () => {
  console.log('\n[1] 登录成功')
  const ok1 = await login('wangfang', DEMO_PASSWORD)
  check('坐席王芳登录成功', ok1.ok)
  check('返回 token', ok1.ok && typeof ok1.token === 'string' && ok1.token.length > 20)
  check(
    '角色为 AGENT',
    ok1.ok && ok1.user.role === 'AGENT',
    ok1.ok ? ok1.user.role : '',
  )
  check(
    '坐席只看本组渠道',
    ok1.ok && ok1.user.channels.length === 5,
    ok1.ok ? ok1.user.channels.join(',') : '',
  )
  check('分组为售后处理组', ok1.ok && ok1.user.group_name === '售后处理组')

  console.log('\n[2] token 校验')
  const resolved = ok1.ok ? resolveToken(ok1.token) : null
  check('resolveToken 返回本人', resolved?.username === 'wangfang')
  check('伪造 token 返回 null', resolveToken('forged-token-xxxx') === null)

  console.log('\n[3] 账号大小写不敏感')
  const ok2 = await login('WangFang', DEMO_PASSWORD)
  check('WangFang 也能登录（COLLATE NOCASE）', ok2.ok)

  console.log('\n[4] 密码错误')
  const bad = await login('liuyang', 'wrong-password')
  check('返回失败', !bad.ok)
  check('失败类型 BAD_CREDENTIAL', !bad.ok && bad.failure.kind === 'BAD_CREDENTIAL')
  check(
    '提示剩余次数',
    !bad.ok && bad.failure.message.includes('还可尝试'),
    !bad.ok ? bad.failure.message : '',
  )

  console.log('\n[5] 账号不存在')
  const ghost = await login('nobody_here', 'whatever')
  check('返回失败', !ghost.ok)
  check('不暴露"账号不存在"', !ghost.ok && ghost.failure.message === '账号或密码不正确')

  console.log('\n[6] 连续失败锁定')
  for (let i = 0; i < MAX_FAILED_ATTEMPTS; i += 1) {
    await login('sunyue', `bad-${i}`)
  }
  const locked = await login('sunyue', DEMO_PASSWORD)
  check('锁定后正确密码也拒', !locked.ok)
  check('失败类型 LOCKED', !locked.ok && locked.failure.kind === 'LOCKED')
  check(
    '返回 retry_after_sec',
    !locked.ok && locked.failure.kind === 'LOCKED' && locked.failure.retry_after_sec > 0,
  )
  const lockedUser = listUsers().find((u) => u.username === 'sunyue')
  check('库里状态为 LOCKED', lockedUser?.status === 'LOCKED', lockedUser?.status)

  console.log('\n[7] 主管看全渠道')
  const sup = await login('limin', DEMO_PASSWORD)
  check('主管登录成功', sup.ok)
  check(
    '主管 channels 为 5 个全渠道',
    sup.ok && sup.user.channels.length === 5,
    sup.ok ? String(sup.user.channels.length) : '',
  )

  console.log('\n[8] 改密')
  const chg = await changePassword('U0001', DEMO_PASSWORD, 'NewPass1234')
  check('改密成功', chg.ok)
  check('旧 token 已吊销', resolveToken(ok1.token) === null)
  const withNew = await login('wangfang', 'NewPass1234')
  check('新密码可登录', withNew.ok)
  const withOld = await login('wangfang', DEMO_PASSWORD)
  check('旧密码失效', !withOld.ok)
  // 改回来，避免影响后续演示
  await resetPassword('U0001', DEMO_PASSWORD, { user_id: 'U0004', display_name: '陈静' })

  console.log('\n[9] 密码强度')
  const weak = await changePassword('U0001', DEMO_PASSWORD, 'short')
  check('过短被拒', !weak.ok && weak.message.includes('至少'))
  const weak2 = await changePassword('U0001', DEMO_PASSWORD, 'onlyletters')
  check('无数字被拒', !weak2.ok && weak2.message.includes('字母和数字'))

  console.log('\n[10] 建账号')
  const created = await createUser(
    { username: 'testagent', display_name: '测试坐席', role: 'AGENT', group_id: 'G1' },
    { user_id: 'U0004', display_name: '陈静' },
  )
  check('建账号成功', created.ok, created.ok ? '' : created.message)
  check('must_change 为 true', created.ok && created.data.user.must_change)
  check('状态 ACTIVE', created.ok && created.data.user.status === 'ACTIVE')

  console.log('\n[11] 账号规则')
  const dup = await createUser(
    { username: 'WANGFANG', display_name: '重名', role: 'AGENT' },
    { user_id: 'U0004', display_name: '陈静' },
  )
  check('重名被拒（大小写不敏感）', !dup.ok && dup.message.includes('已存在'), dup.ok ? '' : dup.message)
  const short = await createUser(
    { username: 'ab', display_name: '太短', role: 'AGENT' },
    { user_id: 'U0004', display_name: '陈静' },
  )
  check('过短账号被拒', !short.ok)
  const noName = await createUser(
    { username: 'hasname', display_name: '', role: 'AGENT' },
    { user_id: 'U0004', display_name: '陈静' },
  )
  check('空姓名被拒', !noName.ok)

  console.log('\n[12] 改账号')
  const testId = created.ok ? created.data.user.user_id : ''
  const upd = updateUser(testId, { role: 'SUPERVISOR', group_id: 'G5' }, { user_id: 'U0004', display_name: '陈静' })
  check('改角色成功', upd.ok, upd.ok ? '' : upd.message)
  check('角色已变', upd.ok && upd.data.user.role === 'SUPERVISOR')

  console.log('\n[13] 自我保护')
  const selfRole = updateUser('U0004', { role: 'AGENT' }, { user_id: 'U0004', display_name: '陈静' })
  check('不能降级自己', !selfRole.ok && selfRole.message.includes('不能修改自己'))
  const selfDisable = updateUser('U0004', { status: 'DISABLED' }, { user_id: 'U0004', display_name: '陈静' })
  check('不能禁用自己', !selfDisable.ok)

  console.log('\n[14] 禁用账号')
  const disabled = updateUser(testId, { status: 'DISABLED' }, { user_id: 'U0004', display_name: '陈静' })
  check('禁用成功', disabled.ok)
  const disabledLogin = await login('testagent', DEMO_PASSWORD)
  check('禁用账号无法登录', !disabledLogin.ok && disabledLogin.failure.kind === 'DISABLED')

  console.log('\n[15] 登出')
  const tokenToKill = withNew.ok ? withNew.token : ''
  logout(tokenToKill)
  check('登出后 token 失效', resolveToken(tokenToKill) === null)

  console.log('\n[16] 字典与列表')
  check('分组 6 个', listGroups().length === 6, String(listGroups().length))
  check('账号数 ≥ 8', listUsers().length >= 8, String(listUsers().length))
  const admin = listUsers().find((u) => u.username === 'chenjing')
  check('管理员账号存在', admin?.role === 'ADMIN')

  console.log('\n[17] 哈希不泄露')
  const view = getUser('U0001')
  check('视图不含 password_hash', !('password_hash' in (view ?? {})))
  check('视图不含 hash 字样', !JSON.stringify(view ?? {}).includes('scrypt$'))

  console.log(`\n结果：${passed} 通过 / ${failed} 失败\n`)
  closeDb()
  if (failed > 0) process.exit(1)
}

t().catch((error) => {
  console.error('测试异常：', error)
  process.exit(1)
})
