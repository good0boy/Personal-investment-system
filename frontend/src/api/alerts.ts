/**
 * 模块 5 · 用户提醒接口（阶段 5）。
 *
 * 读路径只读（架构纪律 5）。两个写路径：
 *   - `POST /test`  发链路测试消息 —— **同步**返回结果。
 *     与模块 4 的 `/run` 相反：发消息正常 200ms，验
 *     收标准就是「点一下，消息到手机」，返回"已入队"等于没验证。
 *   - `POST /{id}/resend` 重发失败记录 —— 也是同步。
 */
import { http } from './client'
import type {
  AlertChannelStatus,
  AlertLogsResp,
  AlertMeta,
  AlertSendResp,
  AlertTemplatePreview,
} from '@/types/api'

const BASE = '/api/v1/alerts'

/** 提醒日志（分页 + 按状态/类型筛选） */
export const getAlertLogs = (params: {
  limit?: number
  offset?: number
  status?: string
  action?: string
} = {}) => http.get<AlertLogsResp>(`${BASE}/logs`, params)

/** 发一条链路测试消息（同步返回发送结果） */
export const sendAlertTest = (operator = '手动测试') =>
  http.post<AlertSendResp>(`${BASE}/test`, undefined, { operator })

/** 重发一条失败的提醒 */
export const resendAlert = (alertId: number) =>
  http.post<AlertSendResp>(`${BASE}/${alertId}/resend`)

/** 渠道状态（当前渠道 / 凭证脱敏 / 限流 / 模板数） */
export const getAlertStatus = () => http.get<AlertChannelStatus>(`${BASE}/status`)

/** 消息模板预览（原始定义 + 样例渲染） */
export const getAlertTemplates = () => http.get<AlertTemplatePreview[]>(`${BASE}/templates`)

/** 枚举字典（动作 / 状态 / 级别的中文名） */
export const getAlertMeta = () => http.get<AlertMeta>(`${BASE}/meta`)
