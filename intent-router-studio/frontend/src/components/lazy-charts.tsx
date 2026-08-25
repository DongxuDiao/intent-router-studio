/** 图表懒加载（修改方案 V2 §5.2）：ECharts 只进异步 chunk，首个图表渲染时才下载。
 * 页面通过 ChartBoundary 包裹懒加载图表组件，加载期间显示等高占位。
 */
import { Spin } from 'antd'
import { Suspense, lazy } from 'react'
import type { ReactNode } from 'react'

export const LazyConfusionMatrixChart = lazy(() =>
  import('./charts').then((m) => ({ default: m.ConfusionMatrixChart })),
)
export const LazyReliabilityChart = lazy(() =>
  import('./charts').then((m) => ({ default: m.ReliabilityChart })),
)
export const LazyThresholdCurveChart = lazy(() =>
  import('./charts').then((m) => ({ default: m.ThresholdCurveChart })),
)
export const LazyDistributionChart = lazy(() =>
  import('./charts').then((m) => ({ default: m.DistributionChart })),
)
export const LazyLabelDistributionPie = lazy(() =>
  import('./charts').then((m) => ({ default: m.LabelDistributionPie })),
)

/** Suspense 边界 + 与图表等高的占位，避免加载完成后布局跳动。 */
export function ChartBoundary({ height = 300, children }: { height?: number; children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Spin size="small" />
        </div>
      }
    >
      {children}
    </Suspense>
  )
}
