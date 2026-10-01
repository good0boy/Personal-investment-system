<template>
  <div class="page">
    <div class="page-header">
      <h2>系统状态</h2>
      <p>
        阶段 0 验收面：确认数据源、交易日历、存储、元数据库、调度任务是否真的就位。
        这里展示的全部是后端实时返回的数据，不是静态占位。
      </p>
    </div>

    <!-- 顶部：综合状态 -->
    <div class="panel">
      <div class="panel-title">
        <span>总览</span>
        <span class="sub">刷新时间 {{ serverTime }}</span>
      </div>

      <el-alert
        v-if="!health"
        type="error"
        :closable="false"
        show-icon
        title="无法连接后端服务"
        description="请确认后端已启动：cd backend && python -m app.main（默认 http://127.0.0.1:8000）"
      />

      <template v-else>
        <el-alert
          v-if="health.warnings.length"
          type="warning"
          :closable="false"
          show-icon
          class="mb-12"
          title="存在告警"
        >
          <ul class="warn-list">
            <li v-for="(w, i) in health.warnings" :key="i">{{ w }}</li>
          </ul>
        </el-alert>

        <div class="metric-grid">
          <div class="metric">
            <p class="label">后端状态</p>
            <p class="value" :class="statusClass">{{ statusText }}</p>
            <p class="hint">运行 {{ uptimeText }}</p>
          </div>
          <div class="metric">
            <p class="label">Python</p>
            <p class="value">{{ health.python_version }}</p>
            <p class="hint">{{ health.app_name }} v{{ health.app_version }}</p>
          </div>
          <div class="metric">
            <p class="label">akshare</p>
            <p class="value">{{ health.akshare_version }}</p>
            <p class="hint">广度源 · 已锁版本</p>
          </div>
          <div class="metric">
            <p class="label">baostock</p>
            <p class="value">{{ health.baostock_version }}</p>
            <p class="hint">行情主源</p>
          </div>
        </div>
      </template>
    </div>

    <template v-if="health">
      <!-- 数据源 -->
      <div class="panel">
        <div class="panel-title">
          <span>数据源连通性</span>
          <span class="sub">轻量检查（不触发真实请求），点「深度探测」做真实调用</span>
        </div>

        <el-table :data="health.datasources" size="small" border>
          <el-table-column prop="name" label="数据源" width="110" />
          <el-table-column label="可用" width="80">
            <template #default="{ row }">
              <el-tag :type="row.available ? 'success' : 'danger'" size="small" effect="plain">
                {{ row.available ? '可用' : '不可用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="version" label="版本" width="110" />
          <el-table-column prop="message" label="说明" min-width="240" show-overflow-tooltip />
          <el-table-column label="延迟" width="90">
            <template #default="{ row }">
              <span class="mono">{{ row.latency_ms != null ? row.latency_ms + ' ms' : '-' }}</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="btn-row">
          <el-button size="small" :loading="probing" @click="doDeepProbe">
            深度探测（真实请求）
          </el-button>
          <el-button size="small" @click="doRefreshCalendar" :loading="calRefreshing">
            重新同步交易日历
          </el-button>
        </div>

        <div v-if="probeResult" class="code-block mt-12">{{ probeResult }}</div>
      </div>

      <!-- 限流 / 熔断 -->
      <div class="panel">
        <div class="panel-title">
          <span>限流与熔断</span>
          <span class="sub">
            窗口内调用数 / 熔断状态 / 累计等待 —— 数据源被限频时先看这里
          </span>
        </div>

        <el-alert
          v-if="anyBreakerOpen"
          type="error"
          :closable="false"
          show-icon
          class="mb-12"
          title="有数据源正被熔断"
          :description="breakerAlertText"
        />

        <el-table :data="throttleRows" size="small" border>
          <el-table-column label="数据源 · 线路" width="170">
            <template #default="{ row }">
              <span class="mono">{{ row.name }}</span>
              <span v-if="row.group" class="sub"> · {{ row.group }}</span>
            </template>
          </el-table-column>
          <el-table-column label="熔断" width="110">
            <template #default="{ row }">
              <el-tag
                :type="row.open ? 'danger' : row.softStrikes > 0 ? 'warning' : 'success'"
                size="small"
                effect="plain"
              >
                {{ row.open ? `熔断中 ${row.remaining}s` : row.softStrikes > 0 ? `预警 ${row.softStrikes}/${row.softThreshold}` : '正常' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="窗口内调用" width="130">
            <template #default="{ row }">
              <span class="mono">{{ row.callsInWindow }}<span class="sub">
                / {{ row.maxPerMinute > 0 ? row.maxPerMinute + ' 次每分' : '不限次' }}</span></span>
            </template>
          </el-table-column>
          <el-table-column label="最小间隔" width="100">
            <template #default="{ row }">
              <span class="mono">{{ row.minInterval !== null ? row.minInterval + ' s' : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="累计调用" width="100">
            <template #default="{ row }"><span class="mono">{{ row.totalCalls }}</span></template>
          </el-table-column>
          <el-table-column label="累计等待" width="110">
            <template #default="{ row }">
              <span class="mono">{{ row.totalWait !== null ? row.totalWait + ' s' : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="历史熔断次数" width="120">
            <template #default="{ row }"><span class="mono">{{ row.tripCount }}</span></template>
          </el-table-column>
          <el-table-column label="最近一次原因" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="text-small">{{ row.reason || (row.lastTripAt || '-') }}</span>
            </template>
          </el-table-column>
        </el-table>

        <p class="hint mt-8">
          「累计调用 / 累计等待」是从**进程启动**起算的。等待时长变大说明正在被限频；
          「预警 n/m」是连续 soft 失败计数（连接被掐断等），达到 m 次才熔断 ——
          这是为了不被单次网络抖动误伤（见 backend/app/services/data_source/throttle.py）。
        </p>
      </div>

      <!-- 交易日历 + 存储 -->
      <el-row :gutter="16">
        <el-col :xs="24" :md="12">
          <div class="panel">
            <div class="panel-title">
              <span>交易日历</span>
              <span class="sub">来源 baostock</span>
            </div>
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="覆盖区间">
                <span class="mono">
                  {{ health.calendar?.first_date }} ~ {{ health.calendar?.coverage_end }}
                </span>
                <span class="text-muted text-small">
                  （含未来排期，业务只用到 {{ health.calendar?.last_date }}）
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="交易日总数">
                <span class="mono">{{ health.calendar?.trading_days?.toLocaleString() }}</span>
                <span
                  v-if="(health.calendar?.future_trading_days ?? 0) > 0"
                  class="text-muted text-small"
                >
                  （其中未来 {{ health.calendar?.future_trading_days }} 天）
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="最近已发生交易日">
                <span class="mono">{{ health.calendar?.last_date }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="今天是交易日">
                <el-tag
                  :type="health.calendar?.today_is_trading_day ? 'success' : 'info'"
                  size="small"
                  effect="plain"
                >
                  {{ health.calendar?.today_is_trading_day ? '是' : '否' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="上次同步">
                <span class="mono">{{ health.calendar?.synced_at || '-' }}</span>
              </el-descriptions-item>
            </el-descriptions>
          </div>
        </el-col>

        <el-col :xs="24" :md="12">
          <div class="panel">
            <div class="panel-title">
              <span>本地存储</span>
              <span class="sub">合计 {{ health.storage?.total_human }}</span>
            </div>
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="数据根目录">
                <span class="mono text-small">{{ health.storage?.data_root }}</span>
              </el-descriptions-item>
              <el-descriptions-item
                v-for="(v, k) in health.storage?.sections"
                :key="k"
                :label="sectionLabel(String(k))"
              >
                <span class="mono">
                  {{ v.files }} 个文件 ｜ {{ v.human }}
                </span>
              </el-descriptions-item>
            </el-descriptions>
          </div>
        </el-col>
      </el-row>

      <!-- 元数据库 + 调度 -->
      <el-row :gutter="16">
        <el-col :xs="24" :md="12">
          <div class="panel">
            <div class="panel-title">
              <span>元数据库</span>
              <el-tag
                :type="health.database?.initialized ? 'success' : 'danger'"
                size="small"
                effect="plain"
              >
                {{ health.database?.initialized ? '已初始化' : '未初始化' }}
              </el-tag>
            </div>
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="文件">
                <span class="mono text-small">{{ health.database?.db_path }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="大小">
                <span class="mono">{{ health.database?.size_human }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="表数量">
                <span class="mono">{{ tableCount }} 张</span>
              </el-descriptions-item>
            </el-descriptions>
            <div v-if="tableCount" class="table-chips">
              <el-tag
                v-for="(n, t) in health.database?.table_counts"
                :key="t"
                size="small"
                effect="plain"
                :type="n > 0 ? 'success' : 'info'"
              >
                {{ t }}<span class="mono"> {{ n }}</span>
              </el-tag>
            </div>
          </div>
        </el-col>

        <el-col :xs="24" :md="12">
          <div class="panel">
            <div class="panel-title">
              <span>调度任务</span>
              <el-tag
                :type="jobs?.scheduler.running ? 'success' : 'info'"
                size="small"
                effect="plain"
              >
                {{ jobs?.scheduler.running ? '运行中' : '未运行' }}
              </el-tag>
            </div>

            <el-table :data="jobs?.jobs || []" size="small" border>
              <el-table-column prop="job_id" label="任务" width="130" />
              <el-table-column prop="name" label="名称" width="120" />
              <el-table-column prop="trigger" label="触发" min-width="180" />
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'warning'" size="small" effect="plain">
                    {{ row.enabled ? '已启用' : '未启用' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>

            <el-alert
              v-if="jobs?.scheduler?.catch_up?.running"
              type="warning"
              :closable="false"
              show-icon
              class="mb-12"
              title="启动补跑进行中（后台线程）"
              :description="catchUpDesc"
            />

            <p class="text-muted text-small mt-8">
              本地阶段关机就会漏跑，启动时自动补跑已就位（catch_up_on_startup =
              {{ jobs?.scheduler.catch_up_on_startup }}，上限
              {{ jobs?.scheduler.catch_up_max_days }} 个交易日）。
            </p>

            <div class="btn-row">
              <el-button size="small" :loading="catchUpLoading" @click="doCatchUp">
                手动执行补跑扫描
              </el-button>
            </div>
          </div>
        </el-col>
      </el-row>

      <!-- 任务执行日志 -->
      <div class="panel">
        <div class="panel-title">
          <span>任务执行日志</span>
          <span class="sub">最近 {{ jobs?.recent_runs?.length || 0 }} 条</span>
        </div>
        <el-table :data="jobs?.recent_runs || []" size="small" border>
          <el-table-column prop="job_id" label="任务" width="130" />
          <el-table-column prop="run_date" label="逻辑运行日" width="120" />
          <el-table-column prop="trigger_type" label="触发" width="100" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="runStatusType(row.status)" size="small" effect="plain">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              <span class="mono">{{ row.duration != null ? row.duration + 's' : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="说明" min-width="260" show-overflow-tooltip />
          <el-table-column prop="created_at" label="时间" width="165" />
        </el-table>
        <el-empty
          v-if="!jobs?.recent_runs?.length"
          description="暂无执行记录（任务均为未启用状态）"
          :image-size="60"
        />
      </div>

      <!-- 数据源能力矩阵 -->
      <div class="panel">
        <div class="panel-title">
          <span>数据源能力矩阵</span>
          <span class="sub">
            共 {{ matrix?.source_count || 0 }} 个数据源 × {{ matrix?.capability_keys?.length || 0 }}
            项能力 · 实测日期以各源声明为准
          </span>
        </div>
        <el-table :data="matrixTable" size="small" border v-loading="matrixLoading">
          <el-table-column prop="capability" label="数据能力" width="190" fixed />
          <el-table-column
            v-for="s in matrix?.sources || []"
            :key="s.name"
            :label="s.display_name + '（' + s.role + '）'"
            min-width="220"
          >
            <template #default="{ row }">
              <div class="cap-cell">
                <el-tag
                  :type="row[s.name]?.supported ? 'success' : 'info'"
                  size="small"
                  effect="plain"
                >
                  {{ row[s.name]?.supported ? row[s.name]?.priority || '支持' : '不支持' }}
                </el-tag>
                <span class="cap-note text-muted text-small">{{ row[s.name]?.note }}</span>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <el-collapse class="mt-12">
          <el-collapse-item
            v-for="s in matrix?.sources || []"
            :key="s.name"
            :title="`${s.display_name} · 已知坑与稳定性（${s.pitfalls.length} 条）`"
          >
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="代码格式">{{ s.symbol_format }}</el-descriptions-item>
              <el-descriptions-item label="版本锁定">{{ s.version_pin }}</el-descriptions-item>
              <el-descriptions-item v-if="s.version_note" label="版本说明">
                {{ s.version_note }}
              </el-descriptions-item>
              <el-descriptions-item label="接口变更风险">
                <el-tag
                  :type="s.stability?.interface_change_risk === 'high' ? 'danger' : 'success'"
                  size="small"
                  effect="plain"
                >
                  {{ s.stability?.interface_change_risk || 'unknown' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="稳定性说明">
                {{ s.stability?.note }}
              </el-descriptions-item>
              <el-descriptions-item label="实测日期">{{ s.verified_at }}</el-descriptions-item>
            </el-descriptions>
            <ul class="pitfall-list">
              <li v-for="(p, i) in s.pitfalls" :key="i">{{ p }}</li>
            </ul>
          </el-collapse-item>
        </el-collapse>
      </div>

      <!-- 提醒配置 -->
      <div class="panel">
        <div class="panel-title">
          <span>企业微信提醒配置</span>
          <span class="sub">阶段 5 才启用，此处仅校验凭证是否已就位</span>
        </div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="渠道">群机器人 Webhook（方案 A）</el-descriptions-item>
          <el-descriptions-item label="凭证状态">
            <el-tag :type="health.wecom_configured ? 'success' : 'warning'" size="small" effect="plain">
              {{ health.wecom_configured ? '已配置' : '未配置' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Webhook Key">
            <span class="mono">{{ health.wecom_key_masked || '-' }}</span>
            <span class="text-muted text-small">（已脱敏，完整值只在 .env 中）</span>
          </el-descriptions-item>
          <el-descriptions-item label="推送开关">
            <el-tag type="info" size="small" effect="plain">未启用（模块 5 开发时打开）</el-tag>
          </el-descriptions-item>
        </el-descriptions>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import {
  getDatasourceMatrix,
  getHealthDeep,
  getJobs,
  refreshCalendar,
  triggerCatchUp,
  probeDatasource,
} from '@/api/system'
import { useHealthStore } from '@/stores/health'
import type { DatasourceMatrix, JobsData } from '@/types/api'

const healthStore = useHealthStore()
const { health } = storeToRefs(healthStore)

function refresh() {
  void healthStore.refresh()
}

const jobs = ref<JobsData | null>(null)
const matrix = ref<DatasourceMatrix | null>(null)
const matrixLoading = ref(false)
const probing = ref(false)
const probeResult = ref('')
const calRefreshing = ref(false)
const catchUpLoading = ref(false)

const serverTime = computed(() => health.value?.server_time ?? '-')

const statusText = computed(() => {
  const map: Record<string, string> = { ok: '正常', degraded: '部分降级', error: '异常' }
  return map[health.value?.status ?? ''] ?? '未知'
})

const statusClass = computed(() => {
  const s = health.value?.status
  // ⚠️ 不要用 text-up（红）/ text-down（绿）表示系统状态 ——
  //    本 App 里红=涨、绿=跌，会被误读成行情信号
  if (s === 'ok') return 'status-ok'
  if (s === 'degraded') return 'status-warn'
  return 'status-error'
})

const uptimeText = computed(() => {
  const s = health.value?.uptime_seconds ?? 0
  if (s < 60) return `${Math.round(s)} 秒`
  if (s < 3600) return `${Math.round(s / 60)} 分钟`
  return `${(s / 3600).toFixed(1)} 小时`
})

const tableCount = computed(() => Object.keys(health.value?.database?.table_counts ?? {}).length)

const matrixTable = computed(() => {
  const keys = matrix.value?.capability_keys ?? []
  const sources = matrix.value?.sources ?? []
  return keys.map((k) => {
    const row: Record<string, unknown> = { capability: k }
    for (const s of sources) row[s.name] = s.capabilities[k]
    return row
  })
})

// ------------------------------------------------------------------
// 限流 / 熔断（P1-3）
// ------------------------------------------------------------------
// ★ 后端一直在 payload 里塞 `throttle`，但响应模型没声明该字段 → 被 Pydantic
//   静默丢掉，所以这块 UI 以前根本拿不到数据。后端补了 schema 之后这里才成立。
interface ThrottleRow {
  name: string
  open: boolean
  remaining: number
  softStrikes: number
  softThreshold: number
  callsInWindow: number
  maxPerMinute: number | null
  minInterval: number | null
  totalCalls: number
  totalWait: number | null
  tripCount: number
  reason: string
  lastTripAt: string | null
  /** 熔断线路组（如 em_push2 / ths）。单组源为 undefined，不显示 */
  group?: string
}

const throttleRows = computed<ThrottleRow[]>(() => {
  const rows: ThrottleRow[] = []
  for (const d of health.value?.datasources ?? []) {
    const t = d.throttle
    const rl = t?.rate_limiter
    const cb = t?.circuit_breaker
    const base = {
      name: d.name,
      callsInWindow: rl?.calls_in_window ?? 0,
      maxPerMinute: rl?.max_calls_per_minute ?? null,
      minInterval: rl?.min_interval ?? null,
      totalCalls: rl?.total_calls ?? 0,
      totalWait: rl?.total_wait_seconds ?? null,
    }
    const groups = cb?.groups ?? []
    // 只有一个线路组时不显示组名（多数源就是这样，显示了只是噪音）
    if (groups.length <= 1) {
      const g = groups[0]
      rows.push({
        ...base,
        open: g ? g.open : cb?.open ?? false,
        remaining: g ? g.remaining_seconds : cb?.remaining_seconds ?? 0,
        softStrikes: g ? g.soft_strikes : cb?.soft_strikes ?? 0,
        softThreshold: g ? g.soft_threshold : cb?.soft_threshold ?? 0,
        tripCount: g ? g.trip_count : cb?.trip_count ?? 0,
        reason: g ? g.reason : cb?.reason ?? '',
        lastTripAt: g ? g.last_trip_at : cb?.last_trip_at ?? null,
      })
      continue
    }
    // ★ 多线路组：一组一行。这样"东财 push2 熔断、同花顺正常"一眼可见，
    //   而不是被聚合成一个"akshare 熔断了"的假象（这正是本页要修的表达问题）
    for (const g of groups) {
      rows.push({
        ...base,
        group: g.key,
        open: g.open,
        remaining: g.remaining_seconds,
        softStrikes: g.soft_strikes,
        softThreshold: g.soft_threshold,
        tripCount: g.trip_count,
        reason: g.reason,
        lastTripAt: g.last_trip_at,
      })
    }
  }
  return rows
})

const anyBreakerOpen = computed(() => throttleRows.value.some((r) => r.open))

const breakerAlertText = computed(() => {
  const hit = throttleRows.value.filter((r) => r.open)
  return (
    hit.map((r) => `${r.name}（剩余 ${r.remaining}s，原因：${r.reason || '未记录'}）`).join('；') +
    ' —— 熔断期间**一个请求都不会发出去**，接口试跑会直接失败，稍等会自动恢复。'
  )
})

/** 启动补跑进行中的说明文案（把"要等多久、能不能用"直接讲清楚）。 */
const catchUpDesc = computed(() => {
  const st = jobs.value?.scheduler?.catch_up
  if (!st?.running) return ''
  const started = st.started_at ? `自 ${st.started_at.replace('T', ' ')} 起` : ''
  return `${started}正在补齐缺失交易日（增量回补全市场行情，通常 30~90 分钟）。服务本身已可用，页面数据会随补跑推进而变全。`
})

function sectionLabel(key: string) {
  const map: Record<string, string> = {
    quote_daily: '个股日线',
    quote_index: '指数日线',
    factor: '因子',
    backtest: '回测结果',
    universe: '股票池',
    meta: '元数据',
  }
  return map[key] ?? key
}

function runStatusType(status: string) {
  const map: Record<string, 'success' | 'danger' | 'info' | 'warning'> = {
    success: 'success',
    failed: 'danger',
    running: 'warning',
    skipped: 'info',
  }
  return map[status] ?? 'info'
}

async function loadJobs() {
  try {
    jobs.value = await getJobs(20)
  } catch {
    jobs.value = null
  }
}

async function loadMatrix() {
  matrixLoading.value = true
  try {
    matrix.value = await getDatasourceMatrix()
  } catch {
    matrix.value = null
  } finally {
    matrixLoading.value = false
  }
}

async function doDeepProbe() {
  probing.value = true
  probeResult.value = ''
  try {
    const health = await getHealthDeep(true)
    const lines: string[] = ['【深度探测结果】']
    for (const d of health.datasources) {
      lines.push(
        `  ${d.name.padEnd(10)} ${d.available ? '可用' : '不可用'}  版本 ${d.version}  ${
          d.latency_ms ?? '-'
        } ms`,
      )
      if (d.message) lines.push(`      ${d.message}`)
    }
    for (const name of ['baostock', 'akshare', 'fuyao']) {
      try {
        const r = await probeDatasource(name)
        lines.push(`  [probe] ${name}: ${JSON.stringify(r)}`)
      } catch (e) {
        lines.push(`  [probe] ${name} 失败: ${String(e)}`)
      }
    }
    probeResult.value = lines.join('\n')
    ElMessage.success('深度探测完成')
    refresh()
  } catch {
    ElMessage.error('深度探测失败')
  } finally {
    probing.value = false
  }
}

async function doRefreshCalendar() {
  calRefreshing.value = true
  try {
    await refreshCalendar()
    ElMessage.success('交易日历已同步')
    refresh()
  } finally {
    calRefreshing.value = false
  }
}

async function doCatchUp() {
  catchUpLoading.value = true
  try {
    const r = await triggerCatchUp()
    ElMessage.success('补跑扫描完成')
    probeResult.value = JSON.stringify(r, null, 2)
    await loadJobs()
  } finally {
    catchUpLoading.value = false
  }
}

watch(health, () => loadJobs())

onMounted(() => {
  loadJobs()
  loadMatrix()
})
</script>

<style scoped>
.mb-12 {
  margin-bottom: 12px;
}

.mt-8 {
  margin-top: 8px;
}

.mt-12 {
  margin-top: 12px;
}

.mt-16 {
  margin-top: 16px;
}

.btn-row {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}

.status-warn {
  color: #b8860b;
}

.warn-list {
  margin: 4px 0 0;
  padding-left: 18px;
}

.table-chips {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.cap-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.cap-note {
  line-height: 1.5;
}

.pitfall-list {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.8;
  color: #5f6368;
}
</style>
