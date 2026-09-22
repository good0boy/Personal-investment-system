<!--
  模块 2 · 因子库 —— 因子列表页

  ★ 本页**没有"开始计算"按钮**，这是设计而非遗漏（与回测页/寻优页同一条纪律）：
    11 个因子 × 700 万行约 50s；分层回测还要逐日构建股票池，约 3 分钟。
    放进 HTTP 请求链路会卡死服务。它由 CLI 跑，结果落盘，前端只读。

  数据来源（三层，互相不重复存）：
    定义 → factors/registry.yaml
    状态 → data/factor/_manifest.json
    分析 → data/factor/analysis/{name}.json
-->
<template>
  <div class="page">
    <!-- 说明 -->
    <div class="hint-bar">
      <el-icon class="hint-icon"><InfoFilled /></el-icon>
      <div class="hint-body">
        <p class="hint-title">因子由本地 CLI 计算，本页只读产物</p>
        <p class="hint-cmd mono">
          cd backend && python -m factors.calc --category daily && python -m factors.analyze --category daily
        </p>
      </div>
      <el-button size="small" :loading="checking" @click="runCheck">
        <el-icon><CircleCheck /></el-icon>
        自检
      </el-button>
      <el-button size="small" :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <!-- 分类概览 -->
    <div class="cat-grid">
      <div
        v-for="c in categories"
        :key="c.key"
        class="cat-card"
        :class="[`cat-${c.status}`, activeCat === c.key ? 'is-active' : '']"
        @click="activeCat = activeCat === c.key ? '' : c.key"
      >
        <div class="cat-head">
          <span class="cat-name">{{ c.cname }}</span>
          <el-tag size="small" :type="catTagType(c.status)" effect="plain">
            {{ CAT_STATUS[c.status] ?? c.status }}
          </el-tag>
        </div>
        <div class="cat-count">
          <b>{{ c.calculated_count }}</b> / {{ c.factor_count }} 个因子已计算
        </div>
        <div class="cat-desc">{{ c.desc }}</div>
        <div v-if="!c.available && c.note" class="cat-note">{{ c.note }}</div>
      </div>
    </div>

    <!-- 清单元信息 -->
    <div class="meta-line text-small text-muted">
      注册表 <span class="mono">{{ list?.registry_file || DASH }}</span>
      ｜清单更新 <span class="mono">{{ fmtDateTime(list?.manifest_updated_at) }}</span>
      ｜共 <b>{{ list?.total ?? 0 }}</b> 个因子，已计算 <b>{{ list?.calculated ?? 0 }}</b>
      <span v-if="activeCat" class="filter-chip" @click="activeCat = ''">
        仅看 {{ catName(activeCat) }} ×
      </span>
    </div>

    <!-- 因子表 -->
    <div class="panel table-panel" v-loading="loading">
      <el-table
        :data="shownFactors"
        size="small"
        stripe
        :empty-text="emptyText"
        @row-click="openDetail"
      >
        <el-table-column label="因子" min-width="240">
          <template #default="{ row }">
            <div class="cell-name">
              <div class="name-line">
                <span class="factor-name mono">{{ asItem(row).name }}</span>
                <span class="factor-cname">{{ asItem(row).cname }}</span>
                <el-tag
                  v-if="asItem(row).debug_symbols"
                  size="small"
                  type="danger"
                  effect="dark"
                >
                  调试 {{ asItem(row).debug_symbols?.length }} 只
                </el-tag>
              </div>
              <div class="factor-desc">{{ asItem(row).desc }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="分类" width="92" align="center">
          <template #default="{ row }">
            <span class="text-small">{{ catName(asItem(row).category) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="方向假设" width="100" align="center">
          <template #default="{ row }">
            <span :class="dirClass(asItem(row).direction)" class="text-small">
              {{ dirLabel(asItem(row).direction) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="单位" width="150">
          <template #default="{ row }">
            <span class="text-small text-muted">{{ asItem(row).unit || DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="覆盖区间" width="184">
          <template #default="{ row }">
            <template v-if="asItem(row).date_start">
              <div class="mono text-small">
                {{ asItem(row).date_start }} ~ {{ asItem(row).date_end }}
              </div>
              <div class="text-small text-muted">
                {{ fmtNum(asItem(row).symbols, 0) }} 只标的
                ｜非空 {{ fmtPct(1 - (asItem(row).nan_ratio ?? 0), 1) }}
              </div>
            </template>
            <span v-else class="text-muted text-small">未计算</span>
          </template>
        </el-table-column>

        <el-table-column label="IC 均值" width="96" align="right">
          <template #default="{ row }">
            <span
              v-if="asItem(row).analysis_summary?.ic?.mean != null"
              class="mono"
              :class="pnlClass(asItem(row).analysis_summary?.ic?.mean)"
            >
              {{ fmtNum(asItem(row).analysis_summary?.ic?.mean, 4) }}
            </span>
            <span v-else class="text-muted text-small">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="ICIR" width="84" align="right">
          <template #default="{ row }">
            <span
              v-if="asItem(row).analysis_summary?.ic?.ir != null"
              class="mono"
              :class="pnlClass(asItem(row).analysis_summary?.ic?.ir)"
            >
              {{ fmtNum(asItem(row).analysis_summary?.ic?.ir, 3) }}
            </span>
            <span v-else class="text-muted text-small">{{ DASH }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip
              v-if="asItem(row).warnings?.length"
              :content="asItem(row).warnings.join('；')"
              placement="top"
            >
              <el-tag size="small" :type="stateTagType(asItem(row).state)" effect="plain">
                {{ STATE_LABEL[asItem(row).state] ?? asItem(row).state }}
              </el-tag>
            </el-tooltip>
            <el-tag v-else size="small" :type="stateTagType(asItem(row).state)" effect="plain">
              {{ STATE_LABEL[asItem(row).state] ?? asItem(row).state }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="120" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link size="small" type="primary" @click.stop="openDetail(row)">
              详情
            </el-button>
            <el-tooltip v-if="!asItem(row).has_analysis" content="未做有效性分析，详情页只能看定义与时序">
              <span class="text-muted text-small">无分析</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 自检结果 -->
    <el-dialog v-model="checkVisible" title="因子自检（导入 + 签名 + 冒烟调用）" width="760px">
      <div v-if="checkResult">
        <p class="text-small text-muted">
          共 {{ checkResult.total }} 个因子｜
          <span class="status-ok">ok {{ checkResult.ok }}</span>｜
          <span :class="checkResult.broken ? 'status-error' : 'text-muted'">
            broken {{ checkResult.broken }}
          </span>
          ｜{{ checkResult.checked_at }}
        </p>
        <el-table :data="checkResult.items" size="small" max-height="420">
          <el-table-column label="因子" prop="name" width="150" class-name="mono" />
          <el-table-column label="实现" prop="impl" min-width="220" class-name="mono" />
          <el-table-column label="结果" width="80" align="center">
            <template #default="{ row }">
              <span :class="asCheck(row).status === 'ok' ? 'status-ok' : 'status-error'">
                {{ asCheck(row).status }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="200">
            <template #default="{ row }">
              <span class="text-small">{{ asCheck(row).message || DASH }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { checkFactors, getFactors } from '@/api/factors'
import type { FactorCategoryStatus, FactorCheckItem, FactorCheckResp, FactorItem, FactorListResp } from '@/types/api'
import { DASH, fmtDateTime, fmtNum, fmtPct, pnlClass } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const checking = ref(false)
const list = ref<FactorListResp | null>(null)
const activeCat = ref('')

const checkVisible = ref(false)
const checkResult = ref<FactorCheckResp | null>(null)

const CAT_STATUS: Record<FactorCategoryStatus, string> = {
  ready: '可计算',
  planned: '待开发',
  blocked: '数据源未接入',
}

const STATE_LABEL: Record<string, string> = {
  calculated: '已计算',
  not_calculated: '未计算',
  unindexed: '产物存在',
  orphan: '文件缺失',
  error: '状态异常',
}

const DIRECTION_LABEL: Record<number, string> = {
  1: '越大越好 ↑',
  [-1]: '越小越好 ↓',
  0: '中性 —',
}

const categories = computed(() => list.value?.categories ?? [])

const shownFactors = computed(() => {
  const all = list.value?.factors ?? []
  return activeCat.value ? all.filter((f) => f.category === activeCat.value) : all
})

const emptyText = computed(() =>
  list.value
    ? '当前筛选下没有因子。若全部分类都是「未计算」，先在 backend 下跑一次 python -m factors.calc --category daily'
    : '',
)

function catName(key: string) {
  return categories.value.find((c) => c.key === key)?.cname ?? key
}

function catTagType(status: FactorCategoryStatus) {
  if (status === 'ready') return 'success'
  if (status === 'planned') return 'warning'
  return 'danger'
}

function stateTagType(state: string) {
  if (state === 'calculated') return 'success'
  if (state === 'unindexed') return 'warning'
  if (state === 'orphan' || state === 'error') return 'danger'
  return 'info'
}

function dirLabel(direction: number) {
  return DIRECTION_LABEL[direction] ?? DASH
}

function dirClass(direction: number) {
  if (direction > 0) return 'text-up'
  if (direction < 0) return 'text-down'
  return 'text-muted'
}

/**
 * ⚠️ el-table 插槽的 `{ row }` 推断为 Element Plus 自己的 `DefaultRow`（只有索引签名），
 *    不是我们声明的行类型。写窄类型会让 vue-tsc 在每个插槽处报 TS2345。
 *    统一收口成 `unknown` + 内部断言（见开发进度 §4 问题 24）。
 */
function asItem(row: unknown): FactorItem {
  return row as FactorItem
}

function asCheck(row: unknown): FactorCheckItem {
  return row as FactorCheckItem
}

async function load() {
  loading.value = true
  try {
    list.value = await getFactors()
  } finally {
    loading.value = false
  }
}

async function runCheck() {
  checking.value = true
  try {
    checkResult.value = await checkFactors(true)
    checkVisible.value = true
  } finally {
    checking.value = false
  }
}

function openDetail(row: unknown) {
  const r = asItem(row)
  if (!r?.name) return
  void router.push({ name: 'factor-detail', params: { name: r.name } })
}

onMounted(load)
</script>

<style scoped>
.hint-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--color-brand-soft);
  border: 0.5px solid #cfe2f5;
  border-radius: 10px;
  padding: 10px 16px;
  margin-bottom: 14px;
}

.hint-icon {
  color: var(--color-brand);
  font-size: 17px;
  flex: 0 0 auto;
}

.hint-body {
  flex: 1;
  min-width: 0;
}

.hint-title {
  margin: 0;
  font-size: 13px;
  font-weight: 500;
  color: #14456f;
}

.hint-cmd {
  margin: 3px 0 0;
  color: #4a7aa5;
  font-size: 12px;
  word-break: break-all;
}

.cat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.cat-card {
  background: #fff;
  border: 0.5px solid #e4e7ed;
  border-radius: 10px;
  padding: 12px 14px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.cat-card:hover {
  border-color: #c3d7ec;
}

.cat-card.is-active {
  border-color: var(--color-brand);
  box-shadow: 0 0 0 2px rgba(24, 95, 165, 0.12);
}

.cat-planned,
.cat-blocked {
  background: #fbfbfc;
}

.cat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.cat-name {
  font-size: 13.5px;
  font-weight: 500;
}

.cat-count {
  margin-top: 6px;
  font-size: 12.5px;
  color: #5a6069;
}

.cat-count b {
  font-size: 15px;
  color: var(--color-brand);
}

.cat-desc {
  margin-top: 4px;
  font-size: 12px;
  color: #8a8f99;
}

.cat-note {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 0.5px dashed #e4e7ed;
  font-size: 12px;
  color: #a3854a;
  line-height: 1.5;
}

.meta-line {
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.filter-chip {
  margin-left: 6px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--color-brand-soft);
  color: var(--color-brand);
  cursor: pointer;
}

.table-panel {
  padding: 0;
  overflow: hidden;
}

.cell-name {
  line-height: 1.45;
  cursor: pointer;
}

.name-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.factor-name {
  font-weight: 600;
  color: #1f2329;
}

.factor-cname {
  font-size: 12.5px;
  color: #5a6069;
}

.factor-desc {
  margin-top: 2px;
  font-size: 12px;
  color: #9aa0a9;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
</style>
