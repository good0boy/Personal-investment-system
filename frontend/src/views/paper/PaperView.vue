<template>
  <div class="paper-page">
    <!-- ===== 空态：首跑之前 ===== -->
    <template v-if="ov && ov.empty">
      <el-card shadow="never" class="empty-card">
        <el-empty :description="ov.note || '暂无净值数据'">
          <div class="empty-actions">
            <el-button type="primary" :loading="triggering" @click="onRunNow">
              立即跑一次
            </el-button>
            <el-button @click="load">刷新</el-button>
          </div>
          <div class="empty-hint">
            跑批 = 增量回补当日行情（约 30 分钟）+ 引擎全量重算（约 45 秒）。
            每个交易日 23:00 自动执行；数据源更新慢时顺延到次日补跑（保底方案）。
          </div>
        </el-empty>
      </el-card>
    </template>

    <!-- ===== 正常态 ===== -->
    <template v-else-if="ov">
      <!-- 顶部：账户 + 关键数字 -->
      <el-row :gutter="12" class="cards">
        <el-col :span="4">
          <div class="stat-card">
            <div class="stat-label">当前净值</div>
            <div class="stat-value">{{ fmtNum(ov.account.nav, 4) }}</div>
            <div class="stat-sub">资产 {{ fmtYuan(ov.account.cash) }}</div>
          </div>
        </el-col>
        <el-col :span="4">
          <div class="stat-card">
            <div class="stat-label">累计收益</div>
            <div class="stat-value" :class="retClass(ov.metrics?.total_return)">
              {{ fmtPct(ov.metrics?.total_return) }}
            </div>
            <div class="stat-sub">年化 {{ fmtPct(ov.metrics?.annual_return) }}</div>
          </div>
        </el-col>
        <el-col :span="4">
          <div class="stat-card">
            <div class="stat-label">最大回撤</div>
            <div class="stat-value loss">{{ fmtPct(ov.metrics?.max_drawdown) }}</div>
            <div class="stat-sub">夏普 {{ fmtNum(ov.metrics?.sharpe, 2) }}</div>
          </div>
        </el-col>
        <el-col :span="4">
          <div class="stat-card">
            <div class="stat-label">交易</div>
            <div class="stat-value">{{ ov.metrics?.trade_count ?? 0 }} 笔</div>
            <div class="stat-sub">胜率 {{ fmtPct(ov.metrics?.win_rate) }}</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="stat-card">
            <div class="stat-label">数据截止 / 上次跑批</div>
            <div class="stat-value small">{{ ov.data_as_of ?? '—' }}</div>
            <div class="stat-sub">
              {{ ov.account.last_run_date ?? '从未跑批' }} ｜ {{ ov.job?.last_log?.status ?? '无记录' }}
            </div>
          </div>
        </el-col>
        <el-col :span="2">
          <div class="stat-card actions">
            <el-button size="small" type="primary" :loading="triggering" @click="onRunNow">
              立即跑
            </el-button>
            <el-button size="small" @click="load">刷新</el-button>
          </div>
        </el-col>
      </el-row>

      <el-alert
        v-if="ov.note"
        type="warning"
        :title="ov.note"
        :closable="false"
        class="strategy-alert"
      />

      <!-- 主图：净值 vs 基准 -->
      <el-card shadow="never" class="chart-card">
        <template #header>净值曲线（策略 vs 上证指数基准，起点 {{ ov.start_date }}）</template>
        <EChart :option="navOption" height="360px" :empty="navEmpty" empty-text="暂无净值数据" />
      </el-card>

      <!-- 副图：回撤 -->
      <el-card shadow="never" class="chart-card">
        <template #header>回撤</template>
        <EChart :option="drawdownOption" height="180px" :empty="navEmpty" empty-text="暂无回撤数据" />
      </el-card>

      <el-row :gutter="12">
        <!-- 最新持仓 -->
        <el-col :span="14">
          <el-card shadow="never" class="chart-card">
            <template #header>
              最新持仓（{{ latestPosDate || '空仓' }} ｜ {{ ov.latest_positions?.length ?? 0 }} 只）
            </template>
            <el-table :data="ov.latest_positions ?? []" size="small" max-height="320">
              <el-table-column prop="symbol" label="代码" min-width="90" />
              <el-table-column prop="shares" label="股数" width="90" />
              <el-table-column label="成本价" width="90">
                <template #default="{ row }">{{ fmtNum(asRow(row).entry_price, 2) }}</template>
              </el-table-column>
              <el-table-column label="现价" width="90">
                <template #default="{ row }">{{ fmtNum(asRow(row).close, 2) }}</template>
              </el-table-column>
              <el-table-column label="浮动盈亏" width="110">
                <template #default="{ row }">
                  <span :class="pnlClass(asRow(row).unrealized_pnl)">
                    {{ fmtYuan(asRow(row).unrealized_pnl) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column prop="hold_days" label="持有日" width="80" />
            </el-table>
          </el-card>
        </el-col>

        <!-- 任务状态 -->
        <el-col :span="10">
          <el-card shadow="never" class="chart-card">
            <template #header>跑批任务状态</template>
            <div class="job-lines">
              <div><span class="k">调度</span>{{ ov.job?.schedule?.trigger ?? '未注册' }}</div>
              <div>
                <span class="k">下次运行</span>{{ ov.job?.schedule?.next_run_time ?? '—' }}
              </div>
              <div>
                <span class="k">最近执行</span>
                {{ ov.job?.last_log?.run_date ?? '—' }}
                <el-tag
                  v-if="ov.job?.last_log"
                  :type="logTagType(ov.job.last_log.status)"
                  size="small"
                  class="log-tag"
                >
                  {{ ov.job.last_log.status }}
                </el-tag>
              </div>
              <div v-if="ov.job?.last_log?.message" class="log-msg">
                {{ ov.job.last_log.message }}
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 最近交易 -->
      <el-card shadow="never" class="chart-card">
        <template #header>最近交易（最多 50 笔，倒序）</template>
        <el-table :data="ov.recent_trades ?? []" size="small" max-height="360">
          <el-table-column prop="symbol" label="代码" min-width="90" />
          <el-table-column prop="entry_date" label="买入日" width="100" />
          <el-table-column prop="exit_date" label="卖出日" width="100" />
          <el-table-column prop="shares" label="股数" width="90" />
          <el-table-column label="净盈亏" width="110">
            <template #default="{ row }">
              <span :class="pnlClass(asRow(row).net_pnl)">{{ fmtYuan(asRow(row).net_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="净收益率" width="100">
            <template #default="{ row }">
              <span :class="pnlClass(asRow(row).net_return)">
                {{ fmtPct(asRow(row).net_return) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="exit_reason" label="卖出原因" min-width="110" />
        </el-table>
      </el-card>
    </template>

    <!-- 加载失败 -->
    <el-empty v-else-if="loadError" :description="loadError" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { getPaperOverview, runPaperNow } from '@/api/paper'
import type { PaperOverview } from '@/types/api'

/** Element Plus 表格插槽的 row 是 DefaultRow（无业务类型），收口到这里 */
function asRow(row: unknown): Record<string, string | number | null> {
  return (row ?? {}) as Record<string, string | number | null>
}

const ov = ref<PaperOverview | null>(null)
const loadError = ref('')
const triggering = ref(false)
let pollTimer: ReturnType<typeof setTimeout> | null = null

async function load() {
  try {
    ov.value = await getPaperOverview('default')
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '加载失败，请确认后端已启动'
  }
}

onMounted(load)
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
})

async function onRunNow() {
  triggering.value = true
  try {
    await runPaperNow('default')
    ElMessage.success('已加入调度：回补行情约 30 分钟 + 重算约 45 秒，完成后自动可见')
    // 触发后轮询观察 last_run_date 变化（最多 8 次 × 30s）
    let n = 0
    const before = ov.value?.account.last_run_date
    const poll = async () => {
      n += 1
      await load()
      const after = ov.value?.account.last_run_date
      if (after && after !== before) {
        ElMessage.success(`跑批完成，净值已更新至 ${after}`)
        triggering.value = false
        return
      }
      if (n < 8) pollTimer = setTimeout(poll, 30_000)
      else triggering.value = false
    }
    pollTimer = setTimeout(poll, 30_000)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '触发失败')
    triggering.value = false
  }
}

// ---- 格式化 ----
function fmtNum(v: unknown, digits = 2): string {
  const n = typeof v === 'string' ? Number(v) : (v as number | null | undefined)
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return Number(n).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}
function fmtPct(v: unknown): string {
  const n = typeof v === 'string' ? Number(v) : (v as number | null | undefined)
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(2)}%`
}
function fmtYuan(v: unknown): string {
  const n = typeof v === 'string' ? Number(v) : (v as number | null | undefined)
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `¥${Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}
/** 收益/盈亏着色：正红负绿（中国市场惯例） */
function retClass(v: unknown): string {
  const n = typeof v === 'string' ? Number(v) : (v as number | null)
  if (n === null || n === undefined) return ''
  return n > 0 ? 'gain' : n < 0 ? 'loss' : ''
}
function pnlClass(v: unknown): string {
  return retClass(v)
}
function logTagType(status: string): 'success' | 'danger' | 'info' {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  return 'info'
}

// ---- 图表 ----
const navEmpty = computed(() => (ov.value?.nav_series?.length ?? 0) === 0)
const latestPosDate = computed(() => ov.value?.latest_positions?.[0]?.date ?? '')

const navOption = computed<EChartsOption>(() => {
  const pts = ov.value?.nav_series ?? []
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['策略净值', '上证指数（基准）'], top: 0 },
    grid: { left: 60, right: 24, top: 32, bottom: 56 },
    xAxis: { type: 'category', data: pts.map((p) => p.date) },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: unknown) => Number(v).toFixed(2) } },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 20, bottom: 8 },
    ],
    series: [
      {
        name: '策略净值',
        type: 'line',
        data: pts.map((p) => p.nav),
        showSymbol: false,
        lineStyle: { width: 1.6, color: '#c0392b' },
        itemStyle: { color: '#c0392b' },
      },
      {
        name: '上证指数（基准）',
        type: 'line',
        data: pts.map((p) => p.benchmark_nav),
        showSymbol: false,
        lineStyle: { width: 1.2, type: 'dashed', color: '#7f8c8d' },
        itemStyle: { color: '#7f8c8d' },
      },
    ],
  }
})

