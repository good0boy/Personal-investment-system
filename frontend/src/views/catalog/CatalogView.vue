<template>
  <div class="catalog-view">
    <!-- 页头（标题已在顶部栏，这里不重复） -->
    <div class="view-head">
      <p class="sub">
        常用基本面 / 实时 / 历史行情接口。卡片默认收起，点击展开查看入参出参并在线试跑；试跑会<b>真实调用</b>数据源（akshare 全局限流 1 秒/次）。
      </p>

      <div class="head-right">
        <div v-if="check" class="stat-chips">
          <span class="chip chip-total">
            <b>{{ check.total }}</b> 个接口
          </span>
          <span class="chip chip-ok">正常 {{ check.ok }}</span>
          <span v-if="check.broken" class="chip chip-bad">失效 {{ check.broken }}</span>
          <span v-if="check.missing" class="chip chip-bad">缺失 {{ check.missing }}</span>
          <span v-if="check.unchecked" class="chip chip-warn">未检 {{ check.unchecked }}</span>
        </div>
        <el-button type="primary" plain :loading="checking" @click="onRunCheck">
          运行自检
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="catalog-tabs">
      <!-- 三大类 -->
      <el-tab-pane v-for="cat in categories" :key="cat.key" :name="cat.key">
        <template #label>
          <span class="tab-label">
            <el-icon v-if="cat.icon" class="tab-icon"><component :is="cat.icon" /></el-icon>
            {{ cat.label }}
            <span class="tab-count">{{ countByCategory[cat.key] || 0 }}</span>
          </span>
        </template>

        <div v-if="loading" class="loading">
          <el-skeleton :rows="6" animated />
        </div>

        <div v-else class="cat-layout">
          <!-- 左：分类树 -->
          <aside class="side">
            <div class="side-panel">
              <el-input
                v-model="keyword"
                placeholder="搜索接口名 / 说明 / 标签"
                clearable
                :prefix-icon="Search"
              />
              <el-tree
                :data="treeData(cat.key)"
                :props="{ label: 'label', children: 'children' }"
                node-key="key"
                :expand-on-click-node="false"
                :default-expand-all="true"
                highlight-current
                @node-click="onNodeClick"
              />
            </div>
          </aside>

          <!-- 右：接口卡片 -->
          <section class="cards">
            <el-empty
              v-if="filteredItems(cat.key).length === 0"
              description="没有匹配的接口"
            />
            <div v-else class="cards-list">
              <el-collapse v-model="expandedIds" class="cards-collapse">
                <el-collapse-item
                  v-for="item in filteredItems(cat.key)"
                  :key="item.id"
                  :name="item.id"
                  :class="{ 'is-broken': isBroken(item) }"
                >
                  <template #title>
                    <div class="card-title" :class="{ broken: isBroken(item) }">
                      <span class="name">{{ item.name }}</span>
                      <span class="func mono">{{ item.func_name }}</span>
                      <span
                        class="src-tag"
                        :class="item.datasource === 'baostock' ? 'src-baostock' : 'src-akshare'"
                      >
                        {{ item.datasource }}
                      </span>
                      <span v-for="t in item.tags.slice(0, 2)" :key="t" class="tag-soft">
                        {{ t }}
                      </span>
                      <span v-if="isBroken(item)" class="tag-broken">自检失效</span>
                    </div>
                  </template>

                  <div class="card-body">
                    <div class="card-meta">
                      <a
                        v-if="item.doc_url"
                        :href="item.doc_url"
                        target="_blank"
                        rel="noopener"
                        class="meta-link"
                      >
                        官方文档 ↗
                      </a>
                      <span v-if="item.verified_at" class="meta-item">
                        自检于 {{ item.verified_at }}
                      </span>
                      <span v-else class="meta-item is-muted">尚未自检</span>
                      <span class="meta-item is-muted">接口 ID：{{ item.id }}</span>
                    </div>
                    <p class="desc">{{ item.description }}</p>
                    <p v-if="isBroken(item) && item.status_message" class="status-msg">
                      ⚠ {{ item.status_message }}
                    </p>

                    <el-row :gutter="16">
                      <el-col :xs="24" :md="12">
                        <h4>入参（{{ item.params.length }}）</h4>
                        <el-table :data="item.params" size="small" border>
                          <el-table-column prop="name" label="参数" width="130">
                            <template #default="{ row }">
                              <span class="mono">{{ row.name }}</span>
                              <span v-if="row.required" class="req">必填</span>
                            </template>
                          </el-table-column>
                          <el-table-column prop="type" label="类型" width="70" />
                          <el-table-column prop="default" label="默认" width="110">
                            <template #default="{ row }">
                              <span class="mono">{{ row.default ?? '—' }}</span>
                            </template>
                          </el-table-column>
                          <el-table-column prop="desc" label="说明" show-overflow-tooltip />
                        </el-table>
                      </el-col>
                      <el-col :xs="24" :md="12">
                        <h4>
                          出参（{{ item.returns.length }}）
                          <el-tooltip
                            v-if="item.returns_verified === false"
                            content="本机网络受限环境下未能实测，列名依据文档整理"
                            placement="top"
                          >
                            <span class="tag-warn">未实测</span>
                          </el-tooltip>
                        </h4>
                        <el-table :data="item.returns" size="small" border max-height="260">
                          <el-table-column prop="name" label="字段" width="170">
                            <template #default="{ row }">
                              <span class="mono">{{ row.name }}</span>
                            </template>
                          </el-table-column>
                          <el-table-column prop="type" label="类型" width="90" />
                          <el-table-column prop="desc" label="说明" show-overflow-tooltip />
                        </el-table>
                      </el-col>
                    </el-row>

                    <!-- 试跑区 -->
                    <div class="try-zone">
                      <h4 class="try-title">在线试跑</h4>
                      <div class="try-form">
                        <div v-for="p in item.params" :key="p.name" class="try-field">
                          <label class="mono">{{ p.name }}</label>
                          <el-select
                            v-if="p.options && p.options.length"
                            v-model="tryParams[item.id][p.name]"
                            clearable
                            filterable
                            placeholder="选择"
                            style="width: 260px"
                          >
                            <el-option v-for="o in p.options" :key="o" :label="o" :value="o" />
                          </el-select>
                          <el-input
                            v-else
                            v-model="tryParams[item.id][p.name]"
                            :placeholder="p.desc"
                            clearable
                            style="width: 260px"
                          />
                        </div>
                        <el-button
                          type="primary"
                          :loading="tryState[item.id]?.loading"
                          @click="onTry(item)"
                        >
                          试跑
                        </el-button>
                        <el-button
                          :disabled="tryState[item.id]?.loading"
                          @click="onTry(item, true)"
                        >
                          强制刷新
                        </el-button>
                      </div>

                      <el-alert
                        v-if="tryState[item.id]?.error"
                        :title="tryState[item.id]!.error!"
                        type="error"
                        :closable="false"
                        class="try-error"
                      />

                      <div v-if="tryState[item.id]?.result" class="try-result">
                        <div class="result-meta">
                          <span
                            v-if="tryState[item.id]!.result!.cached"
                            class="chip chip-ok"
                          >
                            缓存命中
                          </span>
                          <span class="chip">
                            {{ tryState[item.id]!.result!.shape[0] }} 行 ×
                            {{ tryState[item.id]!.result!.shape[1] }} 列
                          </span>
                          <span class="chip">{{ tryState[item.id]!.result!.elapsed_ms }} ms</span>
                          <span v-if="isEmptyResult(tryState[item.id]!.result!)" class="chip chip-warn">
                            返回空表
                          </span>
                          <span v-if="tryState[item.id]!.result!.note" class="chip chip-warn">
                            {{ tryState[item.id]!.result!.note }}
                          </span>
                        </div>
                        <p v-if="isEmptyResult(tryState[item.id]!.result!)" class="empty-hint">
                          调用没报错但结果为空。常见原因：<b>参数日期超出该接口可查范围</b>
                          （部分东财接口只保留最近几个交易日）、当天数据尚未生成、或该参数组合本就无记录。
                          换个交易日再试通常能确认。
                        </p>
                        <el-table
                          :data="tryState[item.id]!.result!.rows"
                          size="small"
                          border
                          max-height="320"
                        >
                          <el-table-column
                            v-for="(col, idx) in tryState[item.id]!.result!.columns"
                            :key="col.name"
                            :prop="String(idx)"
                            :label="col.name"
                            min-width="110"
                            show-overflow-tooltip
                          >
                            <template #header>
                              <span>{{ col.name }}</span>
                              <span class="dtype">{{ col.dtype }}</span>
                            </template>
                          </el-table-column>
                        </el-table>
                      </div>
                    </div>

                    <div class="code-zone">
                      <h4>示例代码</h4>
                      <div class="code-wrap">
                        <pre class="mono code">{{ item.sample_code }}</pre>
                        <el-button
                          class="code-copy"
                          size="small"
                          text
                          type="primary"
                          @click="copyCode(item.sample_code)"
                        >
                          复制
                        </el-button>
                      </div>
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </div>
          </section>
        </div>
      </el-tab-pane>

      <!-- 数据源能力矩阵 -->
      <el-tab-pane name="matrix">
        <template #label>
          <span class="tab-label">
            <el-icon class="tab-icon"><component :is="'Histogram'" /></el-icon>
            数据源能力矩阵
          </span>
        </template>
        <CapabilityMatrix />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { getCatalogItems, tryCatalog, runCatalogCheck } from '@/api/catalog'
