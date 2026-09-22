<template>
  <div class="local-data">
    <el-alert type="info" :closable="false" class="intro">
      <template #title>
        这一页列的是<b>本地磁盘上已经落盘的数据</b>（以磁盘为唯一真相）。
        它与上面三个标签页回答的是两个问题：那边是「数据源能调什么」，这边是
        「我们已经存了什么」。<b>原始下载</b>（数据源给的）与<b>本地派生</b>（我们算的）
        已分别标注 —— 两者的可信度与更新方式完全不同，混着看会误导。
      </template>
    </el-alert>

    <div class="head">
      <div v-if="data" class="stat-chips">
        <span class="chip chip-total"><b>{{ data.summary.available }}</b> / {{ data.summary.total }} 个维度</span>
        <span v-if="data.summary.missing" class="chip chip-bad">未落盘 {{ data.summary.missing }}</span>
        <span class="chip">{{ data.summary.files.toLocaleString() }} 个文件</span>
        <span class="chip">{{ data.summary.human }}</span>
        <span class="chip">{{ data.summary.rows_human }}行</span>
        <span class="chip mono-chip">{{ data.summary.data_root }}</span>
      </div>
      <el-button :loading="loading" plain @click="load(true)">
        {{ loading ? '正在扫描…' : '重新扫描' }}
      </el-button>
    </div>

    <p v-if="data" class="scan-line">
      扫描于 {{ data.scanned_at }}，耗时 {{ (data.elapsed_ms / 1000).toFixed(1) }} 秒；
      结果在后端缓存 {{ Math.round(data.ttl_seconds / 60) }} 分钟 ——
      刚跑完回补想确认落盘，点「重新扫描」。<b>行数与覆盖区间读的是 parquet 元信息</b>，
      不扫数据列；文件数与体积按目录实际占用统计。
    </p>

    <div class="filters">
      <el-input
        v-model="keyword"
        placeholder="搜索维度名 / 落盘者 / 路径 / 说明"
        clearable
        :prefix-icon="Search"
        class="filter-search"
      />
      <el-radio-group v-model="originFilter" size="small">
        <el-radio-button value="">全部来源</el-radio-button>
        <el-radio-button value="downloaded">原始下载</el-radio-button>
        <el-radio-button value="derived">本地派生</el-radio-button>
        <el-radio-button value="state">本地状态</el-radio-button>
      </el-radio-group>
      <el-checkbox v-model="onlyMissing">只看未落盘的</el-checkbox>
    </div>

    <div v-if="loading" class="loading">
      <el-skeleton :rows="8" animated />
      <p class="scan-hint">
        首次扫描要看 ~2.5 万个 parquet 的文件尾，约 10~15 秒 —— 这是在读真实磁盘，不是卡住。
      </p>
    </div>

    <el-empty v-else-if="!visibleGroups.length" description="没有匹配的数据维度" />

    <template v-else>
      <section v-for="g in visibleGroups" :key="g.key" class="group">
        <div class="group-head">
          <span class="group-title">{{ g.label }}</span>
          <span class="group-count">{{ g.items.length }} 个维度</span>
          <p class="group-desc">{{ g.desc }}</p>
        </div>

        <el-collapse :model-value="expanded" class="cards" @change="onExpand">
          <el-collapse-item
            v-for="d in g.items"
            :key="d.key"
            :name="d.key"
            :class="{ 'is-missing': !d.available }"
          >
            <template #title>
              <div class="card-title">
                <span class="name">{{ d.name }}</span>
                <span class="origin" :class="`origin-${d.origin}`">{{ d.origin_label }}</span>
                <template v-if="d.available">
                  <span class="stat">{{ d.files.toLocaleString() }} 文件</span>
                  <span class="stat">{{ d.human }}</span>
                  <span class="stat">{{ d.rows.toLocaleString() }} 行</span>
                  <span v-if="d.date_start" class="stat stat-range">
                    {{ d.date_start }} ~ {{ d.date_end }}
                  </span>
                </template>
                <span v-else class="tag-missing">未落盘</span>
                <span class="path mono">{{ d.path }}</span>
              </div>
            </template>

            <div class="card-body">
              <p class="desc">{{ d.desc }}</p>

              <div class="meta-grid">
                <div class="meta">
                  <label>行粒度</label>
                  <span>{{ d.grain }}</span>
                </div>
                <div class="meta">
                  <label>落盘者</label>
                  <span class="mono">{{ d.producer }}</span>
                </div>
                <div class="meta">
                  <label>路径</label>
                  <span class="mono">{{ d.path }}</span>
                </div>
                <div v-if="d.available && d.date_start" class="meta">
                  <label>{{ d.date_label }}</label>
                  <span>{{ d.date_start }} ~ {{ d.date_end }}</span>
                </div>
                <div v-if="d.rows_scope" class="meta meta-warn">
                  <label>行数口径</label>
                  <span>{{ d.rows_scope }}</span>
                </div>
                <div v-for="e in d.extras" :key="e.label" class="meta">
                  <label>{{ e.label }}</label>
                  <span>{{ e.value }}</span>
                </div>
              </div>

              <p v-if="d.error" class="err">⚠ {{ d.error }}</p>

              <div v-if="d.factor_names.length" class="factor-chips">
                <span class="fc-label">含 {{ d.factor_count }} 个因子</span>
                <el-tooltip
                  v-for="f in d.factor_names"
                  :key="f.name"
                  :content="f.cname"
                  placement="top"
                  :show-after="150"
                >
                  <span class="fchip mono">{{ f.name }}</span>
                </el-tooltip>
              </div>

              <!-- 示例数据 -->
              <div v-if="d.available" class="sample">
                <div class="sample-head">
                  <h4>示例数据</h4>
                  <template v-if="samples[d.key]">
                    <span class="chip">
                      该维度共 {{ samples[d.key]!.total_rows.toLocaleString() }} 行
                    </span>
                    <span class="chip">
                      此处 {{ samples[d.key]!.shown_rows }} 行 × {{ samples[d.key]!.shown_columns }} 列
                    </span>
                  </template>
                </div>

                <div v-if="sampleLoading[d.key]" class="sample-loading">
                  <el-skeleton :rows="4" animated />
                </div>
                <el-alert
                  v-else-if="sampleError[d.key]"
                  :title="sampleError[d.key]!"
                  type="error"
                  :closable="false"
                />
                <template v-else-if="samples[d.key]">
                  <div class="source">
                    <span class="src-cap">{{ samples[d.key]!.source.caption }}</span>
                    <span v-if="samples[d.key]!.source.symbol" class="chip chip-sym mono">
                      取自 {{ samples[d.key]!.source.symbol }}
                    </span>
                    <span class="src-file mono">{{ samples[d.key]!.source.file }}</span>
                  </div>
                  <p v-if="samples[d.key]!.note" class="note">{{ samples[d.key]!.note }}</p>

                  <el-table
                    :data="samples[d.key]!.rows"
                    size="small"
                    border
                    max-height="420"
                    class="sample-table"
                  >
                    <el-table-column
                      v-for="(col, idx) in samples[d.key]!.columns"
                      :key="col.name"
                      :prop="String(idx)"
                      :label="col.name"
                      min-width="118"
                      show-overflow-tooltip
                    >
                      <template #header>
                        <span>{{ col.name }}</span>
                        <span class="dtype">{{ col.dtype }}</span>
                      </template>
                    </el-table-column>
                  </el-table>

                  <div v-if="samples[d.key]!.tables" class="tables">
                    <span class="fc-label">库里的表（行数）</span>
                    <span
                      v-for="t in samples[d.key]!.tables"
                      :key="t.name"
                      class="fchip mono"
                    >
                      {{ t.name }} · {{ t.rows.toLocaleString() }}
                    </span>
                  </div>
                </template>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { getLocalDatasets, getLocalSample } from '@/api/catalog'