const drawdownOption = computed<EChartsOption>(() => {
  const pts = ov.value?.nav_series ?? []
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: unknown) => `${(Number(v) * 100).toFixed(2)}%`,
    },
    grid: { left: 60, right: 24, top: 12, bottom: 28 },
    xAxis: { type: 'category', data: pts.map((p) => p.date) },
    yAxis: {
      type: 'value',
      max: 0,
      axisLabel: { formatter: (v: unknown) => `${(Number(v) * 100).toFixed(0)}%` },
    },
    dataZoom: [{ type: 'inside' }],
    series: [
      {
        name: '回撤',
        type: 'line',
        data: pts.map((p) => p.drawdown),
        showSymbol: false,
        areaStyle: { color: 'rgba(39, 174, 96, 0.25)' },
        lineStyle: { width: 1, color: '#27ae60' },
        itemStyle: { color: '#27ae60' },
      },
    ],
  }
})
</script>

<style scoped>
.paper-page {
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
  font-size: 17px;
}
.stat-value.gain {
  color: #c0392b;
}
.stat-value.loss {
  color: #27ae60;
}
.stat-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.stat-card.actions {
  align-items: stretch;
  justify-content: center;
  gap: 6px;
}
.stat-card.actions :deep(.el-button) {
  margin-left: 0;
  width: 100%;
}
.chart-card :deep(.el-card__header) {
  font-size: 14px;
  font-weight: 600;
  padding: 10px 16px;
}
.strategy-alert {
  border-radius: 8px;
}
.gain {
  color: #c0392b;
}
.loss {
  color: #27ae60;
}
.job-lines {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 13px;
}
.job-lines .k {
  display: inline-block;
  width: 72px;
  color: var(--el-text-color-secondary);
}
.log-tag {
  margin-left: 8px;
}
.log-msg {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  word-break: break-all;
}
.empty-card {
  border-radius: 8px;
}
.empty-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-bottom: 10px;
}
.empty-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  max-width: 480px;
  margin: 0 auto;
}
</style>
