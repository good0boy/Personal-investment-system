/**
 * 模块 4 · 模拟仿真接口（阶段 4）。
 *
 * 读路径只读（架构纪律 5）。唯一的写路径是「手动触发跑批」——
 * 它只是把任务扔给调度器立即执行（增量回补约 30 分钟 + 重算 45s，
 * HTTP 同步等必超时），结果靠轮询 `last_run_date` 与任务日志观察。
 */
import { http } from './client'
import type { PaperAccountSummary, PaperOverview, PaperTriggerResp } from '@/types/api'

const BASE = '/api/v1/paper'

/** 账户列表（首次访问幂等初始化默认账户） */
export const getPaperAccounts = () => http.get<PaperAccountSummary[]>(`${BASE}/accounts`)

/** 详情聚合：账户 + 净值 + 指标 + 最新持仓 + 最近交易 + 任务状态 */
export const getPaperOverview = (accountId: string) =>
  http.get<PaperOverview>(`${BASE}/${encodeURIComponent(accountId)}/nav`)

/** 手动触发一次跑批（立即入队，非同步执行） */
export const runPaperNow = (accountId: string) =>
  http.post<PaperTriggerResp>(`${BASE}/${encodeURIComponent(accountId)}/run`)
