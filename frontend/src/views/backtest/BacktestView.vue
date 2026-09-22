<!--
  模块 3 · 策略回测 —— 运行列表页

  ★ 本页**没有"运行回测"按钮**，这是设计而非遗漏：
    回测是分钟级计算密集任务（1115 天 × 全市场），放进 HTTP 请求链路会把服务卡死。
    它由 CLI 跑，结果落盘，前端只读（技术架构.md 架构纪律 5）。
-->
<template>
  <div class="page">
    <!-- 说明 -->
    <div class="hint-bar">
      <el-icon class="hint-icon"><InfoFilled /></el-icon>
      <div class="hint-body">
        <p class="hint-title">回测在本地 CLI 运行，本页只读结果</p>
        <p class="hint-cmd mono">
          cd backend && python -m strategies.run --tag baseline
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <!-- 筛选 -->
    <div class="panel filter-bar">
      <div class="filters">
        <el-select
          v-model="filter.strategy"
          placeholder="全部策略"
          clearable
          size="small"
          style="width: 170px"
          @change="load"
        >
          <el-option
            v-for="s in strategies"
            :key="s.name"
            :label="`${s.name} v${s.version}`"
            :value="s.name"
          />
        </el-select>

        <el-select
          v-model="filter.status"
          placeholder="全部状态"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        >
          <el-option label="已完成" value="success" />
          <el-option label="产物不全" value="incomplete" />
          <el-option label="运行中" value="running" />
          <el-option label="失败" value="failed" />
        </el-select>

        <el-input
          v-model="filter.tag"
          placeholder="标签包含…"
          clearable
          size="small"
          style="width: 170px"
          @keyup.enter="load"
          @clear="load"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>

        <el-select v-model="sort" size="small" style="width: 190px" @change="load">
          <el-option v-for="o in SORT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>

        <el-button
          size="small"
          :type="sortDesc ? 'primary' : 'default'"
          plain
          @click="toggleDesc"
        >
          <el-icon><Sort /></el-icon>
          {{ sortDesc ? '降序' : '升序' }}
        </el-button>

        <!-- ★ 默认关掉：一次 12 组的参数寻优会留 12 个 run，
             全列出来会把手工跑的回测全挤到后面 -->
        <el-checkbox v-model="includeSweep" size="small" @change="load">
          含寻优子运行
        </el-checkbox>
      </div>

      <div class="filter-right">
        <span class="text-muted text-small">
          共 {{ total }} 个运行<span v-if="resp?.truncated">（仅显示前 {{ runs.length }}）</span>
        </span>
        <el-button
          size="small"
          type="primary"
          :disabled="selected.length < 2"
          @click="openCompare"
        >
          对比选中（{{ selected.length }}）
        </el-button>
      </div>
    </div>

    <!-- 表格 -->
    <div class="panel table-panel" v-loading="loading">
      <el-table
        ref="tableRef"
        :data="runs"
        size="small"
        stripe
        max-height="620px"
        class="run-table"
        empty-text="还没有回测结果。先跑一次：python -m strategies.run"
        @selection-change="onSelectionChange"
        @row-dblclick="openRow"
      >
        <el-table-column type="selection" width="40" :selectable="() => true" />

        <el-table-column label="策略 / 运行" min-width="240">
          <template #default="{ row }">
            <div class="cell-run">
              <div class="run-line">
                <span class="run-strategy">{{ row.strategy }}</span>
                <el-tag v-if="row.tag" size="small" effect="plain" class="run-tag">
                  {{ row.tag }}
                </el-tag>
                <el-tag v-if="row.is_mock" size="small" type="info" effect="dark">
                  模拟
                </el-tag>
                <!-- ★ 参数寻优的子运行：一次 sweep 留下 12 个这种 run，
                     不标出来列表页会被"格子"淹没（它们其实是同一批数据的 12 种参数） -->
                <el-tag v-if="row.sweep_id" size="small" type="primary" effect="plain">
                  寻优 {{ row.combo_index ?? '' }}
                </el-tag>
                <el-tag v-if="row.is_out_sample" size="small" type="warning" effect="plain">
                  样本外
                </el-tag>
                <!-- ★ 调试运行必须一眼可见：它与全市场 run 的产物结构完全一样 -->
                <el-tag v-if="row.debug_symbols" size="small" type="danger" effect="dark">
                  调试 {{ row.debug_symbols.length }} 只
                </el-tag>
              </div>
              <!-- 完成时间并进第一列，而不是单开一列：列太多会横向滚动，
                   滚动之后"操作"固定列会挡住右边的数值列 -->
              <div class="run-id mono">
                {{ fmtRunId(row.run_id) }}
                <span class="run-time">· {{ fmtDateTime(row.created_at) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="92" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="回测区间" width="186">
          <template #default="{ row }">
            <div class="cell-range mono">
              {{ row.start_date || DASH }} ~ {{ row.end_date || DASH }}
            </div>
            <div class="text-muted text-small">{{ row.trading_days ?? DASH }} 个交易日</div>
          </template>
        </el-table-column>

        <el-table-column label="总收益" width="94" align="right" sortable>
          <template #default="{ row }">
            <span :class="pnlClass(row.total_return)">{{ fmtPctSigned(row.total_return) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="年化" width="94" align="right">
          <template #default="{ row }">
            <span :class="pnlClass(row.annual_return)">{{ fmtPctSigned(row.annual_return) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="最大回撤" width="96" align="right">
          <template #default="{ row }">
            <span :class="pnlClass(row.max_drawdown)">{{ fmtPct(row.max_drawdown) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="夏普" width="76" align="right">
          <template #default="{ row }">{{ fmtNum(row.sharpe) }}</template>
        </el-table-column>

        <el-table-column label="胜率" width="82" align="right">
          <template #default="{ row }">{{ fmtPct(row.win_rate) }}</template>
        </el-table-column>

        <el-table-column label="交易数" width="78" align="right">
          <template #default="{ row }">{{ row.trade_count ?? DASH }}</template>
        </el-table-column>

        <!-- 规格强制展示的两个指标 -->
        <el-table-column label="年化成本" width="92" align="right">
          <template #default="{ row }">
            <el-tooltip content="★ 本策略持有期短，成本是主要敌人（规格 §3）" placement="top">
              <span class="cost-value">{{ fmtPct(row.annual_cost_ratio) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="最大单笔亏损" width="106" align="right">
          <template #default="{ row }">
            <el-tooltip content="★ 本策略不设个股止损，这是风险敞口的唯一窗口" placement="top">
              <span :class="pnlClass(row.max_single_loss)">
                {{ fmtPct(row.max_single_loss) }}
              </span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="80" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link size="small" type="primary" @click="openRow(row)">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 对比对话框 -->
    <el-dialog
      v-model="compareVisible"
      title="多运行净值对比"
      width="920px"
      top="6vh"
      destroy-on-close
    >
      <div v-loading="compareLoading">
        <div class="compare-meta">
          <el-tag
            v-for="s in compareData?.series || []"
            :key="`${s.strategy}/${s.run_id}`"
            size="small"
            effect="plain"
            :type="s.debug ? 'danger' : 'info'"
          >
            {{ s.label }}
            <span :class="pnlClass(s.total_return)" class="tag-pnl">
              {{ fmtPctSigned(s.total_return) }}
            </span>
          </el-tag>
        </div>
        <p class="compare-note text-muted text-small">
          各曲线按各自起点归一化为 1.0；区间外**断开**（不是补 0）——
          断线处就是这个运行没有数据的地方。
        </p>
        <EChart :option="compareOption" height="420px" :empty="!compareData?.count" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { compareRuns, getRuns } from '@/api/backtest'
import type { BacktestRunSummary, BacktestRunsResp, CompareResp } from '@/types/api'
import {
  DASH,
  fmtDateTime,
  fmtNum,
  fmtPct,
  fmtPctSigned,
  fmtRunId,
  pnlClass,
  statusLabel,
  statusTagType,
} from '@/utils/format'

const router = useRouter()

const SORT_OPTIONS = [
  { label: '按完成时间', value: 'created_at' },
  { label: '按总收益', value: 'total_return' },
  { label: '按年化收益', value: 'annual_return' },
  { label: '按夏普', value: 'sharpe' },
  { label: '按最大回撤', value: 'max_drawdown' },
  { label: '按胜率', value: 'win_rate' },
  { label: '按交易数', value: 'trade_count' },
  { label: '按年化成本', value: 'annual_cost_ratio' },
  { label: '按最大单笔亏损', value: 'max_single_loss' },
]

/** 对比上限与后端 `backtest_service.MAX_COMPARE` 一致 */
const MAX_COMPARE = 6

const loading = ref(false)
const resp = ref<BacktestRunsResp | null>(null)
const runs = computed(() => resp.value?.items ?? [])
const strategies = computed(() => resp.value?.strategies ?? [])
const total = computed(() => resp.value?.total ?? 0)

const filter = reactive<{ strategy?: string; status?: string; tag?: string }>({})
const sort = ref('created_at')
const sortDesc = ref(true)
/** 是否把参数寻优的子运行也列出来（默认否，否则列表页会被 sweep 的格子淹没） */
const includeSweep = ref(false)

const tableRef = ref()
const selected = ref<BacktestRunSummary[]>([])

async function load() {
  loading.value = true
  try {
    resp.value = await getRuns({
      strategy: filter.strategy || undefined,
      status: filter.status || undefined,
      tag: filter.tag || undefined,
      sort: sort.value,
      desc: sortDesc.value,
      limit: 200,
      exclude_sweep: !includeSweep.value,
    })
    // 筛选后原选中项可能已不在列表里 → 清掉，避免"对比了看不见的东西"
    selected.value = []
    tableRef.value?.clearSelection?.()
  } finally {
    loading.value = false
  }
}

function toggleDesc() {
  sortDesc.value = !sortDesc.value
  void load()
}

function onSelectionChange(rows: BacktestRunSummary[]) {
  if (rows.length > MAX_COMPARE) {
    ElMessage.warning(`最多同时对比 ${MAX_COMPARE} 个运行`)
    // 回滚最后一次勾选
    const last = rows[rows.length - 1]
    tableRef.value?.toggleRowSelection?.(last, false)
    return
  }
  selected.value = rows
}

function openDetail(row: BacktestRunSummary) {
  void router.push({
    name: 'backtest-detail',
    params: { strategy: row.strategy, runId: row.run_id },
  })
}

/**
 * el-table 的行参数类型是它自己的 `DefaultRow`（不是我们传入的行类型），
 * 插槽与事件都只能用 `unknown` 收口的包装函数接。
 * 这比在模板里到处写 `as` 断言干净。
 */
function openRow(row: unknown) {
  openDetail(row as BacktestRunSummary)
}

// ---------- 对比 ----------
const compareVisible = ref(false)
const compareLoading = ref(false)
const compareData = ref<CompareResp | null>(null)

async function openCompare() {
  if (selected.value.length < 2) return
  compareVisible.value = true
  compareLoading.value = true
  try {
    compareData.value = await compareRuns(
      selected.value.map((r) => ({ strategy: r.strategy, run_id: r.run_id })),
    )
  } finally {
    compareLoading.value = false
  }
}

const compareOption = computed<EChartsOption>(() => {
  const d = compareData.value
  if (!d || !d.count) return {}
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
    },
    legend: {
      top: 0,
      type: 'scroll',
      formatter: (name: string) => (name.length > 26 ? `${name.slice(0, 24)}…` : name),
    },
    grid: { left: 62, right: 22, top: 40, bottom: 58 },
    xAxis: {
      type: 'category',
      data: d.dates,
      boundaryGap: false,
      axisLabel: { fontSize: 11, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { fontSize: 11, formatter: (v: number) => v.toFixed(2) },
      splitLine: { lineStyle: { color: '#eef0f3' } },
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 18, bottom: 10 },
    ],
    series: d.series.map((s) => ({
      // 图例名要短：`strategy/20260918-101429-27dfa23f（baseline）` 会把图例撑爆
      name: (s.tag ? s.tag : s.run_id) + (s.debug ? '［调试］' : ''),
      type: 'line' as const,
      data: s.nav,
      showSymbol: false,
      // ★ 不连线：区间外留空是刻意的，连线会假装"那段也有数据"
      connectNulls: false,
      lineStyle: { width: 1.6 },
      emphasis: { focus: 'series' as const },
    })),
  }
})

onMounted(load)
</script>

<style scoped>
.hint-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--color-brand-soft);
  border: 0.5px solid #cfe2f5;
  border-radius: 10px;
  padding: 10px 16px;
  margin-bottom: 14px;
}

.hint-icon {
  color: var(--color-brand);
  font-size: 17px;
  flex: 0 0 auto;
}

.hint-body {
  flex: 1;
  min-width: 0;
}

.hint-title {
  margin: 0;
  font-size: 13px;
  font-weight: 500;
  color: #14456f;
}

.hint-cmd {
  margin: 3px 0 0;
  color: #4a7aa5;
  font-size: 12px;
}

.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  padding: 12px 16px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.table-panel {
  padding: 0;
  overflow: hidden;
}

.run-table {
  width: 100%;
}

/* 表格内的两行单元格 */
.cell-run {
  line-height: 1.45;
}

.run-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.run-strategy {
  font-weight: 500;
  color: #1f2329;
}

.run-tag {
  transform: scale(0.92);
}

.run-id {
  color: #9aa0a9;
  font-size: 11.5px;
  margin-top: 1px;
}

.run-time {
  margin-left: 4px;
  color: #b4b9c1;
}

.cell-range {
  font-size: 12.5px;
  color: #4a4f57;
}

.cost-value {
  color: var(--color-warn);
  font-weight: 500;
}

.compare-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.tag-pnl {
  margin-left: 6px;
  font-weight: 600;
}

.compare-note {
  margin: 0 0 8px;
}
</style>
