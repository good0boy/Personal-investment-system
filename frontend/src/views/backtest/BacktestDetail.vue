<!--
  模块 3 · 策略回测 —— 运行详情页

  三个信息层：
    1. 顶部提示条（notes）—— **调试运行**这类"别拿它下结论"的警告要第一眼看到
    2. 图表层（净值 vs 基准 + 回撤副图 + 月度热力图）
    3. 明细层（指标卡分组 / 交易记录 / 委托流水 / 配置与产物）

  ★ 净值曲线的时间轴锁定在 `data_store.READ_UNTIL` 之内（回测区间本身），
    这里只是把它画出来，不做任何日期计算 —— 前端不参与"哪些数据可见"的决策。
-->
<template>
  <div class="page" v-loading="loading">
    <!-- 返回 + 标识 -->
    <div class="head">
      <el-button link size="small" @click="goBack">
        <el-icon><ArrowLeft /></el-icon>
        返回列表
      </el-button>

      <div class="head-main">
        <div class="head-title">
          <span class="strategy">{{ detail?.strategy || strategy }}</span>
          <el-tag v-if="detail?.tag" size="small" effect="plain">{{ detail.tag }}</el-tag>
          <el-tag v-if="detail" size="small" :type="statusTagType(detail.status)" effect="plain">
            {{ statusLabel(detail.status) }}
          </el-tag>
          <el-tag v-if="detail?.is_mock" size="small" type="info" effect="dark">
            模拟数据
          </el-tag>
          <el-tag v-if="detail?.is_out_sample" size="small" type="warning" effect="plain">
            样本外
          </el-tag>
          <el-tag
            v-if="detail?.debug_symbols"
            size="small"
            type="danger"
            effect="dark"
          >
            调试运行
          </el-tag>
        </div>
        <div class="head-sub mono">{{ runId }}</div>
      </div>

      <div class="head-right">
        <el-select
          v-if="otherRuns.length"
          size="small"
          placeholder="切换运行"
          style="width: 190px"
          @change="switchRun"
        >
          <el-option
            v-for="r in otherRuns"
            :key="r.run_id"
            :label="`${r.tag || r.run_id}（${r.run_id.slice(0, 15)}）`"
            :value="r.run_id"
          />
        </el-select>
        <el-button size="small" :loading="loading" @click="loadAll">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 提示条 -->
    <div v-if="detail?.notes?.length" class="notes">
      <div
        v-for="(n, i) in detail.notes"
        :key="i"
        class="note"
        :class="`note-${n.level}`"
      >
        <el-icon>
          <component :is="n.level === 'danger' ? 'WarningFilled' : n.level === 'warning' ? 'Warning' : 'InfoFilled'" />
        </el-icon>
        <span>{{ n.text }}</span>
      </div>
    </div>

    <!-- 概览条 -->
    <div v-if="detail" class="panel overview">
      <div class="ov-item">
        <span class="ov-label">回测区间</span>
        <span class="ov-value mono">{{ detail.start_date }} ~ {{ detail.end_date }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">交易日</span>
        <span class="ov-value">{{ detail.trading_days ?? DASH }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">初始资金</span>
        <span class="ov-value mono">{{ fmtMoney(detail.initial_cash, 0) }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">基准</span>
        <span class="ov-value mono">{{ detail.benchmark || DASH }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">策略版本</span>
        <span class="ov-value">v{{ detail.strategy_version || DASH }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">耗时</span>
        <span class="ov-value">{{ detail.cost_seconds ? `${detail.cost_seconds.toFixed(1)}s` : DASH }}</span>
      </div>
      <div class="ov-item">
        <span class="ov-label">完成时间</span>
        <span class="ov-value mono">{{ fmtDateTime(detail.created_at) }}</span>
      </div>
    </div>

    <!-- Tab -->
    <el-tabs v-if="detail" v-model="activeTab" class="detail-tabs">
      <!-- ========== 净值曲线 ========== -->
      <el-tab-pane label="净值曲线" name="equity">
        <div class="panel">
          <div class="panel-title">
            <span>
              策略净值 vs 基准
              <span v-if="equity?.downsampled" class="sub">
                原始 {{ equity.downsampled_from }} 点，已降采样到 {{ equity.count }} 点
              </span>
            </span>
            <span class="text-muted text-small">
              红=策略，灰虚线=基准，橙=回撤（风险用警示色，与涨跌色区分）
            </span>
          </div>
          <EChart :option="equityOption" height="440px" :empty="!equity?.count" />
        </div>

        <div class="panel">
          <div class="panel-title">
            <span>月度收益</span>
            <span class="text-muted text-small">红=盈利，绿=亏损（中国市场惯例）</span>
          </div>
          <EChart :option="monthlyOption" height="300px" :empty="!detail.monthly_returns.length" />
        </div>
      </el-tab-pane>

      <!-- ========== 收益指标 ========== -->
      <el-tab-pane label="收益指标" name="metrics">
        <div v-for="group in metricGroups" :key="group.title" class="panel">
          <div class="panel-title">
            <span>{{ group.title }}</span>
            <span class="text-muted text-small">{{ group.note }}</span>
          </div>
          <div class="metric-grid">
            <div v-for="m in group.items" :key="m.label" class="metric" :class="{ 'metric-key': m.key }">
              <p class="label">
                {{ m.label }}
                <el-tooltip v-if="m.hint" :content="m.hint" placement="top">
                  <el-icon class="hint-icon"><QuestionFilled /></el-icon>
                </el-tooltip>
              </p>
              <p class="value" :class="m.cls">{{ m.value }}</p>
              <p v-if="m.sub" class="hint">{{ m.sub }}</p>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ========== 交易记录 ========== -->
      <el-tab-pane label="交易记录" name="trades">
        <div class="panel">
          <el-tabs v-model="tradeTab" class="inner-tabs">
            <!-- 成交明细 -->
            <el-tab-pane name="filled">
              <template #label>
                成交明细
                <span class="tab-badge">{{ trades?.total ?? 0 }}</span>
              </template>

              <div class="trade-filters">
                <el-input
                  v-model="tradeQuery.symbol"
                  placeholder="代码"
                  size="small"
                  clearable
                  style="width: 130px"
                  @keyup.enter="loadTrades(1)"
                  @clear="loadTrades(1)"
                >
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>

                <el-select
                  v-model="tradeQuery.exit_reasons"
                  placeholder="全部卖出原因"
                  size="small"
                  multiple
                  collapse-tags
                  clearable
                  style="width: 230px"
                  @change="loadTrades(1)"
                >
                  <el-option
                    v-for="r in trades?.filters.exit_reasons || []"
                    :key="r"
                    :label="EXIT_REASON_LABEL[r] || r"
                    :value="r"
                  />
                </el-select>

                <el-radio-group v-model="tradeQuery.win" size="small" @change="loadTrades(1)">
                  <el-radio-button value="all">全部</el-radio-button>
                  <el-radio-button value="win">盈利</el-radio-button>
                  <el-radio-button value="lose">亏损</el-radio-button>
                </el-radio-group>

                <el-date-picker
                  v-model="tradeDateRange"
                  type="daterange"
                  size="small"
                  value-format="YYYY-MM-DD"
                  start-placeholder="买入起"
                  end-placeholder="买入止"
                  style="width: 240px"
                  @change="loadTrades(1)"
                />

                <el-button size="small" @click="exportTrades">
                  <el-icon><Download /></el-icon>
                  导出 CSV
                </el-button>

                <span class="text-muted text-small filter-summary">
                  筛选后 {{ trades?.summary.trade_count ?? 0 }} 笔 ｜
                  净盈亏 <b :class="pnlClass(trades?.summary.net_pnl)">{{ fmtMoneySigned(trades?.summary.net_pnl) }}</b> ｜
                  胜率 {{ fmtPct(trades?.summary.win_rate) }} ｜
                  成本 {{ fmtMoney(trades?.summary.total_cost) }}
                </span>
              </div>

              <!-- ★ :height 动态填满视口剩余空间（写死 560 在高视口下底部留白），内部滚动 -->
              <el-table
                ref="tradesTableRef"
                :data="trades?.items || []"
                size="small"
                stripe
                :height="tradesH"
                empty-text="没有符合条件的交易"
              >
                <el-table-column label="代码" width="106">
                  <template #default="{ row }">
                    <span class="mono">{{ row.symbol }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="买入日" width="102">
                  <template #default="{ row }"><span class="mono">{{ row.entry_date }}</span></template>
                </el-table-column>
                <el-table-column label="买入价" width="84" align="right">
                  <template #default="{ row }">{{ fmtNum(row.entry_price, 2) }}</template>
                </el-table-column>
                <el-table-column label="股数" width="80" align="right">
                  <template #default="{ row }">{{ row.shares }}</template>
                </el-table-column>
                <el-table-column label="买入金额" width="112" align="right">
                  <template #default="{ row }">{{ fmtMoney(row.entry_amount, 0) }}</template>
                </el-table-column>
                <el-table-column label="卖出日" width="102">
                  <template #default="{ row }"><span class="mono">{{ row.exit_date }}</span></template>
                </el-table-column>
                <el-table-column label="卖出价" width="84" align="right">
                  <template #default="{ row }">{{ fmtNum(row.exit_price, 2) }}</template>
                </el-table-column>
                <el-table-column label="持有" width="66" align="right">
                  <template #default="{ row }">{{ row.hold_days }} 日</template>
                </el-table-column>
                <el-table-column label="成本" width="94" align="right">
                  <template #default="{ row }">{{ fmtMoney(row.cost) }}</template>
                </el-table-column>
                <el-table-column label="净盈亏" width="120" align="right" sortable>
                  <template #default="{ row }">
                    <span :class="pnlClass(row.net_pnl)">{{ fmtMoneySigned(row.net_pnl) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="净收益率" width="100" align="right" sortable>
                  <template #default="{ row }">
                    <span :class="pnlClass(row.net_return)">{{ fmtPctSigned(row.net_return) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="卖出原因" min-width="150">
                  <template #default="{ row }">
                    <el-tag
                      v-for="r in row.exit_reason.split('|')"
                      :key="r"
                      size="small"
                      effect="plain"
                      class="reason-tag"
                    >
                      {{ EXIT_REASON_LABEL[r] || r }}
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>

              <el-pagination
                v-if="(trades?.pages || 0) > 1"
                class="pager"
                size="small"
                layout="total, sizes, prev, pager, next, jumper"
                :total="trades?.total || 0"
                :current-page="tradeQuery.page"
                :page-size="tradeQuery.page_size"
                :page-sizes="[20, 50, 100, 200]"
                @current-change="(p: number) => loadTrades(p)"
                @size-change="(s: number) => { tradeQuery.page_size = s; loadTrades(1) }"
              />
            </el-tab-pane>

            <!-- 委托流水 -->
            <el-tab-pane name="orders">
              <template #label>
                委托流水
                <!-- 懒加载：没加载前不显示 0，否则会让人以为"一条都没有" -->
                <span v-if="orders" class="tab-badge">{{ orders.total }}</span>
              </template>

              <div class="trade-filters">
                <el-radio-group v-model="orderStatus" size="small" @change="loadOrders">
                  <el-radio-button value="all">全部</el-radio-button>
                  <el-radio-button value="filled">已成交</el-radio-button>
                  <el-radio-button value="pending">顺延未成交</el-radio-button>
                  <el-radio-button value="rejected">被拒</el-radio-button>
                </el-radio-group>
                <span class="text-muted text-small">
                  跌停/停牌当日<b>没有对手盘</b>，订单顺延而不是按理想价撮合 ——
                  这张表是唯一能看出来这件事的地方（净值曲线上看不出来）。
                </span>
              </div>

              <el-table
                ref="ordersTableRef"
                :data="orders?.items || []"
                size="small"
                stripe
                :height="ordersH"
                empty-text="没有委托记录"
              >
                <el-table-column prop="signal_date" label="信号日" width="102" />
                <el-table-column prop="exec_date" label="执行日" width="102" />
                <el-table-column label="代码" width="106">
                  <template #default="{ row }"><span class="mono">{{ row.symbol }}</span></template>
                </el-table-column>
                <el-table-column label="方向" width="70">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.side === 'buy' ? 'danger' : 'success'" effect="plain">
                      {{ row.side === 'buy' ? '买入' : '卖出' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="shares" label="股数" width="80" align="right" />
                <el-table-column label="价格" width="86" align="right">
                  <template #default="{ row }">{{ row.price === null ? DASH : fmtNum(row.price, 2) }}</template>
                </el-table-column>
                <el-table-column label="金额" width="110" align="right">
                  <template #default="{ row }">{{ row.amount === null ? DASH : fmtMoney(row.amount, 0) }}</template>
                </el-table-column>
                <el-table-column label="状态" width="92">
                  <template #default="{ row }">
                    <el-tag
                      size="small"
                      effect="plain"
                      :type="row.status === 'filled' ? 'success' : row.status === 'pending' ? 'warning' : 'danger'"
                    >
                      {{ ORDER_STATUS_LABEL[row.status] || row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="note" label="备注" min-width="200" show-overflow-tooltip />
              </el-table>
            </el-tab-pane>
          </el-tabs>
        </div>
      </el-tab-pane>

      <!-- ========== 配置与产物 ========== -->
      <el-tab-pane label="配置与产物" name="config">
        <div class="panel">
          <div class="panel-title">
            <span>产物文件</span>
            <span class="text-muted text-small">
              目录 <span class="mono">data/backtest/{{ detail.strategy }}/{{ detail.run_id }}/</span>
            </span>
          </div>
          <div class="files">
            <div v-for="f in detail.files" :key="f.name" class="file-item">
              <span class="mono file-name">{{ f.name }}</span>
              <span class="text-muted text-small">{{ f.human }}</span>
            </div>
          </div>
          <div class="row-stats">
            <span>净值 <b>{{ detail.rows.equity }}</b> 行</span>
            <span>交易 <b>{{ detail.rows.trades ?? 0 }}</b> 笔</span>
            <span>持仓快照 <b>{{ detail.rows.positions ?? 0 }}</b> 行</span>
            <span>委托 <b>{{ detail.rows.orders ?? 0 }}</b> 条</span>
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">
            <span>配置快照（config.json）</span>
            <el-button size="small" link type="primary" @click="copyConfig">
              <el-icon><CopyDocument /></el-icon>
              复制
            </el-button>
          </div>
          <p class="text-muted text-small config-hint">
            这份配置是结果可复现的唯一凭证 —— 两个 run 结果不一致时，
            直接 diff 这里的 JSON 就能定位差异。
          </p>
          <pre class="code-block config-json">{{ configJson }}</pre>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart.vue'
import { getEquity, getOrders, getRunDetail, getRuns, getTrades } from '@/api/backtest'
import type {
  BacktestRunDetail,
  BacktestRunSummary,
  EquityCurve,
  OrdersResp,
  TradesResp,
} from '@/types/api'
import {
  DASH,
  downloadCsv,
  fmtDateTime,
  fmtMoney,
  fmtMoneySigned,
  fmtNum,
  fmtPct,
  fmtPctSigned,
  pnlClass,
  statusLabel,
  statusTagType,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()

const strategy = computed(() => String(route.params.strategy ?? ''))
const runId = computed(() => String(route.params.runId ?? ''))

const loading = ref(false)
const detail = ref<BacktestRunDetail | null>(null)
const equity = ref<EquityCurve | null>(null)
const trades = ref<TradesResp | null>(null)
const orders = ref<OrdersResp | null>(null)
const otherRuns = ref<BacktestRunSummary[]>([])

const activeTab = ref('equity')
const tradeTab = ref('filled')

/**
 * 「交易记录」两个表格的高度 = 视口剩余空间（动态实测，不写死）。
 *
 * 为什么不用写死的 max-height（560/480）：上方概览面板 + 页签头 + 筛选条的高度固定，
 * 视口一高表格底下就留出一大块空白。改成 `height` 让表格**填满剩余空间、内部滚动**，
 * 分页条也始终落在视口内。
 *
 * 尾部预留：
 *   成交明细   84 = 分页条 ~24 + 其 margin 12 + 面板 padding-bottom 16 + page 底 padding 28 + 边框 ~4
 *   委托流水   48 = 上面的分页两条不存在（它没有分页），其余相同
 * 位置用「相对 `.el-main` 的绝对纵坐标」（rect.top 是视口坐标，滚动后会变负）。
 */
const tradesTableRef = ref()
const ordersTableRef = ref()
const tradesH = ref(560)
const ordersH = ref(480)

function fitTableH(
  el: HTMLElement | undefined | null,
  tailPx: number,
  minH: number,
): number | null {
  if (!el || !el.offsetParent) return null // 面板未显示（其它页签激活）→ 不量
  const main = el.closest('.el-main')
  const mainTop = main ? main.getBoundingClientRect().top : 0
  const scrolled = main ? main.scrollTop : 0
  // 以 `.el-main` 的**内容坐标**为基准：表格高 = main 可视高 - 表格顶(内容坐标) - 尾部预留
  // （rect.top 是视口坐标，滚动后会变负，不能直接用）
  const y = el.getBoundingClientRect().top - mainTop + scrolled
  const availH = main ? main.clientHeight : window.innerHeight - mainTop
  return Math.max(minH, Math.round(availH - y - tailPx))
}

function syncTableHeights() {
  const t = fitTableH(tradesTableRef.value?.$el as HTMLElement | null, 84, 280)
  if (t !== null) tradesH.value = t
  const o = fitTableH(ordersTableRef.value?.$el as HTMLElement | null, 48, 280)
  if (o !== null) ordersH.value = o
}

// 页签切换到「交易记录」时表格才真正可见（此前量到的是 0），必须重新实测
watch([activeTab, tradeTab], () => nextTick(() => setTimeout(syncTableHeights, 60)))

/** 卖出原因的中文标签（后端只给枚举字面量） */
const EXIT_REASON_LABEL: Record<string, string> = {
  time_stop: '时间止损',
  take_profit: '止盈',
  stop_loss: '个股止损',
  market_panic: '大盘恐慌',
  limit_down: '跌停',
  st: 'ST',
  delist: '退市强平',
}

const ORDER_STATUS_LABEL: Record<string, string> = {
  filled: '已成交',
  pending: '顺延中',
  rejected: '被拒',
}

// ======================================================================
// 加载
// ======================================================================
async function loadAll() {
  loading.value = true
  try {
    const d = await getRunDetail(strategy.value, runId.value)
    detail.value = d

    // 净值与交易并行；任一失败不该让整页打不开（详情本身已有信息）
    const [eq, tr] = await Promise.allSettled([
      getEquity(strategy.value, runId.value),
      // 初始加载不带筛选（tradeQuery 里的 exit_reasons 是多选数组，不能直接展开传给
      // TradeQuery —— 它要的是逗号串）
      getTrades(strategy.value, runId.value, {
        page: 1,
        page_size: tradeQuery.page_size,
        win: tradeQuery.win,
      }),
    ])
    equity.value = eq.status === 'fulfilled' ? eq.value : null
    if (tr.status === 'fulfilled') trades.value = tr.value

    // 用于顶部的"切换运行"下拉
    const runs = await getRuns({ strategy: d.strategy, limit: 50 })
    otherRuns.value = runs.items.filter((r) => r.run_id !== runId.value)
  } finally {
    loading.value = false
  }
}

function goBack() {
  void router.push({ name: 'backtest' })
}

function switchRun(id: string) {
  void router.push({
    name: 'backtest-detail',
    params: { strategy: strategy.value, runId: id },
  })
}

async function copyConfig() {
  try {
    await navigator.clipboard.writeText(configJson.value)
    ElMessage.success('配置已复制')
  } catch {
    ElMessage.warning('浏览器拒绝了剪贴板访问，请手动选中复制')
  }
}

// ======================================================================
// 指标卡
// ======================================================================
interface MetricCard {
  label: string
  value: string
  sub?: string
  hint?: string
  cls?: string
  /** 规格强制展示的两个指标，加边框强调 */
  key?: boolean
}

const metricGroups = computed(() => {
  const m = detail.value?.metrics ?? {}
  return [
    {
      title: '收益',
      note: '净值口径：总资产 = 现金 + 持仓市值（收盘价 mark-to-market）',
      items: [
        { label: '总收益率', value: fmtPctSigned(m.total_return), cls: pnlClass(m.total_return) },
        { label: '年化收益率', value: fmtPctSigned(m.annual_return), cls: pnlClass(m.annual_return) },
        {
          label: '期末总资产',
          value: fmtMoney(m.final_asset, 0),
          sub: `初始 ${fmtMoney(m.initial_cash, 0)}`,
        },
      ] as MetricCard[],
    },
    {
      title: '风险',
      note: '无风险利率取 0；夏普用日收益年化（×√242）',
      items: [
        {
          label: '最大回撤',
          value: fmtPct(m.max_drawdown),
          cls: pnlClass(m.max_drawdown),
          sub: m.max_drawdown_start ? `${m.max_drawdown_start} → ${m.max_drawdown_end}` : undefined,
        },
        { label: '年化波动率', value: fmtPct(m.annual_volatility) },
        { label: '夏普比率', value: fmtNum(m.sharpe) },
        { label: 'Calmar', value: fmtNum(m.calmar), hint: '年化收益 / |最大回撤|' },
        {
          label: '★ 最大单笔亏损',
          value: fmtPct(m.max_single_loss),
          cls: pnlClass(m.max_single_loss),
          sub: m.max_single_loss_amount ? fmtMoney(m.max_single_loss_amount) : undefined,
          hint: '本策略不设个股止损，只有时间止损 —— 这是风险敞口的唯一窗口',
          key: true,
        },
      ] as MetricCard[],
    },
    {
      title: '交易',
      note: '只统计已平仓的交易',
      items: [
        { label: '交易次数', value: String(m.trade_count ?? 0) },
        {
          label: '胜率',
          value: fmtPct(m.win_rate),
          sub: `${m.win_count ?? 0} 赢 / ${m.lose_count ?? 0} 亏`,
        },
        { label: '盈亏比', value: fmtNum(m.payoff_ratio), sub: `赢均 ${fmtMoney(m.avg_win, 0)} ｜ 亏均 ${fmtMoney(m.avg_lose, 0)}` },
        { label: '单笔期望', value: fmtMoney(m.expectancy), cls: pnlClass(m.expectancy) },
        { label: '平均持有', value: `${fmtNum(m.avg_hold_days, 1)} 日` },
      ] as MetricCard[],
    },
    {
      title: '成本',
      note: '★ 本策略持有期短（≤10 交易日），成本是主要敌人',
      items: [
        {
          label: '★ 年化交易成本',
          value: fmtPct(m.annual_cost_ratio),
          sub: `累计 ${fmtMoney(m.total_cost, 0)}`,
          hint: '分母取平均总资产（不是初始资金）—— 成本按每笔成交额产生，净值翻倍后同样的换手占总资产的比例会下降',
          cls: 'cost-value',
          key: true,
        },
        {
          label: '成本 / 毛收益',
          value: fmtPct(m.cost_to_gross_profit),
          hint: '交易成本吃掉了多少毛收益；>100% 表示策略赚的钱还不够付手续费',
        },
        { label: '年化换手率', value: fmtPct(m.turnover_annual), hint: '年化双边成交额 / 平均总资产' },
      ] as MetricCard[],
    },
  ]
})

// ======================================================================
// 净值 + 回撤（共用 dataZoom，联动缩放）
// ======================================================================
const equityOption = computed<EChartsOption>(() => {
  const eq = equity.value
  if (!eq?.count) return {}
  const s = eq.series
  const dates = s.date
  const hasBench = Array.isArray(s.benchmark_nav) && s.benchmark_nav.some((v) => v !== null)

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      valueFormatter: (v: unknown) =>
        typeof v === 'number' ? `${(v * 100).toFixed(2)}%` : String(v ?? ''),
    },
    legend: { top: 0, data: ['策略净值', ...(hasBench ? ['基准净值'] : []), '回撤'] },
    // 两个 grid 的十字准星联动 —— 这是"主图与副图联动"的关键配置
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 66, right: 26, top: 38, height: '48%' },
      { left: 66, right: 26, top: '66%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category',
        gridIndex: 0,
        data: dates,
        boundaryGap: false,
        axisLabel: { show: false },
        axisTick: { show: false },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: dates,
        boundaryGap: false,
        axisLabel: { fontSize: 11, hideOverlap: true },
      },
    ],
    yAxis: [
      {
        type: 'value',
        gridIndex: 0,
        scale: true,
        name: '净值',
        nameTextStyle: { fontSize: 11, color: '#8a8f99' },
        axisLabel: { fontSize: 11, formatter: (v: number) => v.toFixed(2) },
        splitLine: { lineStyle: { color: '#eef0f3' } },
      },
      {
        type: 'value',
        gridIndex: 1,
        name: '回撤',
        max: 0,
        nameTextStyle: { fontSize: 11, color: '#8a8f99' },
        axisLabel: {
          fontSize: 11,
          // 用 Math.round 而不是 toFixed：ECharts 的分档会出现 ~-0.001 这样的值，
          // toFixed(0) 会渲染成刺眼的 `-0%`。
          formatter: (v: number) => `${Math.round(v * 100)}%`,
        },
        splitLine: { lineStyle: { color: '#eef0f3' } },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1] },
      { type: 'slider', xAxisIndex: [0, 1], height: 18, bottom: 8 },
    ],
    series: [
      {
        name: '策略净值',
        type: 'line',
        data: s.nav ?? [],
        xAxisIndex: 0,
        yAxisIndex: 0,
        showSymbol: false,
        lineStyle: { width: 2, color: '#d03050' },
        itemStyle: { color: '#d03050' },
        z: 3,
      },
      ...(hasBench
        ? [
            {
              name: '基准净值',
              type: 'line' as const,
              data: s.benchmark_nav ?? [],
              xAxisIndex: 0,
              yAxisIndex: 0,
              showSymbol: false,
              lineStyle: { width: 1.4, color: '#8a8f99', type: 'dashed' as const },
              itemStyle: { color: '#8a8f99' },
            },
          ]
        : []),
      {
        name: '回撤',
        type: 'line',
        data: s.drawdown ?? [],
        xAxisIndex: 1,
        yAxisIndex: 1,
        showSymbol: false,
        // 回撤是风险信号，用**警示橙**：红色在本系统里表示"涨"，
        // 用红色画回撤会与"红=好"的直觉冲突（见 styles/main.css 的注释）。
        lineStyle: { width: 1, color: '#d97706' },
        itemStyle: { color: '#d97706' },
        areaStyle: { color: 'rgba(217, 119, 6, 0.18)' },
      },
    ],
  }
})

// ======================================================================
// 月度收益热力图
// ======================================================================
const monthlyOption = computed<EChartsOption>(() => {
  const monthly = detail.value?.monthly_returns ?? []
  if (!monthly.length) return {}

  const years = [...new Set(monthly.map((m) => m.year))].sort((a, b) => a - b)
  const yearIdx = new Map(years.map((y, i) => [y, i]))
  const values = monthly.filter((m) => m.return !== null).map((m) => m.return as number)
  const bound = Math.max(0.02, ...values.map((v) => Math.abs(v)))

  const data = monthly.map((m) => [
    m.month - 1,
    yearIdx.get(m.year) ?? 0,
    m.return === null ? 0 : m.return,
  ])

  return {
    tooltip: {
      position: 'top',
      formatter: (p: any) => {
        const y = years[p.value[1]]
        const v = p.value[2] as number
        return `${y} 年 ${p.value[0] + 1} 月<br/>${(v * 100).toFixed(2)}%`
      },
    },
    grid: { left: 58, right: 22, top: 12, bottom: 56 },
    xAxis: {
      type: 'category',
      data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
      splitArea: { show: true },
      axisLabel: { fontSize: 11 },
    },
    yAxis: {
      type: 'category',
      data: years.map(String),
      splitArea: { show: true },
      axisLabel: { fontSize: 11 },
    },
    visualMap: {
      min: -bound,
      max: bound,
      orient: 'horizontal',
      left: 'center',
      bottom: 8,
      itemWidth: 12,
      itemHeight: 90,
      textStyle: { fontSize: 11 },
      // 绿（亏）→ 灰（平）→ 红（赚）：中国市场惯例
      inRange: { color: ['#18a058', '#f2f3f5', '#d03050'] },
      formatter: (v: unknown) => `${((v as number) * 100).toFixed(0)}%`,
    },
    series: [
      {
        type: 'heatmap',
        data,
        label: {
          show: true,
          fontSize: 10,
          formatter: (p: any) => `${(p.value[2] * 100).toFixed(1)}`,
        },
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,.25)' } },
      },
    ],
  }
})

const configJson = computed(() => JSON.stringify(detail.value?.config ?? {}, null, 2))

// ======================================================================
// 交易记录
// ======================================================================
const tradeQuery = reactive<{
  page: number
  page_size: number
  symbol: string
  exit_reasons: string[]
  win: 'all' | 'win' | 'lose'
}>({
  page: 1,
  page_size: 50,
  symbol: '',
  exit_reasons: [],
  win: 'all',
})

const tradeDateRange = ref<[string, string] | null>(null)

async function loadTrades(page = 1) {
  tradeQuery.page = page
  trades.value = await getTrades(strategy.value, runId.value, {
    page,
    page_size: tradeQuery.page_size,
    symbol: tradeQuery.symbol || undefined,
    exit_reasons: tradeQuery.exit_reasons.length ? tradeQuery.exit_reasons.join(',') : undefined,
    win: tradeQuery.win,
    start: tradeDateRange.value?.[0],
    end: tradeDateRange.value?.[1],
  })
}

const EXPORT_MAX = 500

async function exportTrades() {
  const t = trades.value
  if (!t || !t.total) {
    ElMessage.warning('当前筛选结果为空，没有可导出的内容')
    return
  }
  // 拉一次"筛选后的前 500 条"，而不是只导当前页 —— 用户导出是为了自己再算
  const data = await getTrades(strategy.value, runId.value, {
    page: 1,
    page_size: EXPORT_MAX,
    symbol: tradeQuery.symbol || undefined,
    exit_reasons: tradeQuery.exit_reasons.length ? tradeQuery.exit_reasons.join(',') : undefined,
    win: tradeQuery.win,
    start: tradeDateRange.value?.[0],
    end: tradeDateRange.value?.[1],
  })

  const header = [
    '代码', '买入日', '买入价', '股数', '买入金额', '买入费用',
    '卖出日', '卖出价', '卖出金额', '卖出费用', '总成本',
    '毛盈亏', '净盈亏', '净收益率', '持有交易日', '卖出原因',
  ]
  const rows = data.items.map((r) => [
    r.symbol, r.entry_date, r.entry_price, r.shares, r.entry_amount, r.entry_cost,
    r.exit_date, r.exit_price, r.exit_amount, r.exit_cost, r.cost,
    r.gross_pnl, r.net_pnl, (r.net_return * 100).toFixed(4) + '%', r.hold_days, r.exit_reason,
  ])
  downloadCsv(`${strategy.value}_${runId.value}_trades.csv`, [header, ...rows])

  ElMessage.success(
    data.total > EXPORT_MAX
      ? `已导出前 ${EXPORT_MAX} 条（筛选后共 ${data.total} 条，请收窄筛选条件以导出全部）`
      : `已导出 ${data.items.length} 条`,
  )
}

// ======================================================================
// 委托流水
// ======================================================================
const orderStatus = ref('all')

async function loadOrders() {
  orders.value = await getOrders(strategy.value, runId.value, {
    status: orderStatus.value === 'all' ? undefined : orderStatus.value,
    limit: 1000,
  })
}

// 切到子 tab 才拉数据（省一次请求）。
// ★ 必须 immediate —— 只在"变化时"触发的话，若初始 tab 就是 orders
//   （将来用 URL 参数直接进入某个子 tab 时就会这样），表格会永远空着，
//   而且显示的是"没有委托记录"这种**看起来正常**的空态，很难查。
watch(
  tradeTab,
  (v) => {
    if (v === 'orders' && !orders.value) void loadOrders()
  },
  { immediate: true },
)

watch(
  () => [strategy.value, runId.value],
  () => void loadAll(),
)

onMounted(() => {
  loadAll()
  window.addEventListener('resize', syncTableHeights)
})
onBeforeUnmount(() => window.removeEventListener('resize', syncTableHeights))
</script>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
}

.head-main {
  flex: 1;
  min-width: 0;
}

.head-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.strategy {
  font-size: 17px;
  font-weight: 500;
}

.head-sub {
  color: #9aa0a9;
  font-size: 12px;
  margin-top: 2px;
}

.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ---------- 提示条 ---------- */
.notes {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 14px;
}

.note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 14px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
}

.note .el-icon {
  margin-top: 2px;
  flex: 0 0 auto;
}

.note-danger {
  background: #fdeeee;
  border: 0.5px solid #f5bcbc;
  color: #a3252b;
  font-weight: 500;
}

.note-warning {
  background: #fdf3e6;
  border: 0.5px solid #f3d3a3;
  color: #8a5a12;
}

.note-info {
  background: #f2f4f7;
  border: 0.5px solid #dfe3e8;
  color: #5a6169;
}

/* ---------- 概览 ---------- */
.overview {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 34px;
  padding: 12px 18px;
}

.ov-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.ov-label {
  font-size: 12.5px;
  color: #8a8f99;
}

.ov-value {
  font-size: 13px;
  font-weight: 500;
  color: #1f2329;
}

/* ---------- Tab ---------- */
.detail-tabs {
  margin-top: 2px;
}

/* 页签内面板去掉自带 16px 下边距：它是页签内容的最后一个元素，
   否则交易记录页会比视口多出 16px 滚动量，破坏"正好填满"。
   （收益指标页有多个面板堆叠，只有最后一个失去下边距 —— 上间距不受影响。） */
.detail-tabs .el-tab-pane .panel {
  margin-bottom: 0;
}

.metric-key {
  border: 0.5px solid #f0d0a8;
  background: #fdf9f3;
}

.hint-icon {
  color: #b4b9c1;
  font-size: 12px;
  vertical-align: -1px;
  cursor: help;
}

.cost-value {
  color: var(--color-warn);
}

/* ---------- 交易 ---------- */
.trade-filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.filter-summary {
  margin-left: auto;
}

.tab-badge {
  display: inline-block;
  margin-left: 5px;
  padding: 0 6px;
  border-radius: 8px;
  background: #eef0f3;
  font-size: 11px;
  color: #6b7280;
  line-height: 16px;
}

.reason-tag {
  margin-right: 4px;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

/* ---------- 产物 ---------- */
.files {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.file-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  background: #f7f8fa;
  border-radius: 7px;
  padding: 6px 12px;
}

.file-name {
  font-size: 12.5px;
  color: #3c4148;
}

.row-stats {
  display: flex;
  gap: 20px;
  margin-top: 12px;
  font-size: 12.5px;
  color: #8a8f99;
}

.row-stats b {
  color: #1f2329;
}

.config-hint {
  margin: 0 0 10px;
}

.config-json {
  max-height: 520px;
  overflow: auto;
}

.inner-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}
</style>
