/**
 * 全局后端健康状态。
 *
 * 侧边栏顶部的状态标签和「系统状态」页共用一份数据，
 * 避免通过 router-view 向下传 props（那会把 health 变成其他页面的 DOM 属性）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getHealth } from '@/api/system'
import type { HealthData } from '@/types/api'

export const useHealthStore = defineStore('health', () => {
  const health = ref<HealthData | null>(null)
  const loading = ref(false)
  const lastError = ref<string>('')

  async function refresh() {
    loading.value = true
    lastError.value = ''
    try {
      health.value = await getHealth()
    } catch (e) {
      health.value = null
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  return { health, loading, lastError, refresh }
})
