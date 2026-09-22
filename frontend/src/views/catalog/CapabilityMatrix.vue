<template>
  <div class="matrix">
    <el-alert v-if="verifiedCount < sourceCount" type="info" :closable="false" class="matrix-alert">
      <template #title>
        {{ verifiedCount }}/{{ sourceCount }} 个数据源经本环境实测；其余依据官方文档整理、
        <b>未经真实调用验证</b>（整列标灰）。接入前必须自行实测 —— 抄来的文档不等于可用的结论。
      </template>
    </el-alert>

    <div v-if="loading" class="loading"><el-skeleton :rows="8" animated /></div>

    <template v-else>
      <div class="matrix-card">
        <div class="matrix-head">
          <span class="matrix-title">能力对照表</span>
          <span class="matrix-sub">
            {{ sourceCount }} 个数据源 × {{ rows.length }} 项能力 ·
            <b>{{ criticalTotal }}</b> 项为关键能力（缺一不可用于回测）
          </span>
        </div>

        <el-table :data="rows" border size="small" class="matrix-table">
          <el-table-column label="数据类别" width="220" fixed>
            <template #default="{ row }">
              <div class="cap-group">{{ row.groupLabel }}</div>
              <div class="cap-name">
                <span>{{ row.label }}</span>
                <span v-if="row.critical" class="tag-critical">关键</span>
              </div>
              <div class="cap-desc">{{ row.desc }}</div>
            </template>
          </el-table-column>

          <el-table-column
            v-for="src in sources"
            :key="src.name"
            align="center"
            :min-width="132"
          >
            <template #header>
              <div class="src-head" :class="{ unverified: !src.verified }">
                <div class="src-name">
                  {{ src.display_name }}
                  <span class="tag-pill" :class="src.verified ? 'tag-ok' : 'tag-muted'">
                    {{ src.verified ? '已实测' : '未实测' }}
                  </span>
                </div>
                <div class="src-role">{{ src.role }}</div>
                <div
                  v-if="src.verified && src.verified_at"
                  class="src-verified"
                  :class="{ stale: src.stale }"
                >
                  {{ src.verified_at }}
                  <template v-if="src.stale">（超 {{ verifyStaleDays }} 天，建议复测）</template>
                </div>
              </div>
            </template>

            <template #default="{ $index }">
              <div class="cell" :class="{ unverified: !src.verified }">
                <span class="mark" :class="markClass(src, $index)">
                  {{ markText(src, $index) }}
                </span>
                <el-tooltip
                  v-if="capNote(src, $index)"
                  :content="capNote(src, $index)"
                  placement="top"
                  :show-after="150"
                >
                  <span class="note-dot">i</span>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 每个源的详情卡 -->
      <div class="detail-grid">
        <el-card
          v-for="src in sources"
          :key="src.name"
          shadow="never"
          class="detail-card"
          :class="{ 'unverified-card': !src.verified }"
        >
          <template #header>
            <div class="detail-head">
              <b>{{ src.display_name }}</b>
              <span class="tag-pill" :class="src.verified ? 'tag-ok' : 'tag-muted'">
                {{ src.verified ? '已实测' : '未实测' }}
              </span>
              <span class="push" />
              <span class="detail-count">
                {{ src.supported_count }}/{{ src.capability_count }} 项支持
              </span>
            </div>
          </template>
          <div class="detail-body">
            <p><b>定位：</b>{{ src.role }}（代码格式：{{ src.symbol_format }}）</p>
            <p><b>许可：</b>{{ src.license }}</p>
            <p class="progress-line">
              <b>关键能力覆盖：</b>{{ src.critical_count }}/{{ src.critical_total }}
              <el-progress
                :percentage="Math.round((src.critical_ratio || 0) * 100)"
                :stroke-width="8"
                :show-text="false"
                :color="progressColor(src.critical_ratio || 0)"
                class="progress"
              />
            </p>
            <p>
              <b>稳定性：</b>
              <span class="tag-pill" :class="riskClass(src.stability?.interface_change_risk)">
                {{ riskLabel(src.stability?.interface_change_risk) }}
              </span>
              {{ src.stability?.note }}
            </p>
            <div v-if="src.pitfalls?.length" class="pitfalls">
              <b>已知坑：</b>
              <ul>
                <li v-for="(p, i) in src.pitfalls" :key="i">{{ p }}</li>
              </ul>
            </div>
            <p v-if="src.verified_note" class="verified-note">⚠ {{ src.verified_note }}</p>
            <p v-if="src.homepage" class="homepage">
              <a :href="src.homepage" target="_blank" rel="noopener">{{ src.homepage }}</a>
            </p>
          </div>
        </el-card>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getDatasourceMatrix } from '@/api/system'
