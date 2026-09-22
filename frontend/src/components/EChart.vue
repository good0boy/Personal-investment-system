<!--
  ECharts 包装组件。

  为什么要有这一层：
  1. **集中注册**：vue-echarts 需要 `use([...])` 注册用到的图表/组件，
     散在各页面里必然出现"这个页面注册了 DataZoom、那个页面忘了"的问题。
     集中一处，加新图表类型只改这里。
  2. **统一空态**：没有数据时 ECharts 会画一张白板（连坐标轴都没有），
     看起来像"渲染坏了"。所有图表统一显示可读的空态文案。
  3. **统一尺寸**：高度由 props 控制，避免每个调用方自己写 wrapper 样式。
-->
<template>
  <div class="chart-wrap" :style="{ height: height }">
    <v-chart v-if="hasData" class="chart" :option="option" :update-options="{ notMerge: true }" />
    <div v-else class="chart-empty">
      <el-icon><Histogram /></el-icon>
      <span>{{ emptyText }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, HeatmapChart, LineChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import type { EChartsOption } from 'echarts'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  VisualMapComponent,
  MarkLineComponent,
  MarkAreaComponent,
  TitleComponent,
])

const props = withDefaults(
  defineProps<{
    option: EChartsOption
    height?: string
    empty?: boolean
    emptyText?: string
  }>(),
  {
    height: '320px',
    empty: false,
    emptyText: '暂无数据',
  },
)

/** 有数据 = 未显式置空 && option 里至少有一个 series */
const hasData = computed(() => {
  if (props.empty) return false
  const series = props.option?.series
  if (!series) return false
  return Array.isArray(series) ? series.length > 0 : true
})
</script>

<style scoped>
.chart-wrap {
  width: 100%;
  position: relative;
}

.chart {
  width: 100%;
  height: 100%;
}

.chart-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #a3a7b0;
  font-size: 13px;
  background: #fafbfc;
  border-radius: 10px;
}

.chart-empty .el-icon {
  font-size: 26px;
  opacity: 0.55;
}
</style>
