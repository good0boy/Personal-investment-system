/** 后端统一响应格式（与 backend/app/schemas/common.py 对齐） */
export interface ApiResponse<T = unknown> {
  code: number
  message: string
  data: T | null
  ts: string
}

export interface ApiError {
  code: number
  message: string
  detail?: unknown
  ts?: string
}

/** 滑动窗口限流器的运行状态（与后端 schemas/common.py::RateLimiterStats 对齐）
 *
 * ⚠️ 后端这两个 stats 模型开了 `extra="allow"`，将来加字段会**透传**过来，
 *    所以这里用索引签名兜底，避免"后端加了字段、前端 TS 报错"的连锁改动。
 */
export interface RateLimiterStats {
  name: string
  min_interval: number
  max_calls_per_minute: number | null
  calls_in_window: number
  window_seconds: number
  total_calls: number
  total_wait_seconds: number
  max_wait_seconds: number
  [key: string]: unknown
}

/** 熔断器状态。`open=true` 表示**当前正被熔断**，此期间一个请求都不会发出去。 */
export interface CircuitBreakerStats {
  name: string
  open: boolean
  remaining_seconds: number
  cooldown_seconds: number
  soft_cooldown_seconds: number
  /** 连续 soft 失败计数（"疑似限频 2/3 次"里的 2） */
  soft_strikes: number
  soft_threshold: number
  trip_count: number
  last_trip_at: string | null
  reason: string
  [key: string]: unknown
}

/** 单个数据源的限流 / 熔断 / 缓存状态 */
export interface ThrottleStats {
  datasource: string
  rate_limiter: RateLimiterStats | null
  circuit_breaker: CircuitBreakerStats | null
  cache: Record<string, unknown>
  [key: string]: unknown
}

/** 数据源健康状态 */
export interface DatasourceHealth {
  name: string
  available: boolean
  version: string
  message: string
  checked_at: string
  latency_ms: number | null
  cache: Record<string, number>
  /** 限流 / 熔断状态。老版本后端不返回该字段时为 null/undefined。 */
  throttle?: ThrottleStats | null
}

export interface CalendarStats {
  total_days: number | null
  trading_days: number | null
  first_date: string | null
  /** 日历**覆盖到**哪一天（含未来排期，可能是 2030-12-31） */
  coverage_end: string | null
  /** 覆盖范围内的未来交易日数量（仅参考，业务只能用到 last_date） */
  future_trading_days: number | null
  /** 最后一个**已发生**的交易日（≤ 今天）—— 业务可用上界 */
  last_date: string | null
  today: string | null
  today_is_trading_day: boolean | null
  latest_trading_day: string | null
  synced_at: string | null
}

export interface StorageStats {
  data_root: string
  sections: Record<string, { files: number; bytes: number; human: string }>
  total_bytes: number
  total_human: string
  trade_calendar_ready: boolean
}

export interface DatabaseStats {
  db_path: string
  initialized: boolean
  size_bytes: number
  size_human: string
  table_counts: Record<string, number>
}

export interface JobInfo {
  job_id: string
  name: string
  trigger: string
  next_run_time: string | null
  enabled: boolean
  in_scheduler?: boolean
}

/** 启动补跑的进行状态（P1-1 新增）。
 *
 * 补跑现在跑在**后台线程**里，端口不再被它挡住 —— 所以"服务起来了但数据还在补"
 * 这件事必须能被看到，否则用户会以为"页面没数据 = 服务坏了"。
 */
export interface CatchUpState {
  running: boolean
  started_at: string | null
  finished_at: string | null
  error: string | null
  result: Record<string, unknown> | null
}

export interface SchedulerStatus {
  enabled: boolean
  running: boolean
  timezone: string
  job_count: number
  catch_up_on_startup: boolean
  catch_up_max_days: number
  /** 后端老版本可能不返回该字段 */
  catch_up?: CatchUpState
}

export interface HealthData {
  status: 'ok' | 'degraded' | 'error'
  app_name: string
  app_version: string
  env: string
  server_time: string
  uptime_seconds: number
  python_version: string
  akshare_version: string
  baostock_version: string
  data_last_date: string | null
  datasources: DatasourceHealth[]
  calendar: CalendarStats | null
  storage: StorageStats | null
  database: DatabaseStats | null
  scheduler_enabled: boolean
  jobs: JobInfo[]
  warnings: string[]
  wecom_configured: boolean
  wecom_key_masked: string
}