import type { CapabilityItem, DatasourceCapability } from '@/types/api'

const loading = ref(true)
const sources = ref<DatasourceCapability[]>([])
const capMeta = ref<Record<string, { label?: string; desc?: string; critical?: boolean }>>({})
const capKeys = ref<string[]>([])
const groupOfKey = ref<Record<string, { label: string; note: string }>>({})
const sourceCount = ref(0)
const verifiedCount = ref(0)
const criticalTotal = ref(0)
const verifyStaleDays = ref(90)

interface Row {
  key: string
  label: string
  desc: string
  critical: boolean
  groupLabel: string
}

/** 行 = 能力项，顺序由后端按 groups 声明给出（关键能力靠前） */
const rows = computed<Row[]>(() => {
  const meta = capMeta.value
  const keys = capKeys.value.length ? capKeys.value : Object.keys(meta)
  return keys.map((key) => ({
    key,
    label: meta[key]?.label || key,
    desc: meta[key]?.desc || '',
    critical: Boolean(meta[key]?.critical),
    groupLabel: groupOfKey.value[key]?.label || '其他',
  }))
})

function capOf(src: DatasourceCapability, key: string): CapabilityItem | undefined {
  return src.capabilities?.[key]
}

type MarkKind = 'good' | 'partial' | 'no' | 'na'

function markKind(src: DatasourceCapability, idx: number): MarkKind {
  const cap = capOf(src, rows.value[idx]?.key || '')
  if (!cap) return 'na'
  if (!cap.supported) return 'no'
  return cap.quality === 'excellent' ? 'good' : 'partial'
}

function markClass(src: DatasourceCapability, idx: number): string {
  return markKind(src, idx)
}

function markText(src: DatasourceCapability, idx: number): string {
  return { good: '支持', partial: '部分', no: '不支持', na: '—' }[markKind(src, idx)]
}

function capNote(src: DatasourceCapability, idx: number): string {
  const cap = capOf(src, rows.value[idx]?.key || '')
  if (!cap) return ''
  const parts = [cap.note || '']
  if (cap.priority && cap.priority !== '-') parts.push(`优先级：${cap.priority}`)
  return parts.filter(Boolean).join('；')
}

function riskClass(risk?: string): string {
  if (risk === 'high') return 'tag-bad'
  if (risk === 'low') return 'tag-ok'
  return 'tag-warn'
}

function riskLabel(risk?: string): string {
  if (risk === 'high') return '高（接口频繁变更）'
  if (risk === 'low') return '低（长期稳定）'
  return '中'
}

function progressColor(ratio: number): string {
  if (ratio >= 0.7) return '#18a058'
  if (ratio >= 0.4) return '#e6a23c'
  return '#d03050'
}