import type { LocalDatasetsResp, LocalSample } from '@/types/api'

const loading = ref(false)
const data = ref<LocalDatasetsResp | null>(null)
const keyword = ref('')
const originFilter = ref('')
const onlyMissing = ref(false)
/** 已展开的维度（懒加载示例：点开才去读，避免一次打 19 个表） */
const expanded = ref<string[]>([])

const samples = reactive<Record<string, LocalSample>>({})
const sampleLoading = reactive<Record<string, boolean>>({})
const sampleError = reactive<Record<string, string>>({})

const visibleGroups = computed(() => {
  if (!data.value) return []
  const kw = keyword.value.trim().toLowerCase()
  const out: Array<{ key: string; label: string; desc: string; items: typeof data.value.datasets }> = []
  for (const g of data.value.groups) {
    const items = data.value.datasets.filter((d) => {
      if (d.group !== g.key) return false
      if (originFilter.value && d.origin !== originFilter.value) return false
      if (onlyMissing.value && d.available) return false
      if (!kw) return true
      return (
        d.name.toLowerCase().includes(kw) ||
        d.key.toLowerCase().includes(kw) ||
        d.desc.toLowerCase().includes(kw) ||
        d.producer.toLowerCase().includes(kw) ||
        d.path.toLowerCase().includes(kw)
      )
    })
    if (items.length) out.push({ key: g.key, label: g.label, desc: g.desc, items })
  }
  return out
})

