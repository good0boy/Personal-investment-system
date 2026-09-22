<!--
  模块 2 · 因子库 —— 因子详情页

  五个页签对应因子研究的四件事：
    概览      —— 口径（单位/方向假设/参数/实现路径）与落盘状态
    时序      —— 截面分布随时间怎么移动（默认全市场聚合，不是单只股票）
    截面分布  —— 某一天的分布形态（直方图 + 分位 + 极值标的）
    分层回测  —— ★ 判断因子有没有 alpha 的核心图
    IC 分析   —— ★ 因子有效性的标准评估（RankIC 序列 + ICIR）

  数据全部只读：产物 + analysis/{name}.json。分析没跑过时，后两个页签
  给出**明确的命令**而不是空图 —— 空图会让人以为"因子没用"。

  ⚠️ 一个必须写在页面上的提醒：YAML 里的 direction 是**先验假设**，
      IC / 分层才是**证据**。本项目的头号教训就是"看着合理的假设被实测推翻"。
-->
<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <el-button link size="small" @click="goBack">
        <el-icon><ArrowLeft /></el-icon>
        返回因子列表
      </el-button>
      <h2>
        {{ detail?.cname || name }}
        <span class="mono factor-id">{{ name }}</span>
        <el-tag v-if="detail" size="small" effect="plain">{{ catName }}</el-tag>
        <el-tag v-if="detail?.debug_symbols" size="small" type="danger" effect="dark">
          调试 {{ detail.debug_symbols.length }} 只
        </el-tag>
      </h2>
      <p v-if="detail">{{ detail.desc }}</p>
    </div>

    <!-- ⚠️ 调试运行 / 状态异常：产物结构与全市场一模一样，不打标就会被当真 -->
    <el-alert
      v-for="w in detail?.warnings || []"
      :key="w"
      :type="w.includes('调试') ? 'error' : 'warning'"
      :closable="false"
      show-icon
      class="alert-gap"
    >
      {{ w }}
    </el-alert>

    <el-tabs v-model="tab" class="factor-tabs">
      <!-- ============ 概览 ============ -->
      <el-tab-pane label="概览" name="overview">
        <div class="panel">
          <p class="panel-title">口径与状态</p>
          <div class="metric-grid">
            <div class="metric">
              <p class="label">方向假设（先验）</p>
              <p class="value" :class="dirClass(detail?.direction ?? 0)">
                {{ dirLabel }}
              </p>
              <p class="hint">⚠️ 这是假设不是结论，真假看 IC 与分层</p>
            </div>
            <div class="metric">
              <p class="label">单位</p>
              <p class="value small">{{ detail?.unit || DASH }}</p>
              <p class="hint">定义在 registry.yaml，前端不硬编码</p>
            </div>
            <div class="metric">
              <p class="label">覆盖区间</p>
              <p class="value small mono">
                {{ detail?.date_start || DASH }} ~ {{ detail?.date_end || DASH }}
              </p>
              <p class="hint">{{ fmtNum(detail?.symbols, 0) }} 只标的</p>
            </div>
            <div class="metric">
              <p class="label">产物</p>
              <p class="value small">
                {{ diskText }}
              </p>
              <p class="hint">{{ detail?.file ? shortPath : DASH }}</p>
            </div>
          </div>

          <el-descriptions :column="2" border size="small" class="desc-block">
            <el-descriptions-item label="因子名">
              <span class="mono">{{ detail?.name }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="分类">{{ catName }}</el-descriptions-item>
            <el-descriptions-item label="实现">
              <span class="mono">{{ detail?.impl }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="参数">
              <span class="mono">{{ paramsText }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="计算耗时">
              {{ detail?.seconds != null ? `${fmtNum(detail.seconds, 2)} s` : DASH }}
            </el-descriptions-item>
            <el-descriptions-item label="非空占比">
              {{ detail?.nan_ratio != null ? fmtPct(1 - detail.nan_ratio, 1) : DASH }}
            </el-descriptions-item>
            <el-descriptions-item label="标签">
              <el-tag
                v-for="t in detail?.tags || []"
                :key="t"
                size="small"
                effect="plain"
                class="tag-gap"
              >
                {{ t }}
              </el-tag>
              <span v-if="!detail?.tags?.length">{{ DASH }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="计算时间">
              {{ fmtDateTime(detail?.computed_at) }}
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <!-- 分析摘要 -->
        <div v-if="detail?.has_analysis" class="panel">
          <p class="panel-title">
            有效性摘要
            <span class="sub">
              区间 {{ summaryInterval }}｜{{ analysisSettings.groups }} 组｜持有
              {{ analysisSettings.horizon }} 个交易日
            </span>
          </p>
          <div class="metric-grid">
            <div class="metric">
              <p class="label">IC 均值</p>
              <p class="value" :class="pnlClass(ic.mean)">
                {{ fmtNum(ic.mean, 4) }}
              </p>
              <p class="hint">RankIC，< 0.02 基本等于没有选股能力</p>
            </div>
            <div class="metric">
              <p class="label">ICIR</p>
              <p class="value" :class="pnlClass(ic.ir)">{{ fmtNum(ic.ir, 3) }}</p>
              <p class="hint">单期口径（非年化）｜t = {{ fmtNum(ic.t_stat, 2) }}</p>
            </div>
            <div class="metric">
              <p class="label">IC 胜率</p>
              <p class="value">{{ fmtPct(ic.win_rate, 1) }}</p>
              <p class="hint">IC > 0 的交易日占比</p>
            </div>
            <div class="metric">
              <p class="label">分组单调性</p>
              <p class="value" :class="pnlClass(monotonicity)">
                {{ fmtNum(monotonicity, 2) }}
              </p>
              <p class="hint">组号 vs 累计收益的秩相关，接近 ±1 才算有序</p>
            </div>
            <div class="metric">
              <p class="label">多空年化（毛）</p>
              <p class="value" :class="pnlClass(ls.annual_gross)">
                {{ fmtPctSigned(ls.annual_gross, 1) }}
              </p>
              <p class="hint">日均换手 {{ fmtPct(ls.turnover_daily, 1) }}</p>
            </div>
            <div class="metric">
              <p class="label">多空年化（净）</p>
              <p class="value" :class="pnlClass(ls.annual_net)">
                {{ fmtPctSigned(ls.annual_net, 1) }}
              </p>
              <p class="hint">年化成本 {{ fmtPct(ls.cost_annual, 1) }}</p>
            </div>
          </div>
          <p class="foot-note">
            分组表与曲线见「分层回测」页签；IC 序列见「IC 分析」页签。
            口径说明（成交时点 / 股票池 / 成本）在分层页签底部，**数字脱离口径没法解读**。
          </p>
        </div>

        <el-alert v-else type="info" :closable="false" show-icon class="alert-gap">
          <template #title>还没有做有效性分析</template>
          因子值已经算好，但"这个因子有没有用"要看分层回测与 IC。
          在 backend 下执行：<span class="mono">python -m factors.analyze --name {{ name }}</span>
        </el-alert>
      </el-tab-pane>

      <!-- ============ 时序 ============ -->
      <el-tab-pane label="时序" name="series">
        <div class="panel">
          <p class="panel-title">
            因子走势
            <span class="sub">{{ seriesSub }}</span>
          </p>
          <div class="ctrl-bar">
            <el-radio-group v-model="seriesMode" size="small" @change="loadSeries">
              <el-radio-button label="aggregate">全市场截面聚合</el-radio-button>
              <el-radio-button label="symbols">多标的对比</el-radio-button>
            </el-radio-group>
            <el-select
              v-if="seriesMode === 'symbols'"
              v-model="pickedSymbols"
              multiple
              filterable
              remote
              reserve-keyword
              size="small"
              placeholder="输入代码搜索，如 600519 / sz.000"
              :remote-method="doSearch"
              :loading="searching"
              class="sym-select"
              @change="loadSeries"
            >
              <el-option v-for="s in symOptions" :key="s" :label="s" :value="s" />
            </el-select>
            <span class="text-small text-muted">单位：{{ unitText }}</span>
          </div>
          <EChart :option="seriesOption" height="360px" />
          <p class="foot-note">
            <template v-if="seriesMode === 'aggregate'">
              默认展示<strong>全市场每日截面聚合</strong>（均值 / 中位数 / 25~75 分位）。
              单只股票的因子折线看着有信息量、实际没有 —— 它只是价格走势的影子。
            </template>
            <template v-else>
              成交价口径与回测一致（后复权），因此因子值的水平可以直接对比。
            </template>
          </p>
        </div>
      </el-tab-pane>

      <!-- ============ 截面分布 ============ -->
      <el-tab-pane label="截面分布" name="cross">
        <div class="panel">
          <p class="panel-title">
            某交易日的截面分布
            <span class="sub">样本 {{ fmtNum(cs?.count, 0) }} 只</span>
          </p>
          <div class="ctrl-bar">
            <el-date-picker
              v-model="crossDay"
              type="date"
              size="small"
              value-format="YYYY-MM-DD"
              placeholder="选择交易日"
              :disabled-date="disableFuture"
              @change="loadCross"
            />
            <el-input-number v-model="crossBins" size="small" :min="5" :max="200" :step="5" />
            <span class="text-small text-muted">分箱数</span>
            <el-button size="small" @click="loadCross">查询</el-button>
          </div>
          <EChart :option="histOption" height="300px" />
          <p v-if="cs" class="foot-note">{{ cs.histogram.clip_note }}</p>
        </div>

        <div class="two-col" v-if="cs">
          <div class="panel">
            <p class="panel-title">分位数</p>
            <el-table :data="quantileRows" size="small" stripe>
              <el-table-column prop="label" label="分位" width="90" />
              <el-table-column label="因子值" align="right">
                <template #default="{ row }">
                  <span class="mono">{{ asQuant(row).valueText }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="stat-line">
              均值 <b class="mono">{{ fmtNum(cs.stats.mean, 4) }}</b>｜
              标准差 <b class="mono">{{ fmtNum(cs.stats.std, 4) }}</b>｜
              中位数 <b class="mono">{{ fmtNum(cs.stats.median, 4) }}</b>
            </div>
          </div>

          <div class="panel">
            <p class="panel-title">两端标的</p>
            <el-tabs v-model="extremeTab" class="mini-tabs">
              <el-tab-pane :label="`最高 ${cs.extremes.top.length} 只`" name="top">
                <el-table :data="cs.extremes.top" size="small" stripe max-height="260">
                  <el-table-column prop="symbol" label="代码" class-name="mono" />
                  <el-table-column label="因子值" align="right">
                    <template #default="{ row }">
                      <span class="mono">{{ fmtNum(asExt(row).value, 4) }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </el-tab-pane>
              <el-tab-pane :label="`最低 ${cs.extremes.bottom.length} 只`" name="bottom">
                <el-table :data="cs.extremes.bottom" size="small" stripe max-height="260">
                  <el-table-column prop="symbol" label="代码" class-name="mono" />
                  <el-table-column label="因子值" align="right">
                    <template #default="{ row }">
                      <span class="mono">{{ fmtNum(asExt(row).value, 4) }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </el-tab-pane>
            </el-tabs>
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ 分层回测 ============ -->
      <el-tab-pane label="分层回测" name="layered">
        <template v-if="layered">
          <div class="panel">
            <p class="panel-title">
              分组累计净值
              <span class="sub">
                区间 {{ layered.interval.start }} ~ {{ layered.interval.end }}
                （{{ fmtNum(layered.interval.dates_used, 0) }} 个交易日）
              </span>
            </p>
            <EChart :option="layeredOption" height="380px" />
            <p class="foot-note">
              组号越大 = 因子值越大。曲线是否<strong>有层次</strong>比极值更重要 ——
              只看"最高组 vs 最低组"的差额会被一两个极端组骗。
            </p>
          </div>

          <div class="panel">
            <p class="panel-title">
              分组明细
              <span class="sub">毛收益不含成本；成本按首尾两组的实际换手估算</span>
            </p>
            <el-table :data="layered.group_summary" size="small" stripe>
              <el-table-column prop="group" label="组" width="56" align="center" />
              <el-table-column label="日均收益" align="right">
                <template #default="{ row }">
                  <span class="mono" :class="pnlClass(asGroup(row).avg_daily)">
                    {{ fmtPctSigned(asGroup(row).avg_daily, 3) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="算术年化" align="right">
                <template #default="{ row }">
                  <span class="mono" :class="pnlClass(asGroup(row).annual_return)">
                    {{ fmtPctSigned(asGroup(row).annual_return, 1) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="累计收益" align="right">
                <template #default="{ row }">
                  <span class="mono" :class="pnlClass(asGroup(row).cum_return)">
                    {{ fmtPctSigned(asGroup(row).cum_return, 1) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="日均换手" align="right">
                <template #default="{ row }">
                  <span class="mono">{{ fmtPct(asGroup(row).turnover_daily, 1) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="年化成本" align="right">
                <template #default="{ row }">
                  <span class="mono text-warn-strong">{{ fmtPct(asGroup(row).cost_annual, 1) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="扣成本年化" align="right">
                <template #default="{ row }">
                  <span class="mono" :class="pnlClass(asGroup(row).annual_return_net)">
                    {{ fmtPctSigned(asGroup(row).annual_return_net, 1) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="平均只数" align="right">
                <template #default="{ row }">
                  <span class="mono text-muted">{{ fmtNum(asGroup(row).avg_size, 0) }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="stat-line">
              单调性（组号 vs 累计收益秩相关）
              <b class="mono" :class="pnlClass(layered.monotonicity)">
                {{ fmtNum(layered.monotonicity, 2) }}
              </b>
              ｜多空（首尾两组）年化
              <b class="mono" :class="pnlClass(layered.long_short.annual_gross)">
                {{ fmtPctSigned(layered.long_short.annual_gross, 1) }}
              </b>
              →扣成本
              <b class="mono" :class="pnlClass(layered.long_short.annual_net)">
                {{ fmtPctSigned(layered.long_short.annual_net, 1) }}
              </b>
              ｜平均股票池 {{ fmtNum(layered.settings.avg_universe_size, 0) }} 只/日
            </div>
          </div>

          <div class="panel">
            <p class="panel-title">分析口径（必读）</p>
            <ul class="note-list">
              <li v-for="(n, i) in layered.notes" :key="i">{{ n }}</li>
            </ul>
          </div>
        </template>
        <el-alert v-else-if="!loading" type="info" :closable="false" show-icon>
          <template #title>还没有分层回测结果</template>
          在 backend 下执行：<span class="mono">python -m factors.analyze --name {{ name }}</span>
        </el-alert>
      </el-tab-pane>

      <!-- ============ IC ============ -->
      <el-tab-pane label="IC 分析" name="ic">
        <template v-if="icResp">
          <div class="panel">
            <p class="panel-title">
              RankIC 时序
              <span class="sub">
                {{ icResp.interval.start }} ~ {{ icResp.interval.end }}
                （{{ fmtNum(icResp.summary.n_dates, 0) }} 个交易日）
              </span>
            </p>
            <EChart :option="icOption" height="320px" />
            <div class="metric-grid ic-metrics">
              <div class="metric">
                <p class="label">IC 均值</p>
                <p class="value" :class="pnlClass(icResp.summary.mean)">
                  {{ fmtNum(icResp.summary.mean, 4) }}
                </p>
              </div>
              <div class="metric">
                <p class="label">IC 标准差</p>
                <p class="value">{{ fmtNum(icResp.summary.std, 4) }}</p>
              </div>
              <div class="metric">
                <p class="label">ICIR</p>
                <p class="value" :class="pnlClass(icResp.summary.ir)">
                  {{ fmtNum(icResp.summary.ir, 3) }}
                </p>
                <p class="hint">= 均值 / 标准差</p>
              </div>
              <div class="metric">
                <p class="label">t 值</p>
                <p class="value" :class="pnlClass(icResp.summary.t_stat)">
                  {{ fmtNum(icResp.summary.t_stat, 2) }}
                </p>
                <p class="hint">|t| > 2 才算统计上显著</p>
              </div>
              <div class="metric">
                <p class="label">IC 胜率</p>
                <p class="value">{{ fmtPct(icResp.summary.win_rate, 1) }}</p>
              </div>
              <div class="metric">
                <p class="label">|IC| 均值</p>
                <p class="value">{{ fmtNum(icResp.summary.abs_mean, 4) }}</p>
              </div>
            </div>
          </div>
          <div class="panel">
            <p class="panel-title">分析口径（必读）</p>
            <ul class="note-list">
              <li v-for="(n, i) in icResp.notes" :key="i">{{ n }}</li>
            </ul>
          </div>
        </template>
        <el-alert v-else-if="!loading" type="info" :closable="false" show-icon>
          <template #title>还没有 IC 分析结果</template>
          在 backend 下执行：<span class="mono">python -m factors.analyze --name {{ name }}</span>
        </el-alert>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import {
  getFactorCrossSection,
  getFactorDetail,
  getFactorIc,
  getFactorLayered,
  getFactorSeries,
  searchFactorSymbols,
} from '@/api/factors'
import type {
  FactorCrossSectionResp,
  FactorExtreme,
  FactorGroupRow,
  FactorIcResp,
  FactorItem,
  FactorLayeredResp,
  FactorSeriesResp,
} from '@/types/api'
import { DASH, fmtDateTime, fmtNum, fmtPct, fmtPctSigned, pnlClass } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const name = computed(() => String(route.params.name || ''))
const loading = ref(false)
const tab = ref('overview')

const detail = ref<FactorItem | null>(null)
const seriesResp = ref<FactorSeriesResp | null>(null)
const cs = ref<FactorCrossSectionResp | null>(null)
const layered = ref<FactorLayeredResp | null>(null)
const icResp = ref<FactorIcResp | null>(null)

const seriesMode = ref<'aggregate' | 'symbols'>('aggregate')
const pickedSymbols = ref<string[]>([])
const symOptions = ref<string[]>([])
const searching = ref(false)

const crossDay = ref('')
const crossBins = ref(40)
const extremeTab = ref('top')

/* ---------------- 分组配色：低组浅、高组深，多空单独一色 ---------------- */
const GROUP_COLORS = ['#a8c8ee', '#79a7dd', '#4c86cf', '#2f68b8', '#1b4a90', '#123567']
const LS_COLOR = '#d97706'

/* ---------------- 概览 ---------------- */
const catName = computed(() => detail.value?.category ?? DASH)
const shortPath = computed(() => {
  const f = detail.value?.file ?? ''
  return f ? f.replace(/^.*\/data\//, 'data/') : DASH
})
const diskText = computed(() => {
  const d = detail.value
  if (!d?.rows) return '未计算'
  return `${fmtNum(d.rows, 0)} 行 / ${fmtBytesOf(d.size_bytes)}`
})
const paramsText = computed(() => {
  const p = detail.value?.params ?? {}
  const keys = Object.keys(p)
  if (!keys.length) return '（无参数）'
  return keys.map((k) => `${k}=${String(p[k])}`).join(', ')
})
const dirLabel = computed(() => {
  const d = detail.value?.direction ?? 0
  if (d > 0) return '越大越好 ↑'
  if (d < 0) return '越小越好 ↓'
  return '中性 —'
})

/** 方向着色的类名（走全局 text-up / text-down，别在这里另写颜色） */
function dirClass(direction: number | null | undefined) {
  const d = direction ?? 0
  if (d > 0) return 'text-up'
  if (d < 0) return 'text-down'
  return 'text-muted'
}
const unitText = computed(() => detail.value?.unit || '未标注')

const analysisSettings = computed(() => ({
  groups: layered.value?.settings?.groups ?? detail.value?.analysis_summary?.settings?.groups ?? DASH,
  horizon:
    layered.value?.settings?.horizon ?? detail.value?.analysis_summary?.settings?.horizon ?? DASH,
}))
const summaryInterval = computed(() => {
  const it = detail.value?.analysis_summary?.interval as
    | { start?: string; end?: string }
    | undefined
  return it?.start ? `${it.start} ~ ${it.end}` : DASH
})
const ic = computed(() => detail.value?.analysis_summary?.ic ?? {})
const ls = computed(() => detail.value?.analysis_summary?.long_short ?? {})
const monotonicity = computed(() => detail.value?.analysis_summary?.monotonicity ?? null)

const quantileRows = computed(() => {
  const q = cs.value?.quantiles ?? {}
  return Object.keys(q).map((k) => ({ label: k.toUpperCase(), valueText: fmtNum(q[k], 4) }))
})

function fmtBytesOf(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return DASH
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(1)} ${units[i]}`
}

/* ---------------- 时序 ---------------- */
const seriesSub = computed(() => {
  const r = seriesResp.value
  if (!r) return ''
  const base = `${r.dates[0] ?? DASH} ~ ${r.dates[r.dates.length - 1] ?? DASH}`
  return `${base}｜${r.dates.length} 点（原始 ${r.downsampled_from}，已服务端降采样）`
})

const seriesOption = computed<EChartsOption>(() => {
  const r = seriesResp.value
  if (!r) return { series: [] }
  const keys = Object.keys(r.series)
  return {
    grid: { left: 60, right: 28, top: 36, bottom: 62 },
    tooltip: { trigger: 'axis' },
    legend: { top: 4, type: 'scroll', textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: r.dates, boundaryGap: false, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', height: 20, bottom: 18 },
    ],
    series: keys.map((k, i) => ({
      name: k === 'mean' ? '全市场均值' : k === 'median' ? '中位数' : k,
      type: 'line' as const,
      data: r.series[k],
      symbol: 'none',
      smooth: false,
      lineStyle: {
        width: r.mode === 'cross_section_aggregate' && k !== 'mean' ? 1 : 1.4,
        type:
          r.mode === 'cross_section_aggregate' && (k === 'q25' || k === 'q75')
            ? ('dashed' as const)
            : ('solid' as const),
      },
      itemStyle: { color: GROUP_COLORS[i % GROUP_COLORS.length] },
      connectNulls: false,
    })),
  }
})

async function loadSeries() {
  if (!name.value) return
  try {
    seriesResp.value = await getFactorSeries(name.value, {
      symbols: seriesMode.value === 'symbols' ? pickedSymbols.value.join(',') : undefined,
    })
  } catch {
    seriesResp.value = null
  }
}

async function doSearch(q: string) {
  if (!q) return
  searching.value = true
  try {
    symOptions.value = await searchFactorSymbols(q, 50)
  } finally {
    searching.value = false
  }
}

/* ---------------- 截面 ---------------- */
const histOption = computed<EChartsOption>(() => {
  const c = cs.value
  if (!c) return { series: [] }
  const bins = c.histogram.bins
  const centers = bins.slice(0, -1).map((b, i) => ((b + bins[i + 1]) / 2).toFixed(4))
  return {
    grid: { left: 60, right: 24, top: 24, bottom: 52 },
    tooltip: {
      trigger: 'axis',
      formatter: (p: unknown) => {
        const arr = p as Array<{ name: string; value: number }>
        return arr.map((x) => `因子值 ≈ ${x.name}：${x.value} 只`).join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      data: centers,
      name: `因子值（${c.unit || '无量纲'}）`,
      nameLocation: 'middle',
      nameGap: 30,
      axisLabel: { fontSize: 10, rotate: 40 },
    },
    yAxis: { type: 'value', name: '只数', axisLabel: { fontSize: 10 } },
    series: [
      {
        type: 'bar' as const,
        data: c.histogram.counts,
        itemStyle: { color: '#4c86cf' },
        barCategoryGap: '6%',
      },
    ],
  }
})

async function loadCross() {
  if (!name.value) return
  try {
    cs.value = await getFactorCrossSection(name.value, {
      day: crossDay.value || undefined,
      bins: crossBins.value,
    })
    if (!crossDay.value && cs.value?.day) crossDay.value = cs.value.day
  } catch {
    cs.value = null
  }
}

function disableFuture(d: Date) {
  return d.getTime() > Date.now()
}

/* ---------------- 分层 ---------------- */
const layeredOption = computed<EChartsOption>(() => {
  const l = layered.value
  if (!l || !l.dates.length) return { series: [] }
  const cols = l.nav_columns
  // 每行是 [G1..Gn, LS]
  const byCol = cols.map((_, ci) => l.nav.map((row) => row[ci]))
  return {
    grid: { left: 62, right: 28, top: 40, bottom: 62 },
    tooltip: { trigger: 'axis' },
    legend: { top: 4, type: 'scroll', textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: l.dates, boundaryGap: false, axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'value',
      scale: true,
      name: '累计净值',
      axisLabel: { fontSize: 10, formatter: (v: number) => v.toFixed(1) },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', height: 20, bottom: 18 },
    ],
    series: cols.map((c, ci) => ({
      name: c === 'LS' ? '多空（高组 − 低组）' : `第 ${c.slice(1)} 组`,
      type: 'line' as const,
      data: byCol[ci],
      symbol: 'none',
      lineStyle: {
        width: c === 'LS' ? 1.8 : 1.3,
        type: c === 'LS' ? ('dashed' as const) : ('solid' as const),
      },
      itemStyle: { color: c === 'LS' ? LS_COLOR : GROUP_COLORS[ci % GROUP_COLORS.length] },
    })),
  }
})

/* ---------------- IC ---------------- */
const icOption = computed<EChartsOption>(() => {
  const r = icResp.value
  if (!r || !r.series.length) return { series: [] }
  const dates = r.series.map((s) => s.date)
  return {
    grid: { left: 58, right: 24, top: 30, bottom: 62 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', name: 'RankIC', axisLabel: { fontSize: 10 } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', height: 20, bottom: 18 },
    ],
    series: [
      {
        type: 'bar' as const,
        data: r.series.map((s) => ({
          value: s.ic,
          // 中国市场惯例：正 IC 用红、负 IC 用绿
          itemStyle: { color: (s.ic ?? 0) >= 0 ? '#d03050' : '#18a058' },
        })),
        barMaxWidth: 6,
      },
    ],
  }
})

/* ---------------- 行类型收口（el-table 插槽不给行类型） ---------------- */
function asGroup(row: unknown): FactorGroupRow {
  return row as FactorGroupRow
}
function asExt(row: unknown): FactorExtreme {
  return row as FactorExtreme
}
function asQuant(row: unknown): { label: string; valueText: string } {
  return row as { label: string; valueText: string }
}

function goBack() {
  void router.push({ name: 'factors' })
}

async function loadAnalysis() {
  if (!detail.value?.has_analysis) return
  try {
    layered.value = await getFactorLayered(name.value)
  } catch {
    layered.value = null
  }
  try {
    icResp.value = await getFactorIc(name.value)
  } catch {
    icResp.value = null
  }
}

async function load() {
  if (!name.value) return
  loading.value = true
  try {
    detail.value = await getFactorDetail(name.value)
    await Promise.all([loadSeries(), loadCross(), loadAnalysis()])
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.factor-id {
  font-size: 14px;
  font-weight: 400;
  color: #8a8f99;
  margin-left: 6px;
}

.alert-gap {
  margin-bottom: 12px;
}

.factor-tabs {
  background: #fff;
  border: 0.5px solid #e4e7ed;
  border-radius: 10px;
  padding: 4px 16px 16px;
}

.metric .value.small {
  font-size: 14px;
  font-weight: 500;
  line-height: 1.4;
}

.desc-block {
  margin-top: 14px;
}

.tag-gap {
  margin-right: 4px;
}

.ctrl-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.sym-select {
  min-width: 260px;
}

.foot-note {
  margin: 10px 0 0;
  font-size: 12px;
  color: #8a8f99;
  line-height: 1.6;
}

.stat-line {
  margin-top: 10px;
  font-size: 12.5px;
  color: #5a6069;
}

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

@media (max-width: 1100px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

.mini-tabs {
  margin-top: -6px;
}

.ic-metrics {
  margin-top: 14px;
}

.note-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12.5px;
  color: #5a6069;
  line-height: 1.85;
}

.text-warn-strong {
  color: var(--color-warn);
  font-weight: 600;
}
</style>
