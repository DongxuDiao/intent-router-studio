/** ECharts 图表组件：混淆矩阵 / 校准曲线 / 阈值曲线 / 分布。 */
import ReactECharts from 'echarts-for-react'
import { LABEL_NAMES } from '../types'

export function ConfusionMatrixChart({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  const max = Math.max(...matrix.flat(), 1)
  const option = {
    tooltip: { formatter: (p: { data: number; name: string[] }) => `${p.name.join(' → ')}: ${p.data}` },
    grid: { left: 110, bottom: 90, right: 40, top: 10 },
    xAxis: {
      type: 'category',
      data: labels.map((l) => LABEL_NAMES[l] ?? l),
      name: '预测',
      nameLocation: 'middle',
      nameGap: 50,
      axisLabel: { rotate: 30 },
    },
    yAxis: {
      type: 'category',
      data: labels.map((l) => LABEL_NAMES[l] ?? l),
      name: '真实',
      inverse: true,
    },
    visualMap: {
      min: 0,
      max,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      inRange: { color: ['#f0f5ff', '#85a5ff', '#2f54eb'] },
      show: false,
    },
    series: [
      {
        type: 'heatmap',
        data: matrix.flatMap((row, i) => row.map((v, j) => [j, i, v])),
        label: { show: true, formatter: (p: { data: number }) => (p.data > 0 ? String(p.data) : '') },
        emphasis: { itemStyle: { shadowBlur: 4 } },
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: 360 }} notMerge />
}

export function ReliabilityChart({
  before,
  after,
}: {
  before?: { bin: number; confidence: number; accuracy: number }[] | null
  after?: { bin: number; confidence: number; accuracy: number }[] | null
}) {
  const option = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['校准前', '校准后', '理想'] },
    grid: { left: 50, right: 20, bottom: 40, top: 40 },
    xAxis: { type: 'value', min: 0, max: 1, name: '置信度' },
    yAxis: { type: 'value', min: 0, max: 1, name: '准确率' },
    series: [
      ...(before
        ? [{ name: '校准前', type: 'line' as const, data: before.map((d) => [d.confidence, d.accuracy]), symbolSize: 6 }]
        : []),
      ...(after
        ? [{ name: '校准后', type: 'line' as const, data: after.map((d) => [d.confidence, d.accuracy]), symbolSize: 6 }]
        : []),
      { name: '理想', type: 'line' as const, data: [[0, 0], [1, 1]], lineStyle: { type: 'dashed', color: '#999' }, symbol: 'none' },
    ],
  }
  return <ReactECharts option={option} style={{ height: 300 }} notMerge />
}

export interface CurvePoint {
  value: number
  coverage: number | null
  safe_coverage: number | null
  false_write_rate: number | null
  unclear_rate: number | null
}

export function ThresholdCurveChart({
  points,
  current,
  title,
}: {
  points: CurvePoint[]
  current?: number
  title?: string
}) {
  // 当前阈值处的竖直参考线
  const markLine = current
    ? {
        silent: true,
        symbol: 'none',
        label: { formatter: '当前 {c}', fontSize: 10 },
        lineStyle: { color: '#fa541c', type: 'dashed' as const },
        data: [{ xAxis: current }],
      }
    : undefined
  const option = {
    title: title ? { text: title, left: 'center', textStyle: { fontSize: 13 } } : undefined,
    tooltip: { trigger: 'axis' },
    legend: { data: ['coverage', 'safe_coverage', 'false_write_rate', 'unclear_rate'], bottom: 0 },
    grid: { left: 50, right: 60, bottom: 60, top: 40 },
    xAxis: { type: 'value', name: '阈值', min: Math.min(...points.map((p) => p.value)), max: Math.max(...points.map((p) => p.value)) },
    yAxis: { type: 'value', name: '比例' },
    series: [
      { name: 'coverage', type: 'line', data: points.map((p) => [p.value, p.coverage]) },
      { name: 'safe_coverage', type: 'line', data: points.map((p) => [p.value, p.safe_coverage]), markLine },
      { name: 'false_write_rate', type: 'line', data: points.map((p) => [p.value, p.false_write_rate]) },
      { name: 'unclear_rate', type: 'line', data: points.map((p) => [p.value, p.unclear_rate]) },
    ],
  }
  return <ReactECharts option={option} style={{ height: 320 }} notMerge />
}

export function DistributionChart({
  edges,
  counts,
  title,
}: {
  edges: number[]
  counts: number[]
  title: string
}) {
  const labels = edges.slice(0, -1).map((e, i) => `${e.toFixed(2)}-${edges[i + 1].toFixed(2)}`)
  const option = {
    title: { text: title, left: 'center', textStyle: { fontSize: 13 } },
    tooltip: {},
    grid: { left: 40, right: 20, bottom: 60, top: 40 },
    xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 9 } },
    yAxis: { type: 'value', name: '样本数' },
    series: [{ type: 'bar', data: counts, itemStyle: { color: '#597ef7' } }],
  }
  return <ReactECharts option={option} style={{ height: 280 }} notMerge />
}

export function LabelDistributionPie({ distribution }: { distribution: Record<string, number> }) {
  const data = Object.entries(distribution).map(([name, value]) => ({
    name: LABEL_NAMES[name] ?? name,
    value,
  }))
  const option = {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['35%', '65%'],
        data,
        label: { formatter: '{b}: {c}' },
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: 260 }} notMerge />
}
