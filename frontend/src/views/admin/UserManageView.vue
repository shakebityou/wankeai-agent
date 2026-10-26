<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Refresh } from '@element-plus/icons-vue'
import { ROLE_LABELS, type UserRole } from '@/permission/matrix'
import { useAuthStore } from '@/stores/auth'
import {
  createUserApi,
  fetchAudit,
  fetchGroups,
  fetchUsers,
  resetPasswordApi,
  updateUserApi,
  type AccountUser,
  type AuditEntry,
  type GroupOption,
  type UserSummary,
} from '@/api/auth'
import { formatDateTime } from '@/utils/format'
import SectionCard from '@/components/common/SectionCard.vue'
import PageState from '@/components/common/PageState.vue'

/**
 * 账号管理（仅系统管理员）。
 *
 * 三条不可让步的设计：
 * 1. **密码不出现在任何响应里**：列表只展示账号、姓名、角色、分组、状态；
 *    新建/重置密码时输入框是本地的，提交后立刻清空，不回显。
 * 2. **不能对自己动手**：不允许改自己的角色、不允许禁用自己。
 *    否则一次误操作就能把系统里唯一的管理员锁在外面。
 * 3. **每一步都留痕**：新建/改角色/禁用/重置密码都写审计日志，
 *    页面底部能直接看到最近的账号变更记录，谁改的、什么时候改的一目了然。
 */
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref<string | null>(null)
const items = ref<AccountUser[]>([])
const summary = ref<UserSummary | null>(null)
const groups = ref<GroupOption[]>([])
const total = ref(0)

const filters = reactive({ keyword: '', role: '', status: '', group_id: '' })
const page = ref(1)
const pageSize = ref(20)

const audit = ref<AuditEntry[]>([])
const auditLoading = ref(false)

const ROLE_OPTIONS: Array<{ value: UserRole; label: string }> = [
  { value: 'AGENT', label: ROLE_LABELS.AGENT },
  { value: 'SUPERVISOR', label: ROLE_LABELS.SUPERVISOR },
  { value: 'KB_OPERATOR', label: ROLE_LABELS.KB_OPERATOR },
  { value: 'ADMIN', label: ROLE_LABELS.ADMIN },
]

const STATUS_LABELS: Record<AccountUser['status'], string> = {
  ACTIVE: '正常',
  DISABLED: '已停用',
  LOCKED: '已锁定',
}

const STATUS_TONE: Record<AccountUser['status'], string> = {
  ACTIVE: 'ok',
  DISABLED: 'bad',
  LOCKED: 'warn',
}

const isSelf = (row: AccountUser): boolean => row.user_id === auth.user?.user_id

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const data = await fetchUsers({
      keyword: filters.keyword,
      role: filters.role,
      status: filters.status,
      group_id: filters.group_id,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
    summary.value = data.summary
  } catch (error) {
    loadError.value = (error as { message?: string }).message ?? '账号列表加载失败'
  } finally {
    loading.value = false
  }
}

async function loadAudit(): Promise<void> {
  auditLoading.value = true
  try {
    const data = await fetchAudit(20)
    audit.value = data.items
  } catch {
    // 审计日志加载失败不该阻断主流程，静默置空
    audit.value = []
  } finally {
    auditLoading.value = false
  }
}

onMounted(async () => {
  const [, groupData] = await Promise.all([load(), fetchGroups().catch(() => ({ items: [] }))])
  groups.value = groupData.items
  void loadAudit()
})

function search(): void {
  page.value = 1
  void load()
}

function reset(): void {
  filters.keyword = ''
  filters.role = ''
  filters.status = ''
  filters.group_id = ''
  page.value = 1
  void load()
}

function changePage(next: number): void {
  page.value = next
  void load()
}

// ── 新建 / 编辑弹窗 ────────────────────────────────────────────

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const saving = ref(false)
const form = reactive({
  user_id: '',
  username: '',
  display_name: '',
  role: 'AGENT' as UserRole,
  group_id: '' as string,
  password: '',
  status: 'ACTIVE' as 'ACTIVE' | 'DISABLED',
})
const formError = reactive<Record<string, string>>({})

const dialogTitle = computed(() => (dialogMode.value === 'create' ? '新建账号' : '编辑账号'))