onMounted(async () => {
  try {
    const data = await getDatasourceMatrix()
    sources.value = data.sources
    capMeta.value = data.capability_meta?.capabilities || {}
    capKeys.value = data.capability_keys || []
    sourceCount.value = data.source_count
    verifiedCount.value = data.verified_count
    criticalTotal.value = data.critical_total ?? 0
    verifyStaleDays.value = data.verify_stale_days || 90

    // capability key → 所属分组（用于表头分组标签）
    const map: Record<string, { label: string; note: string }> = {}
    for (const g of data.capability_meta?.groups || []) {
      for (const k of g.items || []) {
        map[k] = { label: g.label, note: g.note || '' }
      }
    }
    groupOfKey.value = map
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.matrix-alert {
  margin-bottom: 14px;
  border-radius: 12px;
}
.loading {
  padding: 24px 0;
}

/* ---------------- 表格卡 ---------------- */
.matrix-card {
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 16px;
  padding: 16px 18px 18px;
  box-shadow: 0 1px 3px rgba(31, 35, 41, 0.04);
}
.matrix-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}
.matrix-title {
  font-size: 14px;
  font-weight: 600;
}
.matrix-sub {
  font-size: 12.5px;
  color: #8a8f99;
}
.matrix-table {
  width: 100%;
}
.matrix-table :deep(.el-table__inner-wrapper),
.matrix-table :deep(.el-table__border-left-patch) {
  border-radius: 12px;
}

.cap-group {
  font-size: 11px;
  color: #a3a7b0;
  letter-spacing: 0.4px;
  margin-bottom: 2px;
}
.cap-name {
  display: flex;
  gap: 6px;
  align-items: center;
  font-weight: 600;
}
.cap-desc {
  color: #8a8f99;
  font-size: 12px;
  margin-top: 2px;
  line-height: 1.5;
}
.tag-critical {
  display: inline-block;
  padding: 0 7px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 17px;
  background: #fdecef;
  color: #b3203f;
}

/* ---------------- 表头 ---------------- */
.src-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 2px 0;
}
.src-head.unverified {
  opacity: 0.6;
}
.src-name {
  display: flex;
  gap: 6px;
  justify-content: center;
  align-items: center;
  font-weight: 600;
}
.src-role {
  color: #8a8f99;
  font-size: 12px;
}
.src-verified {
  color: #1c7a4a;
  font-size: 11px;
}
.src-verified.stale {
  color: #a96400;
}

/* ---------------- 单元格标记 ---------------- */
.cell {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.cell.unverified {
  opacity: 0.45;
}
.mark {
  display: inline-block;
  min-width: 48px;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 19px;
  border: 1px solid transparent;
}
.mark.good {
  background: #eaf6ef;
  color: #1c7a4a;
}
.mark.partial {
  background: #fdf3e3;
  color: #a96400;
}
.mark.no {
  background: #f6f7f9;
  color: #a3a7b0;
}
.mark.na {
  background: transparent;
  color: #c0c4cc;
}
.note-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  cursor: help;
  font-size: 10px;
  font-style: italic;
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-weight: 700;
}

/* ---------------- 通用胶囊标签 ---------------- */
.tag-pill {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 18px;
  white-space: nowrap;
}
.tag-ok {
  background: #eaf6ef;
  color: #1c7a4a;
}
.tag-warn {
  background: #fdf3e3;
  color: #a96400;
}
.tag-bad {
  background: #fdecef;
  color: #b3203f;
}
.tag-muted {
  background: #f2f4f7;
  color: #7d828b;
}

/* ---------------- 详情卡 ---------------- */
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 14px;
  margin-top: 16px;
}
.detail-card {
  border-radius: 16px;
  border: 1px solid #e8eaee;
  transition: box-shadow 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
}
.detail-card:hover {
  border-color: #cfe0f2;
  box-shadow: 0 8px 22px rgba(24, 95, 165, 0.1);
  transform: translateY(-2px);
}
.detail-card.unverified-card {
  opacity: 0.82;
}
.detail-card :deep(.el-card__header) {
  padding: 12px 16px;
  background: #fafcfe;
}
.detail-card :deep(.el-card__body) {
  padding: 14px 16px;
}
.detail-head {
  display: flex;
  gap: 8px;
  align-items: center;
}
.push {
  flex: 1;
}
.detail-count {
  font-size: 12px;
  color: #8a8f99;
}
.detail-body p {
  margin: 7px 0;
  font-size: 13px;
  line-height: 1.65;
  color: #4a4f57;
}
.progress-line {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.progress {
  margin-top: 2px;
}
.pitfalls ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.pitfalls li {
  font-size: 12.5px;
  line-height: 1.75;
  color: #5f6368;
}
.verified-note {
  color: #a96400;
}
.homepage a {
  color: var(--color-brand);
  font-size: 12.5px;
  word-break: break-all;
}
</style>