import type {
  CatalogCategoryMeta,
  CatalogCheckSummary,
  CatalogItem,
  TryResult,
} from '@/types/api'
import CapabilityMatrix from './CapabilityMatrix.vue'

const activeTab = ref('fundamental')
const loading = ref(true)
const items = ref<CatalogItem[]>([])
const categories = ref<CatalogCategoryMeta[]>([])
const check = ref<CatalogCheckSummary | null>(null)
const checking = ref(false)

const keyword = ref('')
const subFilter = ref('')
const expandedIds = ref<string[]>([])

/** 每个接口的试跑参数草稿：{ [itemId]: { [paramName]: string } } */
const tryParams = reactive<Record<string, Record<string, string>>>({})
/** 每个接口的试跑状态 */
const tryState = reactive<
  Record<string, { loading?: boolean; result?: TryResult; error?: string }>
>({})

const countByCategory = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const it of items.value) out[it.category] = (out[it.category] || 0) + 1
  return out
})

function isBroken(item: CatalogItem): boolean {
  return item.status === 'broken' || item.status === 'missing'
}

/** 调用成功但结果为空 —— 界面必须点出来，否则会被误读成"接口正常" */
function isEmptyResult(r: TryResult): boolean {
  return r.shape[0] === 0 || r.shape[1] === 0
}