function openCreate(): void {
  dialogMode.value = 'create'
  Object.assign(form, {
    user_id: '',
    username: '',
    display_name: '',
    role: 'AGENT' as UserRole,
    group_id: groups.value[0]?.group_id ?? '',
    password: '',
    status: 'ACTIVE' as const,
  })
  clearFormError()
  dialogVisible.value = true
}

function openEdit(row: AccountUser): void {
  dialogMode.value = 'edit'
  Object.assign(form, {
    user_id: row.user_id,
    username: row.username,
    display_name: row.display_name,
    role: row.role,
    group_id: row.group_id ?? '',
    password: '',
    status: row.status === 'DISABLED' ? ('DISABLED' as const) : ('ACTIVE' as const),
  })
  clearFormError()
  dialogVisible.value = true
}

function clearFormError(): void {
  for (const key of Object.keys(formError)) delete formError[key]
}

function validate(): boolean {
  clearFormError()

  if (dialogMode.value === 'create') {
    const username = form.username.trim()
    if (!username) {
      formError.username = '请输入登录账号'
    } else if (!/^[a-zA-Z0-9._-]{3,32}$/.test(username)) {
      formError.username = '账号需 3–32 位，只允许字母、数字、点、下划线、短横线'
    }
    if (form.password && form.password.length > 0) {
      if (form.password.length < 8) formError.password = '密码至少 8 位'
      else if (!/[a-zA-Z]/.test(form.password) || !/\d/.test(form.password)) {
        formError.password = '密码需同时包含字母和数字'
      }
    }
  }

  if (!form.display_name.trim()) formError.display_name = '请输入姓名'

  return Object.keys(formError).length === 0
}

async function save(): Promise<void> {
  if (saving.value) return
  if (!validate()) return

  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      await createUserApi({
        username: form.username.trim(),
        display_name: form.display_name.trim(),
        role: form.role,
        group_id: form.group_id || null,
        password: form.password || undefined,
      })
      ElMessage.success('账号已创建，首次登录需修改密码')
    } else {
      await updateUserApi(form.user_id, {
        display_name: form.display_name.trim(),
        role: form.role,
        group_id: form.group_id || null,
        status: form.status,
      })
      ElMessage.success('账号已更新')
    }
    dialogVisible.value = false
    await Promise.all([load(), loadAudit()])
  } catch (error) {
    const err = error as { code?: number; message?: string }
    // 10001 是字段级校验失败，落到对应输入框旁边
    if (err?.code === 10001 && err.message) {
      formError.username = err.message
    }
  } finally {
    saving.value = false
  }
}

// ── 重置密码 ──────────────────────────────────────────────────

const resetVisible = ref(false)
const resetTarget = ref<AccountUser | null>(null)
const resetForm = reactive({ password: '', confirm: '' })
const resetError = ref('')
const resetting = ref(false)

function openReset(row: AccountUser): void {
  resetTarget.value = row
  resetForm.password = ''
  resetForm.confirm = ''
  resetError.value = ''
  resetVisible.value = true
}

async function confirmReset(): Promise<void> {
  if (!resetTarget.value || resetting.value) return
  resetError.value = ''

  if (resetForm.password.length < 8) {
    resetError.value = '密码至少 8 位'
    return
  }
  if (!/[a-zA-Z]/.test(resetForm.password) || !/\d/.test(resetForm.password)) {
    resetError.value = '密码需同时包含字母和数字'
    return
  }
  if (resetForm.password !== resetForm.confirm) {
    resetError.value = '两次输入的密码不一致'
    return
  }

  resetting.value = true
  try {
    await resetPasswordApi(resetTarget.value.user_id, resetForm.password)
    ElMessage.success(`已重置 ${resetTarget.value.display_name} 的密码，其所有会话已失效`)
    resetVisible.value = false
    resetForm.password = ''
    resetForm.confirm = ''
    await Promise.all([load(), loadAudit()])
  } catch (error) {
    resetError.value = (error as { message?: string }).message ?? '重置失败'
  } finally {
    resetting.value = false
  }
}

// ── 启用 / 停用 ───────────────────────────────────────────────