export interface JobRunRecord {
  id: number
  job_id: string
  run_date: string
  trigger_type: string
  status: string
  duration: number | null
  message: string | null
  created_at: string
  finished_at: string | null
}

export interface JobsData {
  scheduler: SchedulerStatus
  jobs: JobInfo[]
  recent_runs: JobRunRecord[]
}

/** 数据源能力 */
export interface CapabilityItem {
  supported: boolean
  quality: string
  note: string
  priority: string
}

export interface CapabilityMetaItem {
  label: string
  desc?: string
  critical?: boolean
}

export interface CapabilityGroup {
  key: string
  label: string
  note?: string
  items?: string[]
}

export interface DatasourceCapability {
  name: string
  display_name: string
  role: string
  version_pin: string
  version_note?: string
  homepage: string
  license: string
  symbol_format: string
  symbol_note?: string
  stability: { interface_change_risk?: string; note?: string }
  pitfalls: string[]
  capabilities: Record<string, CapabilityItem>
  /** ★ 实测标记：false = 依据文档整理、未在本环境真实调用 */
  verified: boolean
  verified_at: string | null
  verified_by?: string | null
  verified_note?: string | null
  stale?: boolean
  supported_count?: number
  capability_count?: number
  critical_count?: number
  critical_total?: number
  critical_ratio?: number
}

export interface DatasourceMatrix {
  capability_meta: {
    groups: CapabilityGroup[]
    capabilities: Record<string, CapabilityMetaItem>
  }
  capability_keys: string[]
  sources: DatasourceCapability[]
  source_count: number
  verified_count: number
  critical_total?: number
  verify_stale_days?: number
}

export interface CalendarDays {
  start: string
  end: string
  count: number
  days: string[]
}

// ======================================================================
// 模块 1 · 接口目录
// ======================================================================
export interface CatalogParam {
  name: string
  type: string
  required?: boolean
  default?: unknown
  desc: string
  options?: string[]
}

export interface CatalogReturn {
  name: string
  type: string
  desc: string
}

export interface CatalogItem {
  id: string
  name: string
  category: string
  sub_category: string
  datasource: 'akshare' | 'baostock'
  func_name: string
  description: string
  symbol_format?: string | null
  params: CatalogParam[]
  returns: CatalogReturn[]
  sample_params: Record<string, unknown>
  sample_code: string
  tags: string[]
  doc_url?: string
  status: 'ok' | 'broken' | 'missing' | 'unchecked'
  status_message?: string | null
  verified_at?: string | null
  returns_verified?: boolean
  returns_note?: string
}

export interface CatalogCategoryMeta {
  key: string
  label: string
  icon?: string
}

export interface CatalogCheckSummary {
  total: number
  ok: number
  broken: number
  missing: number
  unchecked: number
  last_checked_at: string | null
  stale_count: number
  removed_count: number
}

export interface CatalogItemsResp {
  items: CatalogItem[]
  total: number
  categories: CatalogCategoryMeta[]
  check: CatalogCheckSummary
}

export interface CatalogTree {
  categories: Array<{
    key: string
    label: string
    icon?: string
    count: number
    sub_groups: Array<{ key: string; items: Array<{ id: string; name: string; datasource: string }> }>
  }>
  total: number
}

export interface TryResult {
  columns: Array<{ name: string; dtype: string }>
  rows: string[][]
  shape: [number, number]
  elapsed_ms: number
  note?: string
  cached: boolean
  truncated: { rows: boolean; cols: boolean }
}

export interface TryRequest {
  params: Record<string, unknown>
  force?: boolean
}

// ======================================================================
// 模块 3 · 策略回测（阶段 2）
//
// 数据全部来自 `data/backtest/{strategy}/{run_id}/`（文件契约），
// 前端只读展示，不触发回测、不做任务队列。
// ======================================================================
export type BacktestStatus = 'success' | 'running' | 'failed' | 'incomplete'

/** 策略注册表条目 */
export interface BacktestStrategy {
  name: string
  display_name: string
  version: string
  description: string
  impl_path: string
  /** false = 数据库里有登记但代码已删，历史结果仍可看但跑不了 */
  in_code: boolean
  params: Record<string, unknown>
}

