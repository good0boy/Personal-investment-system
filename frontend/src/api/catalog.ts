/** 模块 1 · 接口目录（阶段 1）
 *
 * 只保留 CatalogView 真正在用的 3 个调用。
 * 2026-09-18 清理：删掉了 `getCatalogTree` / `getCatalogCheckStatus` /
 * `markCatalogBroken` 三个从未被任何组件引用的包装函数。
 * 判断依据：阶段 2~5 是回测/因子/模拟/提醒，都不会用到它们，**不属于预留接口**。
 *   · 目录树前端本来就用 `/items` 一次拉全后本地算，`/tree` 是冗余的第二条路径；
 *   · 自检汇总已包含在 `/items` 的 `check` 字段里；
 *   · `markCatalogBroken` 后端接口仍在（`POST /catalog/check/broken/{id}`），
 *     需要验证"自检标红"链路时用 /docs 或 curl 手动调即可，不必占用前端代码。
 */
import { http } from './client'
import type { CatalogItemsResp, TryRequest, TryResult } from '@/types/api'

/** 全部接口定义（含自检状态）—— 首屏一次拉全，本地过滤 */
export const getCatalogItems = (category?: string) => {
  const params = category ? { category } : undefined
  return http.get<CatalogItemsResp>('/api/v1/catalog/items', params)
}

/** 在线试跑：真实调用数据源（慢，1~5s；相同参数 5 分钟内走缓存） */
export const tryCatalog = (itemId: string, body: TryRequest) =>
  http.post<TryResult>(`/api/v1/catalog/${itemId}/try`, body)

/** 手动触发自检（liveSample>0 时含真实抽样调用，上限 20） */
export const runCatalogCheck = (liveSample = 0) =>
  http.post<Record<string, unknown>>('/api/v1/catalog/check', undefined, {
    live_sample: liveSample,
  })