async function toggleStatus(row: AccountUser): Promise<void> {
  const nextStatus = row.status === 'DISABLED' ? 'ACTIVE' : 'DISABLED'
  const actionText = nextStatus === 'DISABLED' ? '停用' : '启用'

  try {
    await ElMessageBox.confirm(
      nextStatus === 'DISABLED'
        ? `停用后「${row.display_name}」将立即被踢下线，且无法再登录。确认停用？`
        : `启用后「${row.display_name}」可以重新登录。确认启用？`,
      `${actionText}账号`,
      { confirmButtonText: `确认${actionText}`, cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  try {
    await updateUserApi(row.user_id, { status: nextStatus })
    ElMessage.success(`已${actionText}「${row.display_name}」`)
    await Promise.all([load(), loadAudit()])
  } catch {
    // 拦截器已弹出错误提示
  }
}

function groupName(groupId: string | null): string {
  if (!groupId) return '—'
  return groups.value.find((group) => group.group_id === groupId)?.group_name ?? groupId
}

/** 当前表单角色是否需要绑定分组：只有坐席需要（坐席按分组看会话） */
const roleNeedsGroup = computed(() => form.role === 'AGENT')

/** 三态判断：PageState 需要单一状态值，这里统一算一次，模板里只读结果 */
const listState = computed<'loading' | 'error' | 'empty' | null>(() => {
  if (loading.value && items.value.length === 0) return 'loading'
  if (loadError.value) return 'error'
  if (items.value.length === 0) return 'empty'
  return null
})

const auditState = computed<'loading' | 'empty' | null>(() => {
  if (auditLoading.value && audit.value.length === 0) return 'loading'
  if (audit.value.length === 0) return 'empty'
  return null
})

/** 审计动作的中文名 */
const AUDIT_LABELS: Record<string, string> = {
  LOGIN: '登录',
  LOGOUT: '退出登录',
  LOGIN_LOCKED: '登录失败锁定',
  CHANGE_PASSWORD: '修改密码',
  USER_CREATE: '新建账号',
  USER_UPDATE: '修改账号',
  USER_RESET_PASSWORD: '重置密码',
}
</script>

<template>
  <div class="users wk-page">
    <header class="users__head">
      <div>
        <h1 class="users__title">账号管理</h1>
        <p class="users__sub">
          账号、角色与分组是权限的唯一来源：角色决定能进哪些页面，分组决定坐席能看哪些会话。
        </p>
      </div>
      <div class="users__head-actions">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建账号</el-button>
      </div>
    </header>

    <div v-if="summary" class="users__stats">
      <div class="stat">
        <span class="stat__label">账号总数</span>
        <span class="stat__value">{{ summary.total }}</span>
      </div>
      <div class="stat">
        <span class="stat__label">正常</span>
        <span class="stat__value is-ok">{{ summary.active }}</span>
      </div>
      <div class="stat">
        <span class="stat__label">已停用</span>
        <span class="stat__value is-bad">{{ summary.disabled }}</span>
      </div>
      <div class="stat">
        <span class="stat__label">已锁定</span>
        <span class="stat__value is-warn">{{ summary.locked }}</span>
      </div>
      <div class="stat stat--roles">
        <span class="stat__label">按角色分布</span>
        <span class="stat__roles">
          <span v-for="option in ROLE_OPTIONS" :key="option.value" class="stat__role">
            {{ option.label }}
            <b>{{ summary.by_role[option.value] }}</b>
          </span>
        </span>
      </div>
    </div>

    <el-card shadow="never" class="users__filter">
      <div class="filters">
        <el-input
          v-model="filters.keyword"
          class="filters__keyword"
          placeholder="搜索账号或姓名"
          :prefix-icon="Search"
          clearable
          @keyup.enter="search"
          @clear="search"
        />
        <el-select v-model="filters.role" placeholder="全部角色" clearable class="filters__select" @change="search">
          <el-option v-for="option in ROLE_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable class="filters__select" @change="search">
          <el-option label="正常" value="ACTIVE" />
          <el-option label="已停用" value="DISABLED" />
          <el-option label="已锁定" value="LOCKED" />
        </el-select>
        <el-select
          v-model="filters.group_id"
          placeholder="全部分组"
          clearable
          class="filters__select"
          @change="search"
        >
          <el-option v-for="group in groups" :key="group.group_id" :label="group.group_name" :value="group.group_id" />
        </el-select>
        <el-button @click="reset">重置</el-button>
      </div>
    </el-card>

    <PageState
      v-if="listState"
      :state="listState"
      :title="listState === 'empty' ? '没有匹配的账号' : ''"
      :description="listState === 'empty' ? '换个筛选条件试试，或新建一个账号' : ''"
      :detail="loadError ?? ''"
      retryable
      @retry="load"
    />
    <el-card v-else shadow="never" class="users__table-card">
        <el-table :data="items" class="users__table" stripe>
          <el-table-column label="账号" width="130">
            <template #default="{ row }">
              <code class="users__account">{{ (row as AccountUser).username }}</code>
            </template>
          </el-table-column>

          <el-table-column label="姓名" width="120">
            <template #default="{ row }">
              <span class="users__name">{{ (row as AccountUser).display_name }}</span>
              <span v-if="isSelf(row as AccountUser)" class="users__self">本人</span>
            </template>
          </el-table-column>

          <el-table-column label="角色" width="120">
            <template #default="{ row }">
              <span class="wk-pill wk-pill--brand">{{ ROLE_LABELS[(row as AccountUser).role] }}</span>
            </template>
          </el-table-column>

          <el-table-column label="所属分组" width="130">
            <template #default="{ row }">
              <span class="users__muted">{{ groupName((row as AccountUser).group_id) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <span class="wk-pill" :class="`wk-pill--${STATUS_TONE[(row as AccountUser).status]}`">
                {{ STATUS_LABELS[(row as AccountUser).status] }}
              </span>
            </template>
          </el-table-column>

          <el-table-column label="最后登录" width="160">
            <template #default="{ row }">
              <span class="users__muted">{{ formatDateTime((row as AccountUser).last_login_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" min-width="190">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row as AccountUser)">编辑</el-button>
              <el-button link type="primary" size="small" @click="openReset(row as AccountUser)">重置密码</el-button>
              <el-tooltip v-if="isSelf(row as AccountUser)" content="不能停用自己" placement="top">
                <span>
                  <el-button link type="danger" size="small" disabled>停用</el-button>
                </span>
              </el-tooltip>
              <el-button
                v-else
                link
                :type="(row as AccountUser).status === 'DISABLED' ? 'success' : 'danger'"
                size="small"
                @click="toggleStatus(row as AccountUser)"
              >
                {{ (row as AccountUser).status === 'DISABLED' ? '启用' : '停用' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="users__pager">
          <span class="users__pager-total users__total">共 {{ total }} 个账号</span>
          <el-pagination
            layout="prev, pager, next"
            :current-page="page"
            :page-size="pageSize"
            :total="total"
            background
            @current-change="changePage"
          />
        </div>
      </el-card>

    <SectionCard title="最近的账号变更" subtitle="所有账号操作都会留痕，可追溯到操作人与时间">
      <PageState v-if="auditState" :state="auditState" :title="auditState === 'empty' ? '暂无变更记录' : ''" />
      <ul v-else class="audit">
          <li v-for="entry in audit" :key="entry.id" class="audit__row">
            <span class="audit__time">{{ formatDateTime(entry.at) }}</span>
            <span class="audit__action">{{ AUDIT_LABELS[entry.action] ?? entry.action }}</span>
            <span class="audit__actor">{{ entry.actor_name ?? '系统' }}</span>
            <span v-if="entry.target" class="audit__target">{{ entry.target }}</span>
            <span v-else class="audit__target">—</span>
          </li>
      </ul>
    </SectionCard>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="480px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="登录账号" :error="formError.username">
          <el-input
            v-model="form.username"
            :disabled="dialogMode === 'edit'"
            placeholder="3–32 位字母数字，用于登录"
          />
          <span v-if="dialogMode === 'edit'" class="form-hint">账号创建后不可修改</span>
        </el-form-item>

        <el-form-item label="姓名" :error="formError.display_name">
          <el-input v-model="form.display_name" placeholder="真实姓名，会显示在会话与工单记录里" />
        </el-form-item>

        <el-form-item label="角色">
          <el-select v-model="form.role" :disabled="isSelf({ user_id: form.user_id } as AccountUser)" class="form-full">
            <el-option v-for="option in ROLE_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
          <span v-if="isSelf({ user_id: form.user_id } as AccountUser)" class="form-hint">不能修改自己的角色</span>
        </el-form-item>

        <el-form-item v-if="roleNeedsGroup" label="所属分组">
          <el-select v-model="form.group_id" class="form-full" placeholder="坐席按分组决定可见的会话范围">
            <el-option
              v-for="group in groups"
              :key="group.group_id"
              :label="`${group.group_name}（${group.group_id}）`"
              :value="group.group_id"
            />
          </el-select>
        </el-form-item>

        <el-form-item v-if="dialogMode === 'create'" label="初始密码" :error="formError.password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="留空则使用统一初始密码 Wk@2026demo"
            autocomplete="new-password"
          />
          <span class="form-hint">无论是否自定义，首次登录都会强制修改密码</span>
        </el-form-item>

        <el-form-item v-if="dialogMode === 'edit'" label="账号状态">
          <el-radio-group v-model="form.status" :disabled="isSelf({ user_id: form.user_id } as AccountUser)">
            <el-radio value="ACTIVE">正常</el-radio>
            <el-radio value="DISABLED">停用</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" title="重置密码" width="420px" :close-on-click-modal="false">
      <p class="reset__hint">
        为 <b>{{ resetTarget?.display_name }}</b>（{{ resetTarget?.username }}）设置新密码。
        重置后该账号所有已登录设备会立即失效，下次登录需再次修改密码。
      </p>
      <el-form label-position="top" @submit.prevent="confirmReset">
        <el-form-item label="新密码">
          <el-input v-model="resetForm.password" type="password" show-password placeholder="至少 8 位，需含字母和数字" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="resetForm.confirm" type="password" show-password placeholder="再输入一次" />
        </el-form-item>
        <p v-if="resetError" class="reset__error">{{ resetError }}</p>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="resetting" @click="confirmReset">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.users {
  display: flex;
  flex-direction: column;
  gap: $space-4;
  padding: $space-6;

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: $space-4;
  }

  &__title {
    margin: 0 0 $space-1;
    font-size: $text-xl;
    font-weight: 700;
    color: $ink-900;
  }

  &__sub {
    margin: 0;
    font-size: $text-xs;
    line-height: 1.7;
    color: $ink-500;
  }

  &__head-actions {
    display: flex;
    gap: $space-2;
  }

  &__stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr)) 2fr;
    gap: $space-3;
  }

  &__table-card {
    border-radius: $radius-md;
  }

  &__account {
    font-size: $text-xs;
    color: $ink-700;
  }

  &__name {
    font-weight: 600;
    color: $ink-900;
  }

  &__self {
    margin-left: 6px;
    padding: 1px 5px;
    border-radius: 3px;
    background: $brand-50;
    color: $brand-ink;
    font-size: 10px;
  }

  &__muted {
    font-size: $text-xs;
    color: $ink-500;
  }

  &__pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: $space-3;
  }

  &__pager-total {
    font-size: $text-xs;
    color: $ink-500;
  }
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: $space-3 $space-4;
  border: 1px solid $ink-150;
  border-radius: $radius-md;
  background: $surface;

  &__label {
    font-size: 11px;
    color: $ink-500;
  }

  &__value {
    font-size: $text-2xl;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: $ink-900;

    &.is-ok {
      color: $success-ink;
    }

    &.is-bad {
      color: $danger-ink;
    }

    &.is-warn {
      color: $warning-ink;
    }
  }

  &--roles {
    grid-column: span 1;
  }

  &__roles {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  &__role {
    padding: 1px 7px;
    border-radius: $radius-pill;
    background: $ink-100;
    color: $ink-700;
    font-size: 11px;

    b {
      margin-left: 3px;
      color: $ink-900;
    }
  }
}

.filters {
  display: flex;
  gap: $space-2;

  &__keyword {
    flex: 1 1 220px;
    max-width: 280px;
  }

  &__select {
    width: 140px;
  }
}

.form-full {
  width: 100%;
}

.form-hint {
  display: block;
  margin-top: 4px;
  font-size: 11px;
  color: $ink-400;
}

.audit {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;

  &__row {
    display: grid;
    grid-template-columns: 160px 110px 100px 1fr;
    gap: $space-3;
    padding: 7px 0;
    border-bottom: 1px solid $ink-100;
    font-size: $text-xs;

    &:last-child {
      border-bottom: 0;
    }
  }

  &__time {
    font-variant-numeric: tabular-nums;
    color: $ink-500;
  }

  &__action {
    font-weight: 600;
    color: $ink-800;
  }

  &__actor {
    color: $ink-600;
  }

  &__target {
    color: $ink-500;
  }
}

.reset {
  &__hint {
    margin: 0 0 $space-4;
    font-size: $text-xs;
    line-height: 1.8;
    color: $ink-600;
  }

  &__error {
    margin: 0;
    font-size: $text-xs;
    color: $danger-ink;
  }
}
</style>