/** 列表页的一行 */
export interface BacktestRunSummary {
  strategy: string
  run_id: string
  tag: string
  status: BacktestStatus
  has_metrics: boolean
  is_out_sample: boolean
  /** ★ true = mock 假产物（前端联调用），必须显式标出，否则会和真实结果混在一起 */
  is_mock: boolean
  /**
   * ★ 非空 = 这个 run 是参数寻优的子运行，值就是那个 sweep_id。
   * 一次 sweep 会生成 12 个 run（每组一个），它们与真实回测产物结构一样 ——
   * 不标出来，回测列表页会被这些"格子"淹没。
   */
  sweep_id: string | null
  /** 该 run 在 sweep 网格里的组序号（1 起） */
  combo_index: number | null
  /**
   * ★ false = 明细表（trades/positions/orders）未落盘。
   * 此时 `trade_count` 仍有值但交易记录页是空的 —— 必须据此给出提示，
   * 否则用户会以为数据坏了。
   */
  detail_written: boolean
  interval_name: string
  start_date: string | null
  end_date: string | null
  preheat_start: string | null
  strategy_version: string
  params: Record<string, unknown>
  initial_cash: number | null
  max_positions: number | null
  benchmark: string | null
  created_at: string
  finished_at: string
  cost_seconds: number | null
  trading_days: number | null
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  win_rate: number | null
  trade_count: number | null
  /** ★ 规格强制展示：年化交易成本 */
  annual_cost_ratio: number | null
  /** ★ 规格强制展示：最大单笔亏损 */
  max_single_loss: number | null
  /** 非空 = 调试运行（只跑了几只标的），指标**没有可比性** */
  debug_symbols: string[] | null
}

export interface BacktestRunsResp {
  items: BacktestRunSummary[]
  total: number
  truncated: boolean
  strategies: BacktestStrategy[]
  sort: string
  desc: boolean
}

/** metrics.json 的字段（后端 `services/metrics.py` 的口径） */
export interface BacktestMetrics {
  initial_cash?: number | null
  final_asset?: number | null
  trading_days?: number | null
  years?: number | null
  total_return?: number | null
  annual_return?: number | null
  max_drawdown?: number | null
  max_drawdown_start?: string | null
  max_drawdown_end?: string | null
  annual_volatility?: number | null
  sharpe?: number | null
  calmar?: number | null
  trade_count?: number | null
  win_count?: number | null
  lose_count?: number | null
  win_rate?: number | null
  payoff_ratio?: number | null
  expectancy?: number | null
  avg_win?: number | null
  avg_lose?: number | null
  max_single_loss?: number | null
  max_single_loss_amount?: number | null
  max_single_win?: number | null
  avg_hold_days?: number | null
  total_cost?: number | null
  annual_cost_ratio?: number | null
  cost_to_gross_profit?: number | null
  turnover_annual?: number | null
}

export interface MonthlyReturn {
  year: number
  month: number
  /** 该月收益率；null = 该月无数据 */
  return: number | null
}

/** 详情页顶部的语义化提示（调试运行 / 样本外 / 无成交 …） */
export interface RunNote {
  level: 'danger' | 'warning' | 'info'
  text: string
}

export interface RunFile {
  name: string
  bytes: number
  human: string
}

export interface BacktestRunDetail extends BacktestRunSummary {
  config: Record<string, unknown>
  metrics: BacktestMetrics
  monthly_returns: MonthlyReturn[]
  files: RunFile[]
  rows: {
    equity: number
    trades: number | null
    positions: number | null
    orders: number | null
  }
  notes: RunNote[]
}

/** 净值曲线（列式返回） */
export interface EquityCurve {
  count: number
  /** 降采样前的原始点数 */
  downsampled_from: number
  downsampled: boolean
  start_date: string | null
  end_date: string | null
  series: {
    date: string[]
    nav?: Array<number | null>
    benchmark_nav?: Array<number | null>
    drawdown?: Array<number | null>
    cash?: Array<number | null>
    market_value?: Array<number | null>
    position_count?: Array<number | null>
  }
}

export interface TradeRow {
  symbol: string
  entry_date: string
  entry_price: number
  shares: number
  entry_amount: number
  entry_cost: number
  exit_date: string
  exit_price: number
  exit_amount: number
  exit_cost: number
  cost: number
  gross_pnl: number
  net_pnl: number
  net_return: number
  hold_days: number
  entry_reason: string
  /** 可能是多值：`time_stop|take_profit` */
  exit_reason: string
}

