<template>
  <el-container class="layout">
    <!-- 侧边栏 -->
    <el-aside :width="sidebarWidth" class="sidebar">
      <div class="brand">
        <svg class="brand-logo" viewBox="0 0 32 32" aria-hidden="true">
          <rect width="32" height="32" rx="7" fill="#185FA5" />
          <path
            d="M8 21l4.5-6 3.5 4.5L23 11"
            fill="none"
            stroke="#fff"
            stroke-width="2.4"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle cx="23" cy="11" r="2.4" fill="#F09595" />
        </svg>
        <div class="brand-text">
          <p class="brand-name">A股投资本地服务</p>
          <p class="brand-stage">数据分类 · 策略回测</p>
        </div>
      </div>

      <el-menu
        :default-active="activePath"
        :collapse="collapsed"
        class="menu"
        router
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <template #title>
            <span class="menu-label">{{ m.title }}</span>
            <el-tag
              v-if="!m.done"
              size="small"
              type="info"
              effect="plain"
              class="menu-tag"
            >
              {{ m.stage }}
            </el-tag>
          </template>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-footer">
        <el-button link size="small" @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </el-button>
      </div>
    </el-aside>

    <el-container>
      <!-- 顶部栏 -->
      <el-header height="56px" class="header">
        <div class="header-left">
          <h1 class="header-title">{{ currentTitle }}</h1>
          <span v-if="currentStage" class="header-stage">
            {{ currentStage }}
            <el-tag v-if="currentDone" size="small" type="success" effect="plain">已完成</el-tag>
            <el-tag v-else size="small" type="warning" effect="plain">待开发</el-tag>
          </span>
        </div>

        <div class="header-right">
          <el-tooltip :content="healthTooltip" placement="bottom">
            <el-tag :type="healthTagType" effect="light" size="small">
              <el-icon class="tag-icon"><component :is="healthIcon" /></el-icon>
              {{ healthLabel }}
            </el-tag>
          </el-tooltip>

          <el-tooltip content="重新读取后端状态" placement="bottom">
            <el-button size="small" :loading="loading" @click="refresh">
              <el-icon><Refresh /></el-icon>
            </el-button>
          </el-tooltip>

          <el-button size="small" tag="a" href="/docs" target="_blank">
            API 文档
          </el-button>
        </div>
      </el-header>

      <!-- 内容区 -->
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useHealthStore } from '@/stores/health'

const route = useRoute()
const router = useRouter()

const collapsed = ref(false)
const sidebarWidth = computed(() => (collapsed.value ? '64px' : '232px'))

const healthStore = useHealthStore()
const { health, loading } = storeToRefs(healthStore)

interface MenuItem {
  path: string
  title: string
  icon?: string
  stage?: string
  done?: boolean
  description?: string
}

const menus = computed<MenuItem[]>(() => {
  return router
    .getRoutes()
    .filter(
      (r) =>
        r.meta?.title &&
        !r.meta?.hideInMenu &&
        r.path !== '/:pathMatch(.*)*',
    )
    .map((r) => ({
      path: r.path,
      title: r.meta.title as string,
      icon: r.meta.icon,
      stage: r.meta.stage,
      done: r.meta.done,
      description: r.meta.description,
    }))
    // ★ 按**开发阶段**排序，不能按路径字母序。
    //   路径字母序会排成 用户提醒 / 策略回测 / 数据分类 / 因子库 / 模拟仿真 / 系统状态，
    //   完全打乱开发顺序，也让人找不到"最该看的那个页面"。
    .sort((a, b) => stageOrder(a.stage) - stageOrder(b.stage) || a.path.localeCompare(b.path))
})

/** 从 `阶段 N` 里取出 N 用于排序；没有 stage 的排最后 */
function stageOrder(stage?: string): number {
  if (!stage) return 999
  const m = stage.match(/\d+/)
  return m ? Number(m[0]) : 999
}

const activePath = computed(() => route.path)
const currentTitle = computed(() => route.meta?.title || 'A股投资本地服务')
const currentStage = computed(() => route.meta?.stage || '')
const currentDone = computed(() => Boolean(route.meta?.done))

// ---------- 健康指示 ----------
const healthStatus = computed(() => health.value?.status ?? 'unknown')

const healthLabel = computed(() => {
  const map: Record<string, string> = {
    ok: '后端正常',
    degraded: '部分降级',
    error: '后端异常',
    unknown: '未连接',
  }
  return map[healthStatus.value] ?? '未知'
})

const healthTagType = computed(() => {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
    ok: 'success',
    degraded: 'warning',
    error: 'danger',
    unknown: 'info',
  }
  return map[healthStatus.value] ?? 'info'
})

const healthIcon = computed(() => {
  const map: Record<string, string> = {
    ok: 'CircleCheck',
    degraded: 'Warning',
    error: 'CircleClose',
    unknown: 'QuestionFilled',
  }
  return map[healthStatus.value] ?? 'QuestionFilled'
})

const healthTooltip = computed(() => {
  const h = health.value
  if (!h) return '尚未获取后端状态'
  const lines = [
    `${h.app_name} v${h.app_version}（${h.env}）`,
    `Python ${h.python_version}`,
    `akshare ${h.akshare_version} ｜ baostock ${h.baostock_version}`,
    `交易日历 ${h.calendar?.first_date ?? '-'} ~ ${h.calendar?.last_date ?? '-'}`,
  ]
  if (h.warnings?.length) lines.push(`⚠ ${h.warnings.length} 条告警`)
  return lines.join('\n')
})

function refresh() {
  void healthStore.refresh()
}

onMounted(refresh)
</script>

<style scoped>
.layout {
  height: 100vh;
}

.sidebar {
  background: #fff;
  border-right: 0.5px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  transition: width 0.2s;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  /* 必须与右侧 .header（el-header height=56px）完全同高，
     否则两条底部分割线在交界处错开成台阶 */
  height: 56px;
  padding: 0 16px;
  border-bottom: 0.5px solid #e4e7ed;
  flex: 0 0 56px;
}

.brand-logo {
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
}

.brand-text {
  overflow: hidden;
  white-space: nowrap;
}

.brand-name {
  margin: 0;
  font-size: 13.5px;
  font-weight: 500;
  color: #1f2329;
}

.brand-stage {
  margin: 2px 0 0;
  font-size: 11.5px;
  color: #8a8f99;
}

.menu {
  flex: 1;
  border-right: none;
  overflow-y: auto;
  padding: 8px 0;
}

.menu-label {
  margin-right: auto;
}

.menu-tag {
  transform: scale(0.86);
  transform-origin: right center;
}

.sidebar-footer {
  border-top: 0.5px solid #e4e7ed;
  padding: 6px;
  text-align: center;
}

.header {
  background: #fff;
  border-bottom: 0.5px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.header-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.header-title {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
}

.header-stage {
  font-size: 12.5px;
  color: #8a8f99;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tag-icon {
  margin-right: 3px;
  vertical-align: -2px;
}

.main {
  padding: 0;
  background: #f5f6f8;
  overflow-y: auto;
}
</style>
