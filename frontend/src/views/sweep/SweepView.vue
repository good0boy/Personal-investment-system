<!--
  模块 3 · 参数寻优 —— 任务列表页

  ★ 本页**没有"开始寻优"按钮**，这是设计而非遗漏（与回测页同一条纪律）：
    12~16 组 × 1115 天 × 全市场，一次几十分钟。放进 HTTP 请求链路会卡死服务。
    它由 CLI 跑，结果落盘，前端只读（技术架构.md 架构纪律 5）。
-->
<template>
  <div class="page">
    <!-- 说明 -->
    <div class="hint-bar">
      <el-icon class="hint-icon"><InfoFilled /></el-icon>
      <div class="hint-body">
        <p class="hint-title">参数寻优在本地 CLI 运行，本页只读结果</p>
        <p class="hint-cmd mono">
          cd backend && python -m strategies.sweep --config sweeps/hold_tp.yaml --tag v1
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <div class="panel table-panel" v-loading="loading">
      <el-table
        :data="rows"
        size="small"
        stripe
        class="sweep-table"
        empty-text="还没有参数寻优结果。先跑一次：python -m strategies.sweep --config sweeps/hold_tp.yaml"
        @row-dblclick="openDetail"
      >
        <el-table-column label="任务 / 策略" min-width="260">
          <template #default="{ row }">
            <div class="cell-run">
              <div class="run-line">
                <span class="run-strategy">{{ row.strategy }}</span>
                <el-tag v-if="row.tag" size="small" effect="plain" class="run-tag">
                  {{ row.tag }}
                </el-tag>
                <!-- ★ 调试运行必须一眼可见：它跑出的"高收益"没有任何意义 -->
                <el-tag v-if="row.debug_symbols" size="small" type="danger" effect="dark">
                  调试 {{ row.debug_symbols.length }} 只
                </el-tag>
                <el-tag v-if="!row.is_complete" size="small" type="warning" effect="plain">
                  未完成
                </el-tag>
              </div>
              <div class="run-id mono">
                {{ row.sweep_id }}
                <span class="run-time">· {{ fmtDateTime(row.created_at) }}</span>
              </div>
              <div v-if="row.description" class="run-desc">{{ row.description }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="118" align="center">
          <template #default="{ row }">
            <span :class="row.is_complete ? '' : 'text-warn'">
              {{ row.done_count }} / {{ row.combo_count }}
            </span>
            <el-progress
              :percentage="pct(row)"
              :stroke-width="4"
              :show-text="false"
              :status="row.is_complete ? 'success' : undefined"
              class="prog"
            />
          </template>
        </el-table-column>

        <el-table-column label="目标指标" width="112" align="center">
          <template #default="{ row }">
            <span class="mono">{{ objectiveLabel(row.objective) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="寻优区间" width="196">
          <template #default="{ row }">
            <div class="cell-range mono">
              {{ row.start_date || DASH }} ~ {{ row.end_date || DASH }}
            </div>
            <div class="text-muted text-small">
              {{ row.interval_name === 'in_sample' ? '样本内' : row.interval_name || DASH }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="80" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link size="small" type="primary" @click="openDetail(row)">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getSweeps } from '@/api/sweep'
import type { SweepSummary } from '@/types/api'
import { DASH, fmtDateTime } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const rows = ref<SweepSummary[]>([])

const OBJECTIVE_LABEL: Record<string, string> = {
  annual_return: '年化收益',
  total_return: '总收益',
  sharpe: '夏普',
  calmar: '卡玛',
}

function objectiveLabel(v: string) {
  return OBJECTIVE_LABEL[v] ?? v
}

/**
 * ⚠️ 参数用 `unknown` 而不是 `SweepSummary`：
 *    el-table 插槽的 `{ row }` 推断为它自己的 `DefaultRow`（只有索引签名），
 *    不是我们声明的行类型。写窄类型会让 vue-tsc 在每个插槽处报 TS2345。
 *    这里收口 + 内部断言，接口边界干净。
 */
function pct(row: unknown) {
  const r = row as SweepSummary
  if (!r || !r.combo_count) return 0
  return Math.min(100, Math.round((r.done_count / r.combo_count) * 100))
}

async function load() {
  loading.value = true
  try {
    rows.value = await getSweeps()
  } finally {
    loading.value = false
  }
}

/** el-table 的行参数类型是它自己的 `DefaultRow`，用 unknown 收口的包装函数接 */
function openDetail(row: unknown) {
  const r = row as SweepSummary
  void router.push({ name: 'sweep-detail', params: { sweepId: r.sweep_id } })
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
}

.table-panel {
  padding: 0;
  overflow: hidden;
}

.sweep-table {
  width: 100%;
}

.cell-run {
  line-height: 1.45;
}

.run-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.run-strategy {
  font-weight: 500;
  color: #1f2329;
}

.run-tag {
  transform: scale(0.92);
}

.run-id {
  color: #9aa0a9;
  font-size: 11.5px;
  margin-top: 1px;
}

.run-time {
  margin-left: 4px;
  color: #b4b9c1;
}

.run-desc {
  font-size: 12px;
  color: #8a8f99;
  margin-top: 2px;
}

.cell-range {
  font-size: 12.5px;
  color: #4a4f57;
}

.prog {
  margin-top: 4px;
}
</style>