export interface TradesResp {
  items: TradeRow[]
  total: number
  page: number
  page_size: number
  pages: number
  /** 统计口径是**筛选后的全量**，不是当前页 */
  summary: {
    trade_count: number
    net_pnl: number | null
    win_rate: number | null
    avg_net_return: number | null
    total_cost: number | null
  }
  filters: { exit_reasons: string[] }
}

export interface OrderRow {
  signal_date: string
  exec_date: string
  symbol: string
  side: string
  shares: number | null
  price: number | null
  amount: number | null
  status: string
  reason: string
  note: string
}

export interface OrdersResp {
  items: OrderRow[]
  total: number
  shown?: number
  columns?: string[]
}

export interface CompareSeries {
  strategy: string
  run_id: string
  tag: string
  label: string
  debug: boolean
  total_return: number | null
  max_drawdown: number | null
  start_date?: string
  end_date?: string
  nav: Array<number | null>
}

export interface CompareResp {
  dates: string[]
  series: CompareSeries[]
  count: number
}

// ======================================================================
// 模块 3 · 参数寻优（阶段 2）
//
// 产物在 `data/sweep/{sweep_id}/`（与回测产物 `data/backtest/` 分开）：
//   grid.json / results.parquet / runs.json / summary.md
// 同样只读展示。一个 sweep = 一次网格扫描，每组参数跑一次完整回测，
// 所以 sweep 只存**索引**（组序号 → run_id），净值/交易仍在 backtest 目录。
// ======================================================================

/** 列表页的一行 */
export interface SweepSummary {
  sweep_id: string
  strategy: string
  tag: string
  description: string
  objective: string
  interval_name: string
  start_date: string | null
  end_date: string | null
  /** 网格总组数 */
  combo_count: number
  /** 已完成的组数 */
  done_count: number
  /** ★ false = 还在跑（或已中断）。中途落盘所以能看到部分结果 */
  is_complete: boolean
  missing_files: string[]
  /** 非空 = 调试运行（只跑了几只标的），**结论不可用于评估策略** */
  debug_symbols: string[] | null
  created_at: string
}

/** results.parquet 的一行：一组参数 + 它的指标 */
export interface SweepResultItem {
  combo_index: number
  run_id: string | null
  failed: boolean
  /** ★ 敏感性分析：该点正交邻居（上下左右）的 objective 均值 —— 判"高原"的依据 */
  neighbor_mean: number | null
  /** ★ true = 尖峰组合（自己不错但邻居跟不上 = 噪声拟合），不建议使用 */
  is_spike: boolean
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  calmar: number | null
  annual_cost_ratio: number | null
  trade_count: number | null
  win_rate: number | null
  payoff_ratio: number | null
  avg_hold_days: number | null
  max_single_loss: number | null
  cost_seconds: number | null
  /** 网格参数名 → 取值（动态，由 grid 决定） */
  [param: string]: unknown
}

export interface SweepInterval {
  name: string
  start: string
  end: string
  preheat_start: string
  is_out_sample: boolean
}

export interface SweepDetail {
  sweep_id: string
  strategy: string
  description: string
  objective: string
  /** 参数名 → 候选值列表 */
  params_grid: Record<string, Array<string | number>>
  param_names: string[]
  /** 敏感性分析的轴：x = 横轴参数，series = 分组参数 */
  sensitivity_axes: { x?: string; series?: string | null }
  combo_count: number
  interval: SweepInterval
  meta: {
    tag?: string
    created_at?: string
    symbols?: string[] | null
    config_path?: string
    objectives?: string[]
    spike_ratio?: number
  }
  items: SweepResultItem[]
  /** 组序号（字符串）→ run_id，供跳回测详情页 */
  runs: Record<string, string>
  /** 自动生成的结论文本（markdown 源文） */
  summary_md: string
}


// ======================================================================
// 模块 2 · 因子库（阶段 3）
//
// 三层数据来源，前端不触发任何计算：
//   定义 → backend/factors/registry.yaml（人工维护）
//   状态 → data/factor/_manifest.json（由 python -m factors.calc 写入）
//   分析 → data/factor/analysis/{name}.json（由 python -m factors.analyze 写入）
// ======================================================================

/** 分类状态：ready 可计算 / planned 待开发 / blocked 依赖的数据源没有 */
export type FactorCategoryStatus = 'ready' | 'planned' | 'blocked'