function filteredItems(catKey: string): CatalogItem[] {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter((it) => {
    if (it.category !== catKey) return false
    if (subFilter.value && (it.sub_category || '其他') !== subFilter.value) return false
    if (!kw) return true
    return (
      it.name.toLowerCase().includes(kw) ||
      it.func_name.toLowerCase().includes(kw) ||
      it.description.toLowerCase().includes(kw) ||
      it.tags.some((t) => t.toLowerCase().includes(kw))
    )
  })
}

function treeData(catKey: string) {
  const subs = new Map<string, number>()
  for (const it of filteredItems(catKey)) {
    const k = it.sub_category || '其他'
    subs.set(k, (subs.get(k) || 0) + 1)
  }
  const children = Array.from(subs.entries()).map(([k, n]) => ({
    key: k,
    label: `${k}（${n}）`,
    count: n,
  }))
  return [
    {
      key: '',
      label: `全部（${children.reduce((s, c) => s + (c.count || 0), 0)}）`,
      children,
    },
  ]
}

function onNodeClick(node: { key?: string }) {
  subFilter.value = node.key || ''
}

function ensureTryDraft(item: CatalogItem): void {
  if (!tryParams[item.id]) {
    const draft: Record<string, string> = {}
    for (const p of item.params) {
      draft[p.name] = p.default == null ? '' : String(p.default)
    }
    tryParams[item.id] = draft
  }
}

async function onTry(item: CatalogItem, force = false) {
  ensureTryDraft(item)
  tryState[item.id] = { loading: true }
  try {
    const result = await tryCatalog(item.id, {
      params: { ...tryParams[item.id] },
      force,
    })
    tryState[item.id] = { result }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    tryState[item.id] = { error: msg }
  }
}

