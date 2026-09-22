/** 系统类接口（阶段 0）
 *
 * 清理记录（2026-09-18）：删掉 5 个从未被引用的包装函数 ——
 * `getBootInfo` / `getCalendar` / `getCalendarDays` / `getStorage` /
 * `getDatasourceHealth`。它们都不是后续阶段的预留接口：
 *   · `/api/v1/system/calendar`、`/storage`、`/datasources/health` 的数据
 *     已由 `/api/health`（`health.calendar` / `health.storage` /
 *     `health.datasources`）统一返回，SystemView 用的是那一份；
 *   · `/system/calendar/days` 是给"按区间列交易日"用的，前端不做日期计算
 *     （模块 3 前端只读回测结果文件），没有消费者。
 */
import { http } from './client'
import type {
  DatasourceMatrix,
  HealthData,
  JobsData,
  SchedulerStatus,
} from '@/types/api'

export interface BootInfo {
  name: string
  version: string
  stage: string
  docs: string
  health: string
  boot: Record<string, unknown>
}

/** 健康检查（快照，不触发网络探测） */
export const getHealth = () => http.get<HealthData>('/api/health')

/** 健康检查（深度：会真实请求数据源，慢） */
export const getHealthDeep = (deep = true) =>
  http.get<HealthData>('/api/health', { deep })

// ---------- 交易日历 ----------
export const refreshCalendar = () =>
  http.post<Record<string, unknown>>('/api/v1/system/calendar/refresh')

// ---------- 调度 ----------
export const getJobs = (limit = 20) =>
  http.get<JobsData>('/api/v1/system/jobs', { limit })

/** ⏳ 预留（阶段 4 · 模拟仿真）：前端"手动跑一次任务"按钮 */
export const runJob = (jobId: string, runDate?: string) =>
  http.post<Record<string, unknown>>(
    `/api/v1/system/jobs/${jobId}/run`,
    undefined,
    runDate ? { run_date: runDate } : undefined,
  )

export const triggerCatchUp = () =>
  http.post<Record<string, unknown>>('/api/v1/system/catch-up')

// ---------- 数据源 ----------
export const getDatasourceMatrix = () =>
  http.get<DatasourceMatrix>('/api/v1/datasources')

export const probeDatasource = (name: string) =>
  http.post<Record<string, unknown>>(`/api/v1/datasources/${name}/probe`)

// ---------- 配置 ----------
/** ⏳ 预留（阶段 2 · 参数寻优页）：展示本轮生效的配置，避免"结果对不上配置" */
export const getConfig = () => http.get<Record<string, unknown>>('/api/v1/system/config')

/** ⏳ 预留（阶段 2）：改完 YAML 后热重载 */
export const reloadConfig = () =>
  http.post<Record<string, unknown>>('/api/v1/system/config/reload')

export type { SchedulerStatus }