/**
 * 因子的落盘状态。⚠️ 三态必须分开处理：
 *   calculated     —— 产物文件在 + 清单有条目
 *   unindexed      —— 文件在但清单没记录（行数/区间未知，不代表不能用）
 *   orphan         —— 清单有条目但文件不在 → 显示"已计算"却点不开，必须标红
 */
export type FactorState =
  | 'calculated'
  | 'not_calculated'
  | 'unindexed'
  | 'orphan'
  | 'error'

export interface FactorCategory {
  key: string
  cname: string
  status: FactorCategoryStatus
  available: boolean
  desc: string
  note: string
  factor_count: number
  calculated_count: number
}

export interface FactorGroupRow {
  /** 组号从 1 开始，数值越大 = 因子值越大 */
  group: number
  avg_daily: number
  /** 算术年化（日均 × 252）—— 不是复合年化 */
  annual_return: number
  cum_return: number
  avg_size: number
  turnover_daily: number
  cost_annual: number
  annual_return_net: number
}

export interface FactorLongShort {
  avg_daily?: number
  annual_gross?: number
  annual_net?: number
  turnover_daily?: number
  cost_annual?: number
  cum_return?: number
}

export interface FactorIcSummary {
  n_dates?: number
  mean?: number
  std?: number
  /** ICIR = IC 均值 / IC 标准差（单期口径，不是年化） */
  ir?: number
  t_stat?: number
  win_rate?: number
  abs_mean?: number
}

export interface FactorAnalysisSummary {
  generated_at: string
  interval: Record<string, unknown>
  settings: {
    groups?: number
    horizon?: number
    avg_universe_size?: number
    mean_round_trip_cost?: number
    universe?: { calendar?: string; first_trade_date_source?: string; warning?: string }
  }
  ic: FactorIcSummary
  group_summary: FactorGroupRow[]
  monotonicity?: number | null
  long_short: FactorLongShort
  warnings: string[]
}

/** 因子列表 / 详情的一行 */
export interface FactorItem {
  name: string
  cname: string
  category: string
  /** 1 越大越好 / -1 越小越好 / 0 中性 —— 这是**先验假设**，不是结论 */
  direction: number
  impl: string
  params: Record<string, unknown>
  params_key: string
  data_deps: string[]
  tags: string[]
  /** 量纲说明（定义在 registry.yaml，前端不硬编码） */
  unit: string
  desc: string
  enabled: boolean

  state: FactorState
  calculated: boolean
  rows?: number | null
  non_null?: number | null
  nan_ratio?: number | null
  symbols?: number | null
  date_start?: string | null
  date_end?: string | null
  computed_at: string
  seconds?: number | null
  size_bytes?: number | null
  file: string
  /** ⚠️ 非空 = 调试运行，截面/IC 结论不可用 */
  debug_symbols?: string[] | null
  warnings: string[]
  has_analysis: boolean
  analysis_summary?: FactorAnalysisSummary
}

export interface FactorListResp {
  categories: FactorCategory[]
  factors: FactorItem[]
  total: number
  calculated: number
  manifest_updated_at: string
  registry_file: string
  notes: string[]
}

export interface FactorSeriesResp {
  name: string
  cname: string
  category: string
  direction: number
  /** symbols = 多标的对比；cross_section_aggregate = 全市场每日截面聚合 */
  mode: 'symbols' | 'cross_section_aggregate'
  unit: string
  dates: string[]
  series: Record<string, Array<number | null>>
  count?: number[]
  downsampled_from: number
  symbols?: string[]
  mode_note?: string
}

export interface FactorHistogram {
  bins: number[]
  counts: number[]
  clip_note: string
}

export interface FactorExtreme {
  symbol: string
  value: number
}

export interface FactorCrossSectionResp {
  name: string
  cname: string
  category: string
  direction: number
  day: string
  unit: string
  count: number
  stats: { mean: number; std: number; min: number; max: number; median: number }
  quantiles: Record<string, number>
  histogram: FactorHistogram
  extremes: { bottom: FactorExtreme[]; top: FactorExtreme[] }
}

export interface FactorLayeredResp {
  name: string
  cname: string
  direction: number
  interval: { start?: string; end?: string; dates_used?: number; dates_skipped_too_few?: number }
  settings: FactorAnalysisSummary['settings']
  group_summary: FactorGroupRow[]
  monotonicity?: number | null
  long_short: FactorLongShort
  dates: string[]
  /** 每行是 [G1, G2, ..., Gn, LS] 的累计净值 */
  nav: number[][]
  nav_columns: string[]
  warnings: string[]
  notes: string[]
}

