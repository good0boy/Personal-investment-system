<!--
  模块 3 · 参数寻优 —— 详情页

  这一页存在的唯一目的是**防止有人拿单点最优去实盘**：
    · 默认高亮的是「高原」组合（邻域平均最高），不是「尖峰」（单点最高）
    · 网格表格里尖峰组合标红，并且在顶部给出警告条
    · 热力图让人**一眼看出**参数挪一格收益掉多少 —— 这是敏感性最直观的形式

  数据来源：`data/sweep/{sweep_id}/`（grid.json + results.parquet + summary.md）
  只读，不触发计算。
-->
<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <el-button link size="small" @click="goBack">
        <el-icon><ArrowLeft /></el-icon>
        返回寻优列表
      </el-button>
      <h2>
        参数寻优详情
        <span class="mono sweep-id">{{ sweepId }}</span>
        <el-tag v-if="debugSymbols" size="small" type="danger" effect="dark">
          调试 {{ debugSymbols.length }} 只
        </el-tag>
        <el-tag v-else-if="detail" size="small" effect="plain" type="success">全市场</el-tag>
      </h2>
      <p v-if="detail">
        策略 <b>{{ detail.strategy }}</b>
        ｜区间 <span class="mono">{{ intervalText }}</span>
        ｜目标指标 <b>{{ objectiveLabel }}</b>
      </p>
    </div>

    <!-- ★ 调试运行警告：产物结构与全市场一模一样，不打标就会有人当真 -->
    <el-alert
      v-if="debugSymbols"
      type="error"
      :closable="false"
      show-icon
      class="alert-gap"
    >
      <template #title>
        本次寻优只用了 {{ debugSymbols.length }} 只标的，结论**不可用于评估策略**
      </template>
      截面选股被严重削弱，跑出的高收益没有意义。此结果仅用于验证流水线是否跑通。
    </el-alert>

    <!-- ★ 尖峰警告 -->
    <el-alert
      v-if="spikes.length"
      type="warning"
      :closable="false"
      show-icon
      class="alert-gap"
    >
      <template #title>检出 {{ spikes.length }} 个「尖峰」组合</template>
      这些组合自己收益不错，但相邻参数的邻居明显跟不上（邻居均值不足它的
      {{ (1 / spikeRatio).toFixed(2) }}）——
      典型的**噪声拟合**，样本外大概率失效。表里已标红，不要选它们。
    </el-alert>

    <!-- 结论 -->
    <div class="panel" v-if="detail">
      <div class="panel-title">
        <span>推荐组合</span>
        <span class="sub">
          选「高原」不选「尖峰」：按**邻域平均**排名，不是单点最高分
        </span>
      </div>

      <div class="reco-grid">
        <div class="reco-card plateau">
          <div class="reco-head">
            <el-icon><Sunny /></el-icon>
            推荐（高原）
          </div>
          <div class="reco-params mono">{{ fmtParams(plateauRow) }}</div>
          <div class="reco-metric">
            <span class="value" :class="pnlClass(numOf(plateauRow, objective))">
              {{ fmtPctSigned(numOf(plateauRow, objective)) }}
            </span>
            <span class="label">{{ objectiveLabel }}</span>
          </div>
          <div class="reco-sub">
            邻域平均
            <b :class="pnlClass(numOf(plateauRow, 'neighbor_mean'))">
              {{ fmtPctSigned(numOf(plateauRow, 'neighbor_mean')) }}
            </b>
          </div>
          <el-button
            v-if="runIdOf(plateauRow)"
            size="small"
            type="primary"
            plain
            class="reco-btn"
            @click="gotoRun(plateauRow)"
          >
            查看完整回测
          </el-button>
        </div>

        <div class="reco-card" :class="{ spike: isSpike(singleRow) }">
          <div class="reco-head">
            <el-icon><TrendCharts /></el-icon>
            单点最优（{{ isSpike(singleRow) ? '尖峰，不推荐' : '与高原一致' }}）
          </div>
          <div class="reco-params mono">{{ fmtParams(singleRow) }}</div>
          <div class="reco-metric">
            <span class="value" :class="pnlClass(numOf(singleRow, objective))">
              {{ fmtPctSigned(numOf(singleRow, objective)) }}
            </span>
            <span class="label">{{ objectiveLabel }}</span>
          </div>
          <div class="reco-sub">
            邻域平均
            <b :class="pnlClass(numOf(singleRow, 'neighbor_mean'))">
              {{ fmtPctSigned(numOf(singleRow, 'neighbor_mean')) }}
            </b>
          </div>
          <p class="reco-warn">
            单点最优**几乎必然过拟合**，仅作参考。
          </p>
        </div>
      </div>
    </div>

    <!-- 热力图 -->
    <div class="panel" v-if="detail && items.length">
      <div class="panel-title">
        <span>参数敏感性热力图</span>
        <span class="sub">
          横轴 {{ xAxis }}｜纵向分组 {{ seriesAxis || '—' }}｜颜色 = {{ objectiveLabel }}
        </span>
      </div>
      <EChart :option="heatOption" height="380px" />
      <p class="note">
        怎么看：<b>看颜色块连成一片（高原），不要看孤立的深色格（尖峰）</b>。
        相邻格颜色跳变越大，说明该参数越是在拟合噪声。带 <b>★</b> 的是推荐组合，
        带 <b>⚠</b> 的是尖峰组合。
      </p>
    </div>

    <!-- 敏感性曲线 -->
    <div class="panel" v-if="detail && seriesAxis && items.length">
      <div class="panel-title">
        <span>参数敏感性曲线</span>
        <span class="sub">每个 {{ seriesAxis }} 取值一条线 —— 线的<b>形状</b>比高度重要</span>
      </div>
      <EChart :option="lineOption" height="340px" />
      <p class="note">
        一条平缓上升的线是健康的；某个点突然冲高再掉下来，那个点就是噪声拟合出来的。
      </p>
    </div>

    <!-- 结果表 -->
    <div class="panel" v-if="detail && items.length">
      <div class="panel-title">
        <span>全网格结果（{{ items.length }} / {{ detail.combo_count }} 组）</span>
        <span class="sub">点列头排序；双击行跳回测详情</span>
      </div>
      <el-table
        :data="sortedItems"
        size="small"
        stripe
        max-height="560px"
        :row-class-name="rowClass"
        empty-text="没有结果"
        @row-dblclick="gotoRun"
        @sort-change="onSortChange"
      >
        <el-table-column
          v-for="name in detail.param_names"
          :key="name"
          :label="name"
          width="118"
          align="right"
        >
          <template #default="{ row }">
            <span class="mono param-val">{{ row[name] }}</span>
          </template>
        </el-table-column>

        <el-table-column label="标记" width="76" align="center">
          <template #default="{ row }">
            <el-tag v-if="isPlateau(row)" size="small" type="success" effect="dark">
              ★ 推荐
            </el-tag>
            <el-tag v-else-if="row.is_spike" size="small" type="danger" effect="dark">
              ⚠ 尖峰
            </el-tag>
            <span v-else class="text-muted">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="年化收益" prop="annual_return" width="104" align="right" sortable>
          <template #default="{ row }">
            <span :class="pnlClass(numOf(row, 'annual_return'))">
              {{ fmtPctSigned(numOf(row, 'annual_return')) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="总收益" prop="total_return" width="98" align="right" sortable>
          <template #default="{ row }">
            <span :class="pnlClass(numOf(row, 'total_return'))">
              {{ fmtPctSigned(numOf(row, 'total_return')) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="邻域平均" prop="neighbor_mean" width="100" align="right" sortable>
          <template #default="{ row }">
            <el-tooltip content="该参数点上下左右四个邻居的目标指标均值 —— 判高原的依据" placement="top">
              <span :class="pnlClass(numOf(row, 'neighbor_mean'))">
                {{ fmtPctSigned(numOf(row, 'neighbor_mean')) }}
              </span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="最大回撤" prop="max_drawdown" width="100" align="right" sortable>
          <template #default="{ row }">
            <span :class="pnlClass(numOf(row, 'max_drawdown'))">
              {{ fmtPct(numOf(row, 'max_drawdown')) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="夏普" prop="sharpe" width="80" align="right" sortable>
          <template #default="{ row }">{{ fmtNum(numOf(row, 'sharpe')) }}</template>
        </el-table-column>

        <el-table-column label="胜率" prop="win_rate" width="84" align="right" sortable>
          <template #default="{ row }">{{ fmtPct(numOf(row, 'win_rate')) }}</template>
        </el-table-column>

        <el-table-column label="年化成本" prop="annual_cost_ratio" width="96" align="right" sortable>
          <template #default="{ row }">
            <span class="cost-value">{{ fmtPct(numOf(row, 'annual_cost_ratio')) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="最大单笔亏损" prop="max_single_loss" width="112" align="right" sortable>
          <template #default="{ row }">
            <span :class="pnlClass(numOf(row, 'max_single_loss'))">
              {{ fmtPct(numOf(row, 'max_single_loss')) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="交易数" prop="trade_count" width="84" align="right" sortable>
          <template #default="{ row }">{{ numOf(row, 'trade_count') ?? DASH }}</template>
        </el-table-column>

        <el-table-column label="操作" width="80" fixed="right" align="center">
          <template #default="{ row }">
            <el-button
              link
              size="small"
              type="primary"
              :disabled="!row.run_id"
              @click="gotoRun(row)"
            >
              回测
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 自动生成的结论原文 -->
    <div class="panel" v-if="detail?.summary_md">
      <div class="panel-title">
        <span>CLI 生成的结论（summary.md）</span>
        <span class="sub">与上面的图是同一份数据的两种呈现</span>
      </div>
      <pre class="summary-md">{{ detail.summary_md }}</pre>
    </div>

    <el-empty v-if="!loading && !detail" description="没有找到这个寻优任务" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { getSweepDetail } from '@/api/sweep'
import type { SweepDetail, SweepResultItem } from '@/types/api'
import { DASH, fmtNum, fmtPct, fmtPctSigned, pnlClass } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const sweepId = computed(() => String(route.params.sweepId || ''))
const loading = ref(false)
const detail = ref<SweepDetail | null>(null)

const items = computed<SweepResultItem[]>(() => detail.value?.items ?? [])
const debugSymbols = computed(() => detail.value?.meta?.symbols ?? null)
const spikeRatio = computed(() => detail.value?.meta?.spike_ratio ?? 1.6)

const xAxis = computed(() => detail.value?.sensitivity_axes?.x ?? detail.value?.param_names?.[0] ?? '')
const seriesAxis = computed(() => {
  const s = detail.value?.sensitivity_axes?.series
  if (s) return s
  // 兜底：第二个参数名（正常情况 grid.json 已写好 sensitivity 配置）
  const names = detail.value?.param_names ?? []
  return names.find((n) => n !== xAxis.value) ?? ''
})

/**
 * 某个轴上的候选取值。
 *
 * ★ 优先取 `params_grid`；但**联合维度**展开后的参数名（如
 *   `take_profit_gross_main`）不在网格定义里（网格里的 key 是 `take_profit`，
 *   值是 dict 数组）→ 必须回退到"从结果行里去重取值"。
 *   否则 `params_grid[key]` 为 undefined → 热力图/折线**静默空白**
 *   （正是本项目最恨的"算了却没呈现"）。
 */
function axisValues(key: string): Array<string | number> {
  const d = detail.value
  if (!d || !key) return []
  const fromGrid = d.params_grid?.[key]
  if (Array.isArray(fromGrid) && fromGrid.length && fromGrid.every((v) => typeof v !== 'object')) {
    return fromGrid as Array<string | number>
  }
  const seen = new Set<string>()
  const out: Array<string | number> = []
  for (const it of items.value) {
    const v: unknown = it[key]
    if (typeof v !== 'string' && typeof v !== 'number') continue
    const s = String(v)
    if (seen.has(s)) continue
    seen.add(s)
    out.push(v)
  }
  out.sort((a, b) =>
    typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b)),
  )
  return out
}

const OBJECTIVE_LABEL: Record<string, string> = {
  annual_return: '年化收益',
  total_return: '总收益',
  sharpe: '夏普',
  calmar: '卡玛',
}
const objectiveLabel = computed(
  () => OBJECTIVE_LABEL[detail.value?.objective ?? ''] ?? detail.value?.objective ?? '',
)
const objective = computed(() => detail.value?.objective ?? 'annual_return')

const intervalText = computed(() => {
  const it = detail.value?.interval
  return it ? `${it.start} ~ ${it.end}` : DASH
})

const spikes = computed(() => items.value.filter((r) => r.is_spike))

// ---------- 取值助手 ----------
/**
 * `SweepResultItem` 的索引签名是 `unknown`（参数名动态），
 * 指标字段取值统一走这里做收口 —— 模板里到处写 `as number` 太脏。
 *
 * ⚠️ 参数类型必须是 `unknown` 而不是 `SweepResultItem`：
 *    el-table 的插槽作用域 `{ row }` 推断出来是它自己的 `DefaultRow`
 *    （只有索引签名），**不是**我们声明的行类型。写成 `SweepResultItem`
 *    会让 vue-tsc 在每一个 `#default="{ row }"` 上报 TS2345，
 *    几十处一起炸。用 `unknown` 收口 + 内部断言，接口边界干净且类型检查能过。
 */
function asRow(row: unknown): SweepResultItem | null {
  return row && typeof row === 'object' ? (row as SweepResultItem) : null
}

function numOf(row: unknown, key: string): number | null {
  const r = asRow(row)
  if (!r) return null
  const v = r[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function runIdOf(row: unknown): string | null {
  return asRow(row)?.run_id ?? null
}

function isSpike(row: unknown): boolean {
  return Boolean(asRow(row)?.is_spike)
}

/** 按「邻域均值优先」挑高原 —— 与后端 `pick_plateau` 同一套排序 */
const plateauRow = computed<SweepResultItem | null>(() => {
  const ok = items.value.filter((r) => numOf(r, objective.value) !== null)
  if (!ok.length) return null
  return [...ok].sort((a, b) => {
    const an = numOf(a, 'neighbor_mean') ?? -Infinity
    const bn = numOf(b, 'neighbor_mean') ?? -Infinity
    if (bn !== an) return bn - an
    return (numOf(b, objective.value) ?? -Infinity) - (numOf(a, objective.value) ?? -Infinity)
  })[0]
})

const singleRow = computed<SweepResultItem | null>(() => {
  const ok = items.value.filter((r) => numOf(r, objective.value) !== null)
  if (!ok.length) return null
  return [...ok].sort(
    (a, b) => (numOf(b, objective.value) ?? -Infinity) - (numOf(a, objective.value) ?? -Infinity),
  )[0]
})

function isPlateau(row: unknown): boolean {
  const p = plateauRow.value
  const r = asRow(row)
  if (!p || !r) return false
  return r.combo_index === p.combo_index
}

/** 参数名 → `hold_days_max=20  take_profit_gross_main=0.06`（读法比 JSON 自然） */
function fmtParams(row: unknown): string {
  const r = asRow(row)
  if (!r) return DASH
  const names = detail.value?.param_names ?? []
  return names.map((n) => `${n}=${String(r[n])}`).join('  ')
}

// ---------- 排序 ----------
/**
 * 用 el-table 自己的 `sort-change` + `sortedItems` 排序。
 *
 * ⚠️ 不要同时用 `sortable` 和手写 `:sort-method`：两者会各自排一次，
 *    表现是"点了列头，顺序变了但不是你要的那个顺序"。
 */
const sortState = ref<{ prop: string; order: 'ascending' | 'descending' | null }>({
  prop: '',
  order: null,
})

function onSortChange(e: { prop: string | null; order: 'ascending' | 'descending' | null }) {
  // `prop` 可能是 null（el-table 在无排序字段时会传 null）→ 归一成空串
  sortState.value = { prop: e.prop || '', order: e.order }
}

function cmpBy(key: string) {
  return (a: SweepResultItem, b: SweepResultItem) => {
    const av = numOf(a, key)
    const bv = numOf(b, key)
    // null 一律沉底（不管升序降序）—— "没有值"不该排在有值前面
    if (av === null && bv === null) return 0
    if (av === null) return 1
    if (bv === null) return -1
    return av - bv
  }
}

const sortedItems = computed(() => {
  const { prop, order } = sortState.value
  if (!prop || !order) return items.value
  const base = [...items.value].sort(cmpBy(prop))
  if (order === 'ascending') return base
  // 降序时把 null 组重新挪回末尾（reverse 会把它们翻到最前）
  const hasVal = base.filter((r) => numOf(r, prop) !== null).reverse()
  const noVal = base.filter((r) => numOf(r, prop) === null)
  return [...hasVal, ...noVal]
})

function rowClass({ row }: { row: unknown }): string {
  if (isPlateau(row)) return 'row-plateau'
  if (isSpike(row)) return 'row-spike'
  return ''
}

// ---------- 热力图 ----------
const heatOption = computed<EChartsOption>(() => {
  const d = detail.value
  if (!d || !items.value.length) return {}
  const xs = axisValues(xAxis.value)
  const srs = seriesAxis.value ? axisValues(seriesAxis.value) : []
  if (!xs.length) return {}

  const xIdx = new Map(xs.map((v, i) => [String(v), i]))
  const sIdx = new Map(srs.map((v, i) => [String(v), i]))

  type Cell = [number, number, number]
  const data: Cell[] = []
  let vmin = Infinity
  let vmax = -Infinity
  const marks: Array<{ value: [number, number]; label: string }> = []

  for (const it of items.value) {
    const v = numOf(it, objective.value)
    if (v === null) continue
    const x = xIdx.get(String(it[xAxis.value]))
    if (x === undefined) continue
    const y = seriesAxis.value ? sIdx.get(String(it[seriesAxis.value])) : 0
    if (y === undefined) continue
    data.push([x, y, v])
    vmin = Math.min(vmin, v)
    vmax = Math.max(vmax, v)
    if (isPlateau(it)) marks.push({ value: [x, y], label: '★' })
    else if (it.is_spike) marks.push({ value: [x, y], label: '⚠' })
  }
  if (!data.length) return {}

  // 全为正/全为负时 visualMap 会把 0 拉进范围 —— 用 0 参与边界，
  // 让「亏损格」与「盈利格」在颜色上真正分开（涨红跌绿）。
  const lo = Math.min(vmin, 0)
  const hi = Math.max(vmax, 0)

  const yLabels = seriesAxis.value ? srs.map((v) => String(v)) : [objectiveLabel.value]

  return {
    tooltip: {
      position: 'top',
      formatter: (p: unknown) => {
        const item = p as { value: [number, number, number] }
        const [x, y, v] = item.value
        const xl = String(xs[x])
        const yl = seriesAxis.value ? `${seriesAxis.value}=${yLabels[y]}` : ''
        const hit = items.value.find(
          (it) =>
            String(it[xAxis.value]) === xl &&
            (!seriesAxis.value || String(it[seriesAxis.value]) === yLabels[y]),
        )
        const tag = hit?.is_spike ? ' ⚠ 尖峰' : isPlateau(hit) ? ' ★ 推荐' : ''
        const nm = hit ? fmtPctSigned(numOf(hit, 'neighbor_mean')) : DASH
        return (
          `${xAxis.value}=${xl}${yl ? '｜' + yl : ''}${tag}<br/>` +
          `${objectiveLabel.value}：<b>${fmtPctSigned(v)}</b><br/>` +
          `邻域平均：${nm}`
        )
      },
    },
    grid: { left: 74, right: 96, top: 22, bottom: 46 },
    xAxis: {
      type: 'category',
      data: xs.map((v) => String(v)),
      name: xAxis.value,
      nameLocation: 'middle',
      nameGap: 28,
      splitArea: { show: true },
      axisLabel: { fontSize: 12 },
    },
    yAxis: {
      type: 'category',
      data: yLabels,
      name: seriesAxis.value || '',
      nameLocation: 'middle',
      nameGap: seriesAxis.value ? 58 : 70,
      splitArea: { show: true },
      axisLabel: { fontSize: 12 },
    },
    visualMap: {
      min: lo,
      max: hi,
      calculable: true,
      orient: 'vertical',
      right: 6,
      top: 'middle',
      itemWidth: 12,
      itemHeight: 110,
      text: ['盈利', '亏损'],
      // ★ 涨红跌绿（中国市场惯例）
      inRange: { color: ['#18a058', '#e8f5ee', '#fdeaea', '#d03050'] },
      // ⚠️ 参数必须收成 `unknown` 再断言：ECharts 的 `LabelFormatter` 签名是
      //    `(value: OptionDataValue, ...)`，而 `OptionDataValue` 含 `undefined`。
      //    直接写 `(v: number) => ...` 会 TS2769（number 比 OptionDataValue 窄）。
      formatter: (v: unknown) => `${(Number(v) * 100).toFixed(1)}%`,
    },
    series: [
      {
        name: objectiveLabel.value,
        type: 'heatmap',
        data,
        label: {
          show: true,
          fontSize: 11,
          formatter: (p: unknown) => {
            const item = p as { value: [number, number, number] }
            const mark = marks.find((m) => m.value[0] === item.value[0] && m.value[1] === item.value[1])
            const pct = `${(item.value[2] * 100).toFixed(1)}%`
            return mark ? `${mark.label}\n${pct}` : pct
          },
        },
        emphasis: {
          itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,0.25)' },
        },
      },
    ],
  }
})

// ---------- 敏感性曲线 ----------
const lineOption = computed<EChartsOption>(() => {
  const d = detail.value
  if (!d || !seriesAxis.value) return {}
  const xs = axisValues(xAxis.value)
  const srs = axisValues(seriesAxis.value)
  if (!xs.length || !srs.length) return {}

  const colorOf = (i: number) =>
    ['#185fa5', '#d97706', '#7c4dff', '#0f9b8e', '#c2185b'][i % 5]

  const series = srs.map((sv, si) => ({
    name: `${seriesAxis.value}=${sv}`,
    type: 'line' as const,
    smooth: false,
    symbol: 'circle',
    symbolSize: 7,
    lineStyle: { width: 2, color: colorOf(si) },
    itemStyle: { color: colorOf(si) },
    data: xs.map((xv) => {
      const hit = items.value.find(
        (it) => String(it[xAxis.value]) === String(xv) && String(it[seriesAxis.value]) === String(sv),
      )
      const v = numOf(hit, objective.value)
      return v === null ? null : Number((v * 100).toFixed(2))
    }),
  }))

  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: unknown) => (v === null || v === undefined ? DASH : `${v}%`),
    },
    legend: { top: 0, type: 'scroll' },
    grid: { left: 62, right: 26, top: 40, bottom: 46 },
    xAxis: {
      type: 'category',
      data: xs.map((v) => String(v)),
      name: xAxis.value,
      nameLocation: 'middle',
      nameGap: 28,
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      name: `${objectiveLabel.value} %`,
      scale: true,
      axisLabel: { fontSize: 11, formatter: (v: number) => `${v}%` },
      splitLine: { lineStyle: { color: '#eef0f3' } },
    },
    series,
  }
})

// ---------- 跳转 ----------
function gotoRun(row: unknown) {
  const r = row as SweepResultItem
  if (!r?.run_id || !detail.value) return
  void router.push({
    name: 'backtest-detail',
    params: { strategy: detail.value.strategy, runId: r.run_id },
  })
}

function goBack() {
  void router.push({ name: 'sweep' })
}

async function load() {
  loading.value = true
  try {
    detail.value = await getSweepDetail(sweepId.value)
  } catch {
    detail.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page-header h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 18px;
}

.sweep-id {
  font-weight: 400;
  color: #8a8f99;
}

.alert-gap {
  margin-bottom: 14px;
}

.note {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: #8a8f99;
  line-height: 1.6;
}

/* ---------- 推荐卡 ---------- */
.reco-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.reco-card {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 14px 16px;
  background: #fafbfc;
}

.reco-card.plateau {
  border-color: #b7dfc6;
  background: #f2fbf5;
}

.reco-card.spike {
  border-color: #f0bcbc;
  background: #fdf6f6;
}

.reco-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  color: #4a4f57;
  margin-bottom: 8px;
}

.reco-params {
  font-size: 13px;
  color: #1f2329;
  word-break: break-all;
  margin-bottom: 10px;
}

.reco-metric {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.reco-metric .value {
  font-size: 22px;
  font-weight: 500;
}

.reco-metric .label {
  font-size: 12.5px;
  color: #8a8f99;
}

.reco-sub {
  font-size: 12.5px;
  color: #8a8f99;
  margin-top: 6px;
}

.reco-btn {
  margin-top: 12px;
}

.reco-warn {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--color-warn);
}

/* ---------- 表格 ---------- */
.cost-value {
  color: var(--color-warn);
  font-weight: 500;
}

.param-val {
  color: #1f2329;
}

:deep(.row-spike) {
  background: #fdf6f6 !important;
}

:deep(.row-plateau) {
  background: #f2fbf5 !important;
}

.summary-md {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  background: #f7f8fa;
  border: 0.5px solid #e4e7ed;
  border-radius: 6px;
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
