/**
 * 模块 2 · 因子库接口（阶段 3，**全部只读**）。
 *
 * 因子由 CLI 计算、有效性分析也由 CLI 跑：
 *   python -m factors.calc --category daily          # 算因子 → data/factor/{category}/{name}.parquet
 *   python -m factors.analyze --category daily       # 分层 + IC → data/factor/analysis/{name}.json
 *
 * ★ 为什么不给"开始计算"按钮：11 个因子 × 700 万行 ≈ 50s，再加上分层回测与
 *   逐日构建股票池要 3 分钟。放进 HTTP 请求链路会卡死服务。
 *   与回测页/寻优页同一条纪律（技术架构.md 架构纪律 5「只读展示优先」）。
 */
import { http } from './client'
import type {
  FactorCheckResp,
  FactorCrossSectionResp,
  FactorIcResp,
  FactorItem,
  FactorLayeredResp,
  FactorListResp,
  FactorSeriesResp,
} from '@/types/api'

const BASE = '/api/v1/factors'

/** 因子注册表列表（定义 + 计算状态） */
export const getFactors = (category?: string) =>
  http.get<FactorListResp>(BASE, category ? { category } : undefined)

/** 因子详情（定义 + 状态 + 分析摘要） */
export const getFactorDetail = (name: string) =>
  http.get<FactorItem>(`${BASE}/${encodeURIComponent(name)}`)

/**
 * 因子时序。
 * · 不给 symbols → 返回全市场每日截面聚合（均值 / 中位数 / 25~75 分位）
 * · 给 symbols   → 返回这些标的各自的因子值（最多 8 只）
 */
export const getFactorSeries = (
  name: string,
  params?: { symbols?: string; start?: string; end?: string; max_points?: number },
) => http.get<FactorSeriesResp>(`${BASE}/${encodeURIComponent(name)}/series`, params)

/** 某交易日的截面分布（直方图 + 分位数 + 极值标的） */
export const getFactorCrossSection = (
  name: string,
  params?: { day?: string; bins?: number; top_n?: number },
) => http.get<FactorCrossSectionResp>(`${BASE}/${encodeURIComponent(name)}/cross-section`, params)

/** 分层回测（分组累计净值 —— 判断因子有没有 alpha 的核心图） */
export const getFactorLayered = (name: string) =>
  http.get<FactorLayeredResp>(`${BASE}/${encodeURIComponent(name)}/layered`)

/** RankIC 序列与摘要 */
export const getFactorIc = (name: string, params?: { max_points?: number }) =>
  http.get<FactorIcResp>(`${BASE}/${encodeURIComponent(name)}/ic`, params)

/** 因子自检（导入 + 签名 + 冒烟调用） */
export const checkFactors = (smoke = true) =>
  http.get<FactorCheckResp>(`${BASE}/check`, { smoke })

/** 本地已有日线数据的标的（供标的检索选择器用） */
export const searchFactorSymbols = (q?: string, limit = 50) =>
  http.get<string[]>(`${BASE}/symbols`, { q, limit })