/** 展开时懒加载示例（每个维度只请求一次） */
function onExpand(names: string | number | Array<string | number>): void {
  const list = Array.isArray(names) ? names.map(String) : [String(names)]
  expanded.value = list
  for (const k of list) {
    // 未落盘的维度没有数据可取 —— 不发这个请求（否则控制台每次都多一条 404）
    if (!data.value?.datasets.find((d) => d.key === k)?.available) continue
    if (!samples[k] && !sampleLoading[k]) void loadSample(k)
  }
}

async function loadSample(key: string): Promise<void> {
  sampleLoading[key] = true
  delete sampleError[key]
  try {
    samples[key] = await getLocalSample(key, 20)
  } catch (e) {
    sampleError[key] = e instanceof Error ? e.message : String(e)
  } finally {
    sampleLoading[key] = false
  }
}

async function load(refresh = false): Promise<void> {
  loading.value = true
  try {
    data.value = await getLocalDatasets(refresh)
    // 尺寸变了（重新扫描可能多出/少掉维度）→ 丢掉已缓存的示例，避免显示旧数据
    if (refresh) {
      for (const k of Object.keys(samples)) delete samples[k]
      expanded.value = []
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => load(false))
</script>

<style scoped>
.local-data {
  padding: 2px 0;
}

.intro {
  border-radius: 12px;
  margin-bottom: 14px;
}
.intro :deep(.el-alert__title) {
  line-height: 1.75;
}

/* ---------------- 页头 ---------------- */
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.stat-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 11px;
  border-radius: 999px;
  font-size: 12px;
  line-height: 20px;
  background: #f2f4f7;
  color: #5f6368;
}
.chip b {
  color: var(--color-brand);
  font-size: 13px;
}
.chip-total {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.chip-bad {
  background: #fdecef;
  color: #b3203f;
}
.chip-sym {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.mono-chip {
  font-family: var(--font-mono);
  font-size: 11.5px;
}
.scan-line {
  margin: 10px 0 14px;
  color: #8a8f99;
  font-size: 12.5px;
  line-height: 1.75;
}

/* ---------------- 筛选 ---------------- */
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 14px;
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 14px;
  margin-bottom: 16px;
}
.filter-search {
  width: 320px;
}
.filters :deep(.el-input__wrapper) {
  border-radius: 10px;
}

.loading {
  padding: 20px 0;
}
.scan-hint {
  margin: 12px 0 0;
  color: #8a8f99;
  font-size: 12.5px;
}

/* ---------------- 分组 ---------------- */
.group {
  margin-bottom: 22px;
}
.group-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.group-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
}
.group-count {
  font-size: 12px;
  color: #8a8f99;
}
.group-desc {
  flex: 1 1 100%;
  margin: 2px 0 0;
  color: #8a8f99;
  font-size: 12.5px;
}

/* ---------------- 卡片 ---------------- */
.cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: none;
  border-bottom: none;
}
.cards :deep(.el-collapse-item) {
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 16px;
  overflow: hidden;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}
