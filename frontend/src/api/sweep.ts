/**
 * 模块 3 · 参数寻优接口（阶段 2，**全部只读**）。
 *
 * 寻优由 CLI 跑（`python -m strategies.sweep --config sweeps/hold_tp.yaml`），
 * 结果落盘到 `data/sweep/{sweep_id}/`。前端只读文件，不触发计算 ——
 * 与回测页同一条纪律（技术架构.md 架构纪律 5「只读展示优先」）。
 *
 * ★ 为什么不给个"开始寻优"按钮：
 *   12~16 组 × 1115 天 × 全市场，跑一次几十分钟。放进 HTTP 请求链路会卡死服务。
 *   参数寻优**必须走 CLI**。
 */
import { http } from './client'
import type { SweepDetail, SweepSummary } from '@/types/api'

const BASE = '/api/v1/sweep'

/** 寻优任务列表（扫盘得到） */
export const getSweeps = () => http.get<SweepSummary[]>(`${BASE}/list`)

/** 寻优详情：网格定义 + 每组指标 + 敏感性分析 + 自动生成的结论 */
export const getSweepDetail = (sweepId: string) =>
  http.get<SweepDetail>(`${BASE}/${encodeURIComponent(sweepId)}`)
