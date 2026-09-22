<template>
  <div class="page">
    <div class="page-header">
      <h2>{{ title }}</h2>
      <p>{{ summary }}</p>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      :title="`${stage} 待开发`"
      class="mb-16"
    >
      <template #default>
        <p class="alert-line">
          当前处于<strong>阶段 0（底座）</strong>，本模块的页面骨架、路由、菜单已就位，
          业务逻辑将在 {{ stage }} 实现。下面列出的是已经确定的完整功能清单。
        </p>
      </template>
    </el-alert>

    <!-- 已就位的基础设施 -->
    <div class="panel">
      <div class="panel-title">
        <span>已就位的基础设施（阶段 0 交付）</span>
        <span class="sub">本模块可直接复用，无需重建</span>
      </div>
      <div class="ready-grid">
        <div v-for="r in ready" :key="r.name" class="ready-item">
          <el-icon class="ready-icon"><component :is="r.icon" /></el-icon>
          <div>
            <p class="ready-name">{{ r.name }}</p>
            <p class="ready-desc">{{ r.desc }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- 功能清单 -->
    <div class="panel">
      <div class="panel-title">
        <span>{{ stage }} 要实现的功能</span>
        <span class="sub">共 {{ features.length }} 项</span>
      </div>
      <div class="feature-list">
        <div v-for="(f, i) in features" :key="i" class="feature-item">
          <div class="feature-head">
            <span class="feature-idx">{{ i + 1 }}</span>
            <span class="feature-name">{{ f.name }}</span>
            <el-tag v-if="f.priority" size="small" :type="f.priority === 'P0' ? 'danger' : 'info'" effect="plain">
              {{ f.priority }}
            </el-tag>
          </div>
          <p class="feature-desc">{{ f.desc }}</p>
          <p v-if="f.acceptance" class="feature-accept">
            <el-icon><Select /></el-icon>
            验收：{{ f.acceptance }}
          </p>
        </div>
      </div>
    </div>

    <!-- 设计要点 -->
    <div v-if="notes.length" class="panel">
      <div class="panel-title">
        <span>关键设计要点</span>
        <span class="sub">开发时不要偏离</span>
      </div>
      <ul class="note-list">
        <li v-for="(n, i) in notes" :key="i" v-html="n"></li>
      </ul>
    </div>

    <!-- 文档引用 -->
    <div class="panel">
      <div class="panel-title">
        <span>规格依据</span>
        <span class="sub">开发前先读这两份文档对应章节</span>
      </div>
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item
          v-for="(d, i) in docs"
          :key="i"
          :label="d.file"
        >
          <span class="mono">{{ d.section }}</span>
          <span class="text-muted text-small"> — {{ d.what }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface Feature {
  name: string
  desc: string
  priority?: string
  acceptance?: string
}

export interface DocRef {
  file: string
  section: string
  what: string
}

export interface ReadyItem {
  name: string
  desc: string
  icon: string
}

const props = withDefaults(
  defineProps<{
    title: string
    summary: string
    stage: string
    features: Feature[]
    notes?: string[]
    docs: DocRef[]
    ready: ReadyItem[]
  }>(),
  {
    notes: () => [],
  },
)

const notes = computed(() => props.notes ?? [])
const ready = computed(() => props.ready ?? [])
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}

.alert-line {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}

.ready-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.ready-item {
  display: flex;
  gap: 10px;
  background: #f7f8fa;
  border-radius: 8px;
  padding: 12px 14px;
}

.ready-icon {
  color: #185fa5;
  font-size: 16px;
  margin-top: 2px;
  flex: 0 0 auto;
}

.ready-name {
  margin: 0 0 3px;
  font-size: 13px;
  font-weight: 500;
}

.ready-desc {
  margin: 0;
  font-size: 12.5px;
  color: #8a8f99;
  line-height: 1.6;
}

.feature-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.feature-item {
  border-left: 2.5px solid #e6f1fb;
  padding: 2px 0 2px 14px;
}

.feature-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.feature-idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #e6f1fb;
  color: #185fa5;
  font-size: 11.5px;
  font-weight: 500;
  flex: 0 0 20px;
}

.feature-name {
  font-size: 13.5px;
  font-weight: 500;
}

.feature-desc {
  margin: 0 0 0 28px;
  font-size: 12.5px;
  color: #5f6368;
  line-height: 1.7;
}

.feature-accept {
  margin: 5px 0 0 28px;
  font-size: 12px;
  color: #18a058;
  display: flex;
  align-items: flex-start;
  gap: 4px;
  line-height: 1.6;
}

.note-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: #3c4043;
}

.note-list :deep(strong) {
  color: #d03050;
}
</style>