async function onRunCheck() {
  checking.value = true
  try {
    await runCatalogCheck(0)
    ElMessage.success('自检完成')
    await load()
  } finally {
    checking.value = false
  }
}

async function copyCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择')
  }
}

async function load() {
  loading.value = true
  try {
    const resp = await getCatalogItems()
    items.value = resp.items
    categories.value = resp.categories
    check.value = resp.check
    for (const it of items.value) ensureTryDraft(it)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.catalog-view {
  padding: 4px 0;
}

/* ---------------- 页头 ---------------- */
.view-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 14px;
}
.view-head .sub {
  margin: 0;
  color: #6b7076;
  font-size: 13px;
  flex: 1 1 auto;
  min-width: 0;
  line-height: 1.65;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 12px;
  white-space: nowrap;
}
.stat-chips {
  display: flex;
  align-items: center;
  gap: 6px;
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
  border: 1px solid transparent;
}
.chip b {
  color: var(--color-brand);
  font-size: 13px;
}
.chip-total {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.chip-ok {
  background: #eaf6ef;
  color: #1c7a4a;
}
.chip-warn {
  background: #fdf3e3;
  color: #a96400;
}
.chip-bad {
  background: #fdecef;
  color: #b3203f;
}

/* ---------------- Tab 栏：椭圆胶囊分段控件 ---------------- */
.catalog-tabs :deep(.el-tabs__header) {
  margin-bottom: 16px;
}
.catalog-tabs :deep(.el-tabs__nav-wrap::after) {
  display: none;
}
.catalog-tabs :deep(.el-tabs__active-bar) {
  display: none;
}
.catalog-tabs :deep(.el-tabs__nav) {
  display: inline-flex;
  gap: 3px;
  padding: 4px;
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 999px;
  box-shadow: 0 1px 3px rgba(31, 35, 41, 0.04);
}
.catalog-tabs :deep(.el-tabs__item) {
  height: 36px;
  line-height: 36px;
  padding: 0 20px !important;
  border-radius: 999px;
  color: #5f6368;
  font-size: 13.5px;
  transition: background 0.18s ease, color 0.18s ease, box-shadow 0.18s ease;
}
.catalog-tabs :deep(.el-tabs__item:hover) {
  color: var(--color-brand);
  background: #f2f6fb;
}
.catalog-tabs :deep(.el-tabs__item.is-active) {
  color: #fff;
  background: var(--color-brand);
  font-weight: 500;
  box-shadow: 0 3px 10px rgba(24, 95, 165, 0.26);
}
.catalog-tabs :deep(.el-tabs__item.is-active:hover) {
  color: #fff;
  background: var(--color-brand);
}
.catalog-tabs :deep(.el-tabs__content) {
  overflow: visible;
}
.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tab-icon {
  font-size: 14px;
}
.tab-count {
  display: inline-block;
  min-width: 18px;
  padding: 0 5px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 16px;
  text-align: center;
  background: rgba(0, 0, 0, 0.07);
  color: inherit;
}
.catalog-tabs :deep(.el-tabs__item.is-active) .tab-count {
  background: rgba(255, 255, 255, 0.24);
}

/* ---------------- 布局 ---------------- */
.cat-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.side {
  width: 268px;
  flex-shrink: 0;
  position: sticky;
  top: 12px;
}
.side-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 16px;
  box-shadow: 0 1px 3px rgba(31, 35, 41, 0.04);
}
.side-panel :deep(.el-input__wrapper) {
  border-radius: 10px;
}
.side-panel :deep(.el-tree-node__content) {
  height: 32px;
  border-radius: 8px;
}
.side-panel :deep(.el-tree-node.is-current > .el-tree-node__content) {
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-weight: 500;
}
.cards {
  flex: 1;
  min-width: 0;
}

/* ---------------- 接口卡片 ---------------- */
.cards-collapse {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-top: none;
  border-bottom: none;
}
.cards-collapse :deep(.el-collapse-item) {
  background: #fff;
  border: 1px solid #e8eaee;
  border-radius: 16px;
  overflow: hidden;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}
.cards-collapse :deep(.el-collapse-item:hover) {
  border-color: #cfe0f2;
  box-shadow: 0 6px 18px rgba(24, 95, 165, 0.08);
}
.cards-collapse :deep(.el-collapse-item.is-broken) {
  border-color: #f3ccd3;
  background: #fffcfc;
}
.cards-collapse :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 54px;
  padding: 11px 18px;
  border-bottom: none;
  background: transparent;
  border-radius: 16px;
  transition: background 0.18s ease;
}
.cards-collapse :deep(.el-collapse-item__header:hover) {
  background: #fafcfe;
}
.cards-collapse :deep(.el-collapse-item__header.is-active) {
  border-radius: 16px 16px 0 0;
  background: #fafcfe;
}
.cards-collapse :deep(.el-collapse-item__wrap) {
  border-bottom: none;
  background: transparent;
}
.cards-collapse :deep(.el-collapse-item__content) {
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
.card-title.broken .name {
  text-decoration: line-through;
  color: #8a8f99;
}
.card-title .func {
  color: #8a8f99;
}
.src-tag,
.tag-soft,
.tag-warn,
.tag-broken,
.req {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  line-height: 18px;
  border: 1px solid transparent;
}
.src-akshare {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.src-baostock {
  background: #fdf3e3;
  color: #a96400;
}
.tag-soft {
  background: #f4f5f7;
  color: #7d828b;
}
.tag-warn {
  background: #fdf3e3;
  color: #a96400;
}
.tag-broken {
  background: #fdecef;
  color: #b3203f;
}
.req {
  margin-left: 6px;
  padding: 0 7px;
  background: #fdecef;
  color: #b3203f;
  font-size: 11px;
}

.card-body {
  padding: 0;
}
.card-body h4 {
  margin: 14px 0 7px;
  font-size: 13px;
  color: #8a8f99;
  display: flex;
  gap: 6px;
  align-items: center;
  font-weight: 500;
}
.desc {
  margin: 8px 0 0;
  line-height: 1.7;
  color: #4a4f57;
}
.card-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  margin-top: 10px;
  font-size: 12px;
}
.meta-item {
  color: #8a8f99;
}
.meta-item.is-muted {
  color: #b3b7bf;
}
.meta-link {
  color: var(--color-brand);
  text-decoration: none;
  border-bottom: 1px dashed rgba(24, 95, 165, 0.4);
  padding-bottom: 1px;
}
.meta-link:hover {
  border-bottom-style: solid;
}
.status-msg {
  color: var(--color-error);
  font-size: 13px;
  margin: 8px 0 0;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

/* ---------------- 试跑区 ---------------- */
.try-zone {
  margin-top: 16px;
  padding: 14px 16px 16px;
  border-radius: 14px;
  background: #f7f9fc;
  border: 1px solid #e6edf6;
}
.try-zone :deep(.el-table) {
  border-radius: 10px;
  overflow: hidden;
}
.try-title {
  margin: 0 0 10px !important;
  color: var(--color-brand) !important;
}
.try-form {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: center;
}
.try-field {
  display: flex;
  align-items: center;
  gap: 8px;
}
.try-field label {
  color: #5f6368;
  min-width: 80px;
  text-align: right;
}
.try-form :deep(.el-input__wrapper),
.try-form :deep(.el-select__wrapper) {
  border-radius: 10px;
}
.try-error {
  margin-top: 12px;
  border-radius: 10px;
}
.try-result {
  margin-top: 14px;
}
.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.empty-hint {
  margin: 0 0 10px;
  padding: 9px 12px;
  border-radius: 10px;
  background: #fffaf0;
  border: 1px solid #f5e3c0;
  color: #8a5a00;
  font-size: 12.5px;
  line-height: 1.7;
}
.dtype {
  color: #a3a7b0;
  font-size: 11px;
  margin-left: 4px;
}

/* ---------------- 代码区 ---------------- */
.code-zone {
  margin-top: 16px;
}
.code-wrap {
  position: relative;
}
.code {
  background: #f5f7fa;
  border: 1px solid #e8eaee;
  padding: 12px 14px;
  border-radius: 12px;
  overflow: auto;
  margin: 0;
  line-height: 1.65;
  font-size: 12.5px;
}
.code-copy {
  position: absolute;
  top: 6px;
  right: 6px;
  background: rgba(255, 255, 255, 0.9);
}

.loading {
  padding: 24px 0;
}
</style>