.cards :deep(.el-collapse-item:hover) {
  border-color: #cfe0f2;
  box-shadow: 0 6px 18px rgba(24, 95, 165, 0.08);
}
.cards :deep(.el-collapse-item.is-missing) {
  border-color: #f3ccd3;
  background: #fffcfc;
}
.cards :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 52px;
  padding: 11px 18px;
  border-bottom: none;
  background: transparent;
  border-radius: 16px;
}
.cards :deep(.el-collapse-item__header.is-active) {
  border-radius: 16px 16px 0 0;
  background: #fafcfe;
}
.cards :deep(.el-collapse-item__wrap) {
  border-bottom: none;
  background: transparent;
}
.cards :deep(.el-collapse-item__content) {
  padding: 0 18px 18px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.card-title .name {
  font-weight: 600;
  font-size: 14px;
}
.card-title .stat {
  font-size: 11.5px;
  line-height: 18px;
  padding: 1px 9px;
  border-radius: 999px;
  background: #f4f5f7;
  color: #7d828b;
}
.card-title .stat-range {
  font-family: var(--font-mono);
  background: #f7f9fc;
  color: #6b8bab;
}
.card-title .path {
  color: #b3b7bf;
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 340px;
}
.origin {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 18px;
}
.origin-downloaded {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.origin-derived {
  background: #eaf6ef;
  color: #1c7a4a;
}
.origin-state {
  background: #f4f5f7;
  color: #7d828b;
}
.tag-missing {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 18px;
  background: #fdecef;
  color: #b3203f;
}

/* ---------------- 卡片内容 ---------------- */
.desc {
  margin: 8px 0 0;
  line-height: 1.75;
  color: #4a4f57;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 8px 18px;
  margin-top: 12px;
  padding: 12px 14px;
  background: #fafbfd;
  border: 1px solid #eef1f5;
  border-radius: 12px;
}
.meta {
  display: flex;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.7;
  min-width: 0;
}
.meta label {
  flex-shrink: 0;
  min-width: 74px;
  color: #8a8f99;
  text-align: right;
  /* 标签不换行（"覆盖（报告期）"这类长标签折行后看着像排版错了） */
  white-space: nowrap;
}
.meta span {
  color: #4a4f57;
  /* 路径/命令里没有空格，用 anywhere 断行不会把单词从中间劈开（break-all 会） */
  overflow-wrap: anywhere;
}
.meta-warn span {
  color: #a96400;
}
.err {
  margin: 10px 0 0;
  padding: 8px 12px;
  border-radius: 10px;
  background: #fdecef;
  border: 1px solid #f3ccd3;
  color: #b3203f;
  font-size: 12.5px;
  line-height: 1.7;
}

.factor-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.fc-label {
  color: #8a8f99;
  font-size: 12px;
  margin-right: 2px;
}
.fchip {
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 18px;
  background: #f4f5f7;
  color: #7d828b;
  cursor: default;
}

/* ---------------- 示例数据 ---------------- */
.sample {
  margin-top: 16px;
  padding: 14px 16px 16px;
  border-radius: 14px;
  background: #f7f9fc;
  border: 1px solid #e6edf6;
}
.sample-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sample-head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-brand);
}
.sample-loading {
  margin-top: 10px;
}
.source {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0 0;
  font-size: 12.5px;
}
.src-cap {
  color: #4a4f57;
}
.src-file {
  color: #a3a7b0;
  font-size: 11.5px;
}
.note {
  margin: 8px 0 0;
  color: #8a5a00;
  background: #fffaf0;
  border: 1px solid #f5e3c0;
  border-radius: 10px;
  padding: 7px 11px;
  font-size: 12.5px;
  line-height: 1.7;
}
.sample-table {
  margin-top: 10px;
}
.sample-table :deep(.el-table) {
  border-radius: 10px;
  overflow: hidden;
}
.dtype {
  color: #a3a7b0;
  font-size: 11px;
  margin-left: 4px;
}
.tables {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 12px;
}
</style>
