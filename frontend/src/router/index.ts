import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 菜单结构 —— 侧边栏按 stage 分组展示。
 * stage 用于标注该模块属于哪个开发阶段（阶段 0 只有系统状态页是"已完成"）。
 */
export interface MenuMeta {
  title: string
  icon?: string
  stage?: string
  done?: boolean
  description?: string
  /** 子页面（详情等）不显示在侧边栏 —— 它们靠列表页点进来 */
  hideInMenu?: boolean
}

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/system',
  },
  {
    path: '/system',
    name: 'system',
    component: () => import('@/views/system/SystemView.vue'),
    meta: {
      title: '系统状态',
      icon: 'Monitor',
      stage: '阶段 0',
      done: true,
      description: '数据源连通性、交易日历、存储、调度任务、配置',
    } satisfies MenuMeta,
  },
  {
    path: '/catalog',
    name: 'catalog',
    component: () => import('@/views/catalog/CatalogView.vue'),
    meta: {
      title: '数据分类',
      icon: 'Grid',
      stage: '阶段 1',
      done: true,
      description: 'akshare/baostock 接口目录，入参出参点击展开，支持在线试跑',
    } satisfies MenuMeta,
  },
  {
    path: '/backtest',
    name: 'backtest',
    component: () => import('@/views/backtest/BacktestView.vue'),
    meta: {
      title: '策略回测',
      icon: 'TrendCharts',
      stage: '阶段 2',
      done: true,
      description: '净值曲线、收益指标、交易记录；回测走 CLI，此处只读展示',
    } satisfies MenuMeta,
  },
  {
    // 详情页：不在侧边栏出现，靠列表页跳转
    path: '/backtest/:strategy/:runId',
    name: 'backtest-detail',
    component: () => import('@/views/backtest/BacktestDetail.vue'),
    meta: {
      title: '回测详情',
      hideInMenu: true,
    } satisfies MenuMeta,
  },
  {
    path: '/sweep',
    name: 'sweep',
    component: () => import('@/views/sweep/SweepView.vue'),
    meta: {
      title: '参数寻优',
      icon: 'Aim',
      stage: '阶段 2',
      done: true,
      description: '持有期 × 止盈阈值网格扫描；热力图看高原与尖峰，CLI 运行',
    } satisfies MenuMeta,
  },
  {
    path: '/sweep/:sweepId',
    name: 'sweep-detail',
    component: () => import('@/views/sweep/SweepDetail.vue'),
    meta: {
      title: '寻优详情',
      hideInMenu: true,
    } satisfies MenuMeta,
  },
  {
    path: '/factors',
    name: 'factors',
    component: () => import('@/views/factors/FactorsView.vue'),
    meta: {
      title: '因子库',
      icon: 'DataAnalysis',
      stage: '阶段 3',
      done: true,
      description: '日度行情因子：时序、截面分布、分层回测、RankIC；因子走 CLI 计算',
    } satisfies MenuMeta,
  },
  {
    // 详情页：不在侧边栏出现，靠列表页跳转
    path: '/factors/:name',
    name: 'factor-detail',
    component: () => import('@/views/factors/FactorDetail.vue'),
    meta: {
      title: '因子详情',
      hideInMenu: true,
    } satisfies MenuMeta,
  },
  {
    path: '/paper',
    name: 'paper',
    component: () => import('@/views/paper/PaperView.vue'),
    meta: {
      title: '模拟仿真',
      icon: 'Clock',
      stage: '阶段 4',
      done: true,
      description: '冻结策略的日度全量重算：净值曲线、持仓、交易、跑批状态',
    } satisfies MenuMeta,
  },
  {
    path: '/alerts',
    name: 'alerts',
    component: () => import('@/views/alerts/AlertsView.vue'),
    meta: {
      title: '用户提醒',
      icon: 'Bell',
      stage: '阶段 5',
      done: true,
      description: '企业微信开/清仓提醒，消息模板、幂等、重试',
    } satisfies MenuMeta,
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFound.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = (to.meta?.title as string) || ''
  document.title = title ? `${title} · A股投资本地服务` : 'A股投资本地服务'
})

export default router