export interface FactorIcResp {
  name: string
  cname: string
  direction: number
  interval: { start?: string; end?: string; dates_used?: number }
  settings: FactorAnalysisSummary['settings']
  summary: FactorIcSummary
  series: Array<{ date: string; ic: number | null }>
  warnings: string[]
  notes: string[]
}

export interface FactorCheckItem {
  name: string
  cname: string
  category: string
  category_status: FactorCategoryStatus
  enabled: boolean
  impl: string
  status: 'ok' | 'broken'
  message: string
  state: FactorState
  date_start?: string | null
  date_end?: string | null
  rows?: number | null
  has_analysis: boolean
}

export interface FactorCheckResp {
  total: number
  ok: number
  broken: number
  items: FactorCheckItem[]
  checked_at: string
}

// ======================================================================
// 模块 4 · 模拟仿真（阶段 4）
// ======================================================================

/** 账户行（paper_account 表 + 列表摘要附加字段） */
export interface PaperAccountSummary {
  id: string
  name: string
  strategy: string | null
  init_cash: number
  cash: number | null
  status: string
  start_date: string | null
  last_run_date: string | null
  created_at: string
  /** 以下为列表接口附加的最新净值摘要（无产物时缺省） */
  data_as_of?: string
  nav?: number
  drawdown?: number
  position_count?: number
}

/** 净值曲线采样点 */
export interface PaperNavPoint {
  date: string
  nav: number
  benchmark_nav: number | null
  drawdown: number | null
  total_asset?: number
  cash?: number
  market_value?: number
  position_count?: number
}

/** 最新持仓行（引擎 positions 快照列直出） */
export interface PaperPositionRow {
  date: string
  symbol: string
  shares: number
  entry_date: string
  entry_price: number
  close: number | null
  market_value: number | null
  unrealized_pnl: number | null
  hold_days: number
}

/** 最近交易行（引擎 trades 契约列直出，键动态） */
export type PaperTradeRow = Record<string, string | number | null>

/** 跑批任务状态 */
export interface PaperJobState {
  schedule: {
    job_id: string
    name: string
    trigger: string
    next_run_time: string | null
    enabled: boolean
    in_scheduler: boolean
  } | null
  last_log: {
    run_date: string
    status: string
    duration: number | null
    message: string | null
    created_at: string
  } | null
}

/** 模拟盘详情聚合（GET /paper/{id}/nav） */
export interface PaperOverview {
  account: PaperAccountSummary
  empty: boolean
  note?: string
  data_as_of?: string
  start_date?: string
  metrics?: Record<string, number | string | null>
  finished_at?: string | null
  nav_series?: PaperNavPoint[]
  latest_positions?: PaperPositionRow[]
  recent_trades?: PaperTradeRow[]
  job?: PaperJobState
}

/** 手动触发响应 */
export interface PaperTriggerResp {
  triggered: boolean
  note: string
}

// ======================================================================
// 模块 5 · 用户提醒（阶段 5）
// ======================================================================

/** 提醒类型 */
export type AlertAction =
  | 'open_position'
  | 'close_position'
  | 'daily_summary'
  | 'test'
  | 'job_failed'

/** 提醒状态 */
export type AlertStatus = 'pending' | 'sent' | 'failed' | 'skipped'

/** 提醒日志行（alert_log 表直出，action_cn 由后端 _decorate 补） */
export interface AlertLogRow {
  id: number
  event_key: string
  account_id: string | null
  symbol: string | null
  action: AlertAction | string
  /** 中文名由后端给（前端不做 if-else 映射 —— 同一口径只能有一处定义） */
  action_cn: string
  level: 'info' | 'warning' | 'error' | string
  title: string | null
  content: string | null
  channel: string
  status: AlertStatus | string
  retry_count: number
  error_message: string | null
  created_at: string
  sent_at: string | null
}

/** 日志分页响应（GET /alerts/logs） */
export interface AlertLogsResp {
  total: number
  items: AlertLogRow[]
  limit: number
  offset: number
  /** 各状态条数，**不受当前筛选影响**（前端标签直接显示，省一次请求） */
  status_counts: Record<string, number>
}

