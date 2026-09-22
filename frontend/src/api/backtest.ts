/**
 * 模块 3 · 策略回测接口（阶段 2，**全部只读**）。
 *
 * 回测由 CLI 跑（`python -m strategies.run`），结果落盘到
 * `data/backtest/{strategy}/{run_id}/`。前端只读文件，不触发计算 ——
 * 见 技术架构.md 架构纪律 5「只读展示优先」。
 */
import { http } from './client'
import type {
  BacktestRunDetail,
  BacktestRunsResp,
  BacktestStrategy,
  CompareResp,
  EquityCurve,
  OrdersResp,
  TradesResp,
} from '@/types/api'

const BASE = '/api/v1/backtest'

/** 已注册策略 */
export const getStrategies = () => http.get<BacktestStrategy[]>(`${BASE}/strategies`)

export interface RunListQuery {
  strategy?: string
  tag?: string
  status?: string
  sort?: string
  desc?: boolean
  limit?: number
  /** 是否滤掉参数寻优的子运行（默认 true；一次 sweep 会留十几个 run） */
  exclude_sweep?: boolean
}

/** 运行列表（扫盘得到，含每个 run 的关键指标） */
export const getRuns = (query: RunListQuery = {}) =>
  http.get<BacktestRunsResp>(`${BASE}/runs`, query as Record<string, unknown>)

/** 运行详情：配置快照 + 指标 + 月度收益 + 产物清单 + 提示 */
export const getRunDetail = (strategy: string, runId: string) =>
  http.get<BacktestRunDetail>(
    `${BASE}/runs/${encodeURIComponent(strategy)}/${encodeURIComponent(runId)}`,
  )

/** 净值曲线（服务端已做 LTTB 降采样） */
export const getEquity = (strategy: string, runId: string, maxPoints = 2000) =>
  http.get<EquityCurve>(
    `${BASE}/runs/${encodeURIComponent(strategy)}/${encodeURIComponent(runId)}/equity`,
    { max_points: maxPoints },
  )

export interface TradeQuery {
  page?: number
  page_size?: number
  symbol?: string
  exit_reasons?: string
  win?: 'win' | 'lose' | 'all'
  start?: string
  end?: string
  sort?: string
  desc?: boolean
}

/** 交易记录（筛选 + 分页） */
export const getTrades = (strategy: string, runId: string, query: TradeQuery = {}) =>
  http.get<TradesResp>(
    `${BASE}/runs/${encodeURIComponent(strategy)}/${encodeURIComponent(runId)}/trades`,
    query as Record<string, unknown>,
  )

/** 委托流水（含跌停/停牌顺延记录） */
export const getOrders = (
  strategy: string,
  runId: string,
  query: { status?: string; limit?: number } = {},
) =>
  http.get<OrdersResp>(
    `${BASE}/runs/${encodeURIComponent(strategy)}/${encodeURIComponent(runId)}/orders`,
    query as Record<string, unknown>,
  )

/** 多 run 净值对比（最多 6 个） */
export const compareRuns = (refs: Array<{ strategy: string; run_id: string }>, maxPoints = 2000) =>
  http.get<CompareResp>(`${BASE}/runs/compare`, {
    refs: refs.map((r) => `${r.strategy}:${r.run_id}`).join(','),
    max_points: maxPoints,
  })
