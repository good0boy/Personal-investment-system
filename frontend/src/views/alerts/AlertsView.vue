<template>
  <div class="alerts-page">
    <!-- ===== 顶部状态卡：渠道 / 凭证 / 限流 / 模板 ===== -->
    <el-row :gutter="12" class="cards">
      <el-col :span="5">
        <div class="stat-card">
          <div class="stat-label">提醒开关</div>
          <div class="stat-value">
            <el-tag :type="st?.enabled ? 'success' : 'info'" size="small" effect="dark">
              {{ st?.enabled ? '已开启' : '已关闭' }}
            </el-tag>
          </div>
          <div class="stat-sub">
            配置 {{ st?.configured ? '已就绪' : '缺失' }}
          </div>
        </div>
      </el-col>
      <el-col :span="5">
        <div class="stat-card">
          <div class="stat-label">当前渠道</div>
          <div class="stat-value small">{{ channelCn }}</div>
          <div class="stat-sub mono">{{ st?.webhook_key_masked || '—' }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card">
          <div class="stat-label">今日已发</div>
          <div class="stat-value">{{ counts.sent ?? 0 }}</div>
          <div class="stat-sub">
            失败 <span :class="(counts.failed ?? 0) > 0 ? 'loss' : ''">{{ counts.failed ?? 0 }}</span>
            ｜ 待发 {{ counts.pending ?? 0 }}
          </div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card">
          <div class="stat-label">限流窗口</div>
          <div class="stat-value small">
            {{ st?.rate?.calls_in_window ?? 0 }} / {{ st?.rate?.max_calls_per_minute ?? '—' }}
          </div>
          <div class="stat-sub">累计 {{ st?.rate?.total_calls ?? 0 }} 次调用</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-label">消息模板</div>
          <div class="stat-value small">{{ st?.template_count ?? 0 }} 个</div>
          <div class="stat-sub">
            关键词兜底
            <span class="mono">「{{ st?.required_keyword || '—' }}」</span>
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- 关键词警告：这是最难查的坑 -->
    <el-alert
      v-if="st && st.enabled && !st.configured"
      type="error"
      :closable="false"
      title="提醒开关已打开，但凭证未配置 —— 所有消息都会以「未配置」失败"
      description="请在 .env 中填写 WECOM_WEBHOOK_URL（方案 A）或 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_CORP_SECRET（方案 B）。"
      show-icon
    />
    <el-alert
      v-else
      type="info"
      :closable="false"
      show-icon
    >
      <template #title>
        企微群若勾选了「自定义关键词」，消息<b>不含该关键词会被静默丢弃</b>
        （接口仍返回 errcode=0，但手机收不到）。本模块所有消息强制带
        <span class="mono">「{{ st?.required_keyword || '持仓提醒' }}」</span>，模板里已保证。
      </template>
    </el-alert>

    <!-- ===== 操作区：测试发送 ===== -->
    <el-card shadow="never" class="ops-card">
      <template #header>
        <div class="ops-header">
          <span>链路测试</span>
          <div class="ops-actions">
            <el-input
              v-model="operator"
              size="small"
              placeholder="触发来源（仅记录用）"
              class="op-input"
            />
            <el-button
              type="primary"
              size="small"
              :loading="testing"
              @click="onTest"
            >
              发送测试消息
            </el-button>
            <el-button size="small" :loading="loading" @click="loadAll">刷新</el-button>
          </div>
        </div>
      </template>

      <div class="test-hint">
        点一下，消息应立刻到手机。此消息<b>故意不去重</b>（每次点都真发），
        用来验证「本地服务 → 企业微信」整条链路。
        <span v-if="lastTest" class="last-test">
          上次结果：
          <el-tag :type="lastTest.ok ? 'success' : 'danger'" size="small">
            {{ lastTest.ok ? '成功' : '失败' }}
          </el-tag>
          <span class="mono">{{ lastTest.message || lastTest.reason || '' }}</span>
          <span v-if="lastTest.waited" class="mono">（首试耗时 {{ lastTest.waited }}s）</span>
        </span>
      </div>
    </el-card>

    <!-- ===== 日志列表 ===== -->
    <el-card shadow="never" class="log-card">
      <template #header>
        <div class="ops-header">
          <span>提醒日志</span>
          <div class="ops-actions">
            <el-select
              v-model="filterStatus"
              size="small"
              placeholder="全部状态"
              clearable
              class="op-select"
              @change="onFilterChange"
            >
              <el-option
                v-for="s in statusOptions"
                :key="s.value"
                :label="`${s.label}（${counts[s.value] ?? 0}）`"
                :value="s.value"
              />
            </el-select>
            <el-select
              v-model="filterAction"
              size="small"
              placeholder="全部类型"
              clearable
              class="op-select"
              @change="onFilterChange"
            >
              <el-option
                v-for="a in actionOptions"
                :key="a.value"
                :label="a.label"
                :value="a.value"
              />
            </el-select>
            <el-button size="small" :loading="loading" @click="loadLogs">查询</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="logs"
        size="small"
        max-height="520"
        @row-click="onRowClick"
      >
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="created_at" label="时间" width="150" />
        <el-table-column prop="action_cn" label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="actionTagType(asRow(row).action)" size="small" effect="plain">
              {{ asRow(row).action_cn }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
        <el-table-column prop="symbol" label="标的" width="100">
          <template #default="{ row }">
            <span class="mono">{{ asRow(row).symbol || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusTagType(asRow(row).status)" size="small">
              {{ statusLabel(asRow(row).status) }}
            </el-tag>
            <span v-if="Number(asRow(row).retry_count) > 0" class="retry-badge">
              重试 {{ asRow(row).retry_count }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="耗时/失败原因" min-width="200">
          <template #default="{ row }">
            <span v-if="asRow(row).error_message" class="err-text">
              {{ asRow(row).error_message }}
            </span>
            <span v-else class="dim">{{ asRow(row).sent_at || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="asRow(row).status === 'failed'"
              size="small"
              type="primary"
              link
              :loading="resendingId === asRow(row).id"
              @click.stop="onResend(asRow(row).id as number)"
            >
              重发
            </el-button>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next, sizes"
          :page-sizes="[20, 50, 100, 200]"
          small
          @current-change="loadLogs"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <!-- ===== 模板预览 ===== -->
    <el-card shadow="never" class="tpl-card">
      <template #header>
        <div class="ops-header">
          <span>消息模板预览（{{ templates.length }} 个）</span>
          <span class="dim mono">{{ st?.templates_file || '' }}</span>
        </div>
      </template>

      <el-collapse v-model="openTemplates">
        <el-collapse-item
          v-for="t in templates"
          :key="t.action"
          :name="t.action"
        >
          <template #title>
            <span class="tpl-title">
              <el-tag size="small" effect="plain">{{ t.action_cn }}</el-tag>
              <span class="mono dim">{{ t.action }}</span>
              <el-tag size="small" :type="levelTagType(t.level)" effect="light">
                {{ t.level }}
              </el-tag>
            </span>
          </template>

          <el-row :gutter="16">
            <el-col :span="12">
              <div class="tpl-label">渲染效果（样例数据，脱敏）</div>
              <pre class="tpl-preview">{{ t.sample.content }}</pre>
            </el-col>
            <el-col :span="12">
              <div class="tpl-label">模板源码（config/alert_templates.yaml）</div>
              <pre class="tpl-source">{{ t.body }}</pre>
            </el-col>
          </el-row>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- ===== 详情抽屉 ===== -->
    <el-drawer v-model="drawerOpen" title="提醒详情" size="520px">
      <div v-if="active" class="detail">
        <div class="detail-row"><span class="k">编号</span>#{{ active.id }}</div>
        <div class="detail-row"><span class="k">类型</span>{{ active.action_cn }}（{{ active.action }}）</div>
        <div class="detail-row"><span class="k">状态</span>
          <el-tag :type="statusTagType(active.status)" size="small">
            {{ statusLabel(active.status) }}
          </el-tag>
          <span v-if="active.retry_count > 0" class="retry-badge">已重试 {{ active.retry_count }} 次</span>
        </div>
        <div class="detail-row"><span class="k">渠道</span>{{ active.channel }}</div>
        <div class="detail-row"><span class="k">幂等键</span><span class="mono small">{{ active.event_key }}</span></div>
        <div class="detail-row"><span class="k">创建</span>{{ active.created_at }}</div>
        <div class="detail-row"><span class="k">送达</span>{{ active.sent_at || '—' }}</div>
        <div v-if="active.error_message" class="detail-row error">
          <span class="k">失败原因</span>{{ active.error_message }}
        </div>
        <div class="detail-label">消息正文（库中原文）</div>
        <pre class="tpl-preview">{{ active.content }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getAlertLogs,
  getAlertMeta,
  getAlertStatus,
  getAlertTemplates,
  resendAlert,
  sendAlertTest,
} from '@/api/alerts'
import type {
  AlertChannelStatus,
  AlertEnumItem,
  AlertLogRow,
  AlertSendResp,
  AlertTemplatePreview,
} from '@/types/api'

/** Element Plus 表格插槽的 row 是 DefaultRow（无业务类型），收口到这里 */
function asRow(row: unknown): Record<string, string | number | null> {
  return (row ?? {}) as Record<string, string | number | null>
}

// ---- 状态 ----
const st = ref<AlertChannelStatus | null>(null)
const logs = ref<AlertLogRow[]>([])
const templates = ref<AlertTemplatePreview[]>([])
const actionOptions = ref<AlertEnumItem[]>([])
const statusOptions = ref<AlertEnumItem[]>([])
const counts = ref<Record<string, number>>({})

const loading = ref(false)
const testing = ref(false)
const resendingId = ref<number | null>(null)
const lastTest = ref<AlertSendResp | null>(null)
const operator = ref('手动测试')

const filterStatus = ref<string>('')
const filterAction = ref<string>('')
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)

const drawerOpen = ref(false)
const active = ref<AlertLogRow | null>(null)
const openTemplates = ref<string[]>([])

const channelCn = computed(() => {
  if (!st.value) return '—'
  return st.value.channel === 'webhook' ? '群机器人（方案 A）' : '自建应用（方案 B）'
})

// ---- 加载 ----
async function loadStatus() {
  try {
    st.value = await getAlertStatus()
    counts.value = { ...counts.value }
  } catch {
    /* client.ts 已弹错误提示 */
  }
}

async function loadLogs() {
  loading.value = true
  try {
    const resp = await getAlertLogs({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      status: filterStatus.value || undefined,
      action: filterAction.value || undefined,
    })
    logs.value = resp.items ?? []
    total.value = resp.total ?? 0
    counts.value = resp.status_counts ?? {}
  } catch {
    logs.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function loadTemplates() {
  try {
    templates.value = await getAlertTemplates()
    // 默认展开第一个，避免页面一片空白
    if (!openTemplates.value.length && templates.value.length) {
      openTemplates.value = [templates.value[0].action]
    }
  } catch {
    templates.value = []
  }
}

async function loadMeta() {
  try {
    const m = await getAlertMeta()
    actionOptions.value = m.actions ?? []
    statusOptions.value = m.statuses ?? []
  } catch {
    /* 下拉为空即可，不阻断页面 */
  }
}

function loadAll() {
  loadStatus()
  loadLogs()
  loadTemplates()
  loadMeta()
}

onMounted(loadAll)

function onFilterChange() {
  page.value = 1
  loadLogs()
}

function onSizeChange(size: number) {
  pageSize.value = size
  page.value = 1
  loadLogs()
}

// ---- 操作 ----
async function onTest() {
  testing.value = true
  try {
    const resp = await sendAlertTest(operator.value || '手动测试')
    lastTest.value = resp
    if (resp.ok) {
      ElMessage.success('测试消息已发送，请查看手机/企微群')
    } else {
      ElMessage.warning(resp.message || resp.reason || '发送未成功')
    }
    await Promise.all([loadLogs(), loadStatus()])
  } catch (err) {
    // 后端用 code!=0 返回失败，client.ts 已弹出中文原因；这里补一条记录便于页面显示
    lastTest.value = {
      ok: false,
      message: err instanceof Error ? err.message : '发送失败',
    }
    await loadLogs()
  } finally {
    testing.value = false
  }
}

async function onResend(alertId: number) {
  resendingId.value = alertId
  try {
    const resp = await resendAlert(alertId)
    if (resp.ok) {
      ElMessage.success(resp.message || '重发成功')
    } else {
      ElMessage.warning(resp.message || '重发未成功')
    }
    await Promise.all([loadLogs(), loadStatus()])
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '重发失败')
  } finally {
    resendingId.value = null
  }
}

function onRowClick(row: unknown) {
  active.value = row as AlertLogRow
  drawerOpen.value = true
}

// ---- 展示辅助 ----
function statusLabel(status: unknown): string {
  const hit = statusOptions.value.find((s) => s.value === status)
  return hit?.label ?? String(status ?? '—')
}

function statusTagType(status: unknown): 'success' | 'danger' | 'info' | 'warning' {
  if (status === 'sent') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'pending') return 'warning'
  return 'info'
}

function actionTagType(action: unknown): 'success' | 'danger' | 'warning' | 'info' {
  if (action === 'open_position') return 'danger' // 开仓 —— 红
  if (action === 'close_position') return 'success' // 清仓 —— 绿
  if (action === 'job_failed') return 'warning'
  return 'info'
}

function levelTagType(level: string): 'success' | 'danger' | 'info' | 'warning' {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}
</script>

<style scoped>
.alerts-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cards {
  margin-bottom: 0;
}
.stat-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 14px;
  height: 92px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  margin: 2px 0;
}
.stat-value.small {
  font-size: 16px;
}
.stat-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ops-card :deep(.el-card__header),
.log-card :deep(.el-card__header),
.tpl-card :deep(.el-card__header) {
  padding: 10px 16px;
}
.ops-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 14px;
  font-weight: 600;
}
.ops-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 400;
}
.op-input {
  width: 180px;
}
.op-select {
  width: 160px;
}
.test-hint {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.8;
}
.last-test {
  display: block;
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.last-test .mono {
  margin-left: 6px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.retry-badge {
  margin-left: 6px;
  font-size: 11px;
  color: var(--el-color-warning);
}
.err-text {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}
.dim {
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.gain {
  color: #c0392b;
}
.loss {
  color: #27ae60;
}
.tpl-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.tpl-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}
.tpl-preview,
.tpl-source {
  margin: 0;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tpl-preview {
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-primary);
}
.tpl-source {
  background: var(--el-fill-color);
  border: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-regular);
}
.detail {
  font-size: 13px;
}
.detail-row {
  display: flex;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.detail-row .k {
  flex: 0 0 72px;
  color: var(--el-text-color-secondary);
}
.detail-row.error {
  color: var(--el-color-danger);
}
.detail-row .small {
  font-size: 11px;
  word-break: break-all;
}
.detail-label {
  margin: 14px 0 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
