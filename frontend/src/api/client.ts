/**
 * axios 封装。
 *
 * 约定：
 *  - 前端一律用相对路径（/api/...），开发环境由 Vite proxy 转发到 :8000
 *  - 自动拆包：直接返回 data 字段，业务代码不用每次都 .data.data
 *  - 错误统一提示：展示后端返回的中文 message
 */
import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import type { ApiResponse } from '@/types/api'

const instance: AxiosInstance = axios.create({
  baseURL: '/',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

/** 业务错误（后端返回 code != 0） */
export class BizError extends Error {
  code: number
  detail?: unknown

  constructor(code: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'BizError'
    this.code = code
    this.detail = detail
  }
}

instance.interceptors.response.use(
  (response) => response,
  (error) => {
    // 网络层错误
    if (!error.response) {
      const msg = error.code === 'ECONNABORTED'
        ? '请求超时，后端可能正在处理长任务'
        : '无法连接后端服务，请确认 uvicorn 已启动（:8000）'
      ElMessage.error(msg)
      return Promise.reject(new BizError(-1, msg))
    }

    const body = error.response.data
    // 后端统一错误体：{ code, message, detail }
    if (body && typeof body === 'object' && 'message' in body) {
      const message = String((body as { message: unknown }).message)
      const code = Number((body as { code?: unknown }).code ?? error.response.status)
      ElMessage.error(message)
      return Promise.reject(new BizError(code, message, (body as { detail?: unknown }).detail))
    }

    const fallback = `请求失败（HTTP ${error.response.status}）`
    ElMessage.error(fallback)
    return Promise.reject(new BizError(error.response.status, fallback))
  },
)

/** 发起请求并拆出 data */
async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const res = await instance.request<ApiResponse<T>>(config)
  const body = res.data

  if (body && typeof body === 'object' && 'code' in body) {
    if (body.code !== 0) {
      ElMessage.error(body.message || '请求失败')
      throw new BizError(body.code, body.message || '请求失败', body.data)
    }
    return body.data as T
  }
  return body as unknown as T
}

export const http = {
  get: <T>(url: string, params?: Record<string, unknown>) =>
    request<T>({ method: 'GET', url, params }),

  post: <T>(url: string, data?: unknown, params?: Record<string, unknown>) =>
    request<T>({ method: 'POST', url, data, params }),

  put: <T>(url: string, data?: unknown) => request<T>({ method: 'PUT', url, data }),

  delete: <T>(url: string, params?: Record<string, unknown>) =>
    request<T>({ method: 'DELETE', url, params }),
}

export default instance