/** 发送结果（/test 与 /resend 的 data 体） */
export interface AlertSendResp {
  dispatched?: boolean
  ok: boolean
  skipped?: boolean
  reason?: string
  alert_id?: number
  status?: string
  errcode?: number | null
  message?: string
  channel?: string
  waited?: number
}

/** 渠道状态（GET /alerts/status）——凭证一律脱敏，无原文 */
export interface AlertChannelStatus {
  channel: 'webhook' | 'app' | string
  enabled: boolean
  configured: boolean
  rate_limit_per_minute: number
  webhook_configured: boolean
  webhook_key_masked: string
  app_configured: boolean
  touser: string
  rate: {
    name: string
    min_interval: number
    max_calls_per_minute: number
    calls_in_window: number
    window_seconds: number
    total_calls: number
    total_wait_seconds: number
    max_wait_seconds: number
  }
  templates_file: string
  template_count: number
  /** 企微群「自定义关键词」——消息不含它会被静默丢弃 */
  required_keyword: string
}

/** 模板预览项（GET /alerts/templates） */
export interface AlertTemplatePreview {
  action: string
  action_cn: string
  title: string
  body: string
  level: string
  sample: {
    title: string
    content: string
    level: string
  }
}

/** 枚举字典项 */
export interface AlertEnumItem {
  value: string
  label: string
}

/** 枚举字典（GET /alerts/meta）——中文名一律由后端给 */
export interface AlertMeta {
  actions: AlertEnumItem[]
  statuses: AlertEnumItem[]
  levels: AlertEnumItem[]
  exit_reasons: AlertEnumItem[]
  channel: string
}

// ======================================================================
// 模块 1 · 本地数据（磁盘上已经落盘的数据维度）
//
// 与上面的接口目录是**两个问题**：接口目录回答"数据源能调什么"，
// 这里回答"本地已经存了什么"。真相是磁盘，不是任何登记表。
// ======================================================================
/** 数据来源：原始下载 / 本地派生 / 本地状态 */
export type LocalOrigin = 'downloaded' | 'derived' | 'state'

/** 维度上的一条补充信息（键值对） */
export interface LocalDimensionExtra {
  label: string
  value: string
}

/** 一个本地数据维度 */
export interface LocalDimension {
  key: string
  name: string
  group: string
  origin: LocalOrigin
  origin_label: string
  desc: string
  /** 一行代表什么 —— 看数字前先看单位 */
  grain: string
  /** 谁把它落盘的（工具 / 服务） */
  producer: string
  path: string
  /** 覆盖区间的口径名（如"覆盖（报告期）"） */
  date_label: string
  /** 行数口径说明：行数与文件数不同源时必须写清楚（如"仅净值表"） */
  rows_scope: string
  extras: LocalDimensionExtra[]
  available: boolean
  files: number
  bytes: number
  human: string
  rows: number
  date_start: string | null
  date_end: string | null
  factor_count: number
  factor_names: Array<{ name: string; cname: string }>
  /** 不可用时的原因（本地没有 / 回补失败 / 读取出错） */
  error: string | null
}

/** 维度分组 */
export interface LocalGroup {
  key: string
  label: string
  desc: string
}

/** 本地数据清单（GET /catalog/local/datasets） */
export interface LocalDatasetsResp {
  groups: LocalGroup[]
  origins: Record<string, string>
  datasets: LocalDimension[]
  summary: {
    total: number
    available: number
    missing: number
    files: number
    bytes: number
    human: string
    rows: number
    rows_human: string
    data_root: string
  }
  /** 本次扫描耗时（首次约 12 秒，之后走缓存） */
  elapsed_ms: number
  scanned_at: string
  ttl_seconds: number
}

/** 某个维度的示例数据（GET /catalog/local/datasets/{key}/sample） */
export interface LocalSample {
  key: string
  dataset: string
  grain: string
  columns: Array<{ name: string; dtype: string }>
  /** 字符串二维数组 —— 前端用列下标当 prop */
  rows: string[][]
  shown_rows: number
  shown_columns: number
  total_rows: number
  /** 裁剪说明（列被裁 / 示例只是一个切片） */
  note: string
  source: {
    kind: string
    file: string
    caption: string
    table: string
    /** 示例取自哪只标的（有则前端明确标出来，避免"这是谁的数"靠猜） */
    symbol?: string
  }
  /** 仅 SQLite 维度有：库里的表清单 */
  tables?: Array<{ name: string; rows: number }>
}
