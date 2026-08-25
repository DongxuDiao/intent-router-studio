/** 基础展示组件。 */
import { Progress, Space, Statistic, Tag, Typography } from 'antd'
import type { ReactNode } from 'react'
import { LABEL_COLORS, LABEL_NAMES } from '../types'
import { fmtPercent } from '../utils/format'

export function LabelTag({ label }: { label: string | null | undefined }) {
  if (!label) return <Tag>未标注</Tag>
  return <Tag color={LABEL_COLORS[label] ?? 'default'}>{LABEL_NAMES[label] ?? label}</Tag>
}

const RUN_STATUS_META: Record<string, { color: string; text: string }> = {
  DRAFT: { color: 'default', text: '草稿' },
  QUEUED: { color: 'blue', text: '排队中' },
  PREPARING: { color: 'processing', text: '准备中' },
  TRAINING_EMBEDDING: { color: 'processing', text: '嵌入微调' },
  TRAINING_HEAD: { color: 'processing', text: '分类头' },
  CALIBRATING: { color: 'processing', text: '校准中' },
  SEARCHING_THRESHOLDS: { color: 'processing', text: '阈值搜索' },
  EVALUATING: { color: 'processing', text: '评估中' },
  PACKAGING: { color: 'processing', text: '打包中' },
  SUCCEEDED: { color: 'success', text: '成功' },
  CANCELLING: { color: 'warning', text: '取消中' },
  CANCELLED: { color: 'default', text: '已取消' },
  FAILED: { color: 'error', text: '失败' },
  INTERRUPTED: { color: 'warning', text: '已中断' },
}

export function RunStatusTag({ status }: { status: string }) {
  const meta = RUN_STATUS_META[status] ?? { color: 'default', text: status }
  return <Tag color={meta.color}>{meta.text}</Tag>
}

export function ModelStatusTag({ status }: { status: string }) {
  const map: Record<string, { color: string; text: string }> = {
    CANDIDATE: { color: 'blue', text: '候选' },
    VALIDATED: { color: 'cyan', text: '已验证' },
    ACTIVE: { color: 'success', text: '激活中' },
    ARCHIVED: { color: 'default', text: '已归档' },
  }
  const meta = map[status] ?? { color: 'default', text: status }
  return <Tag color={meta.color}>{meta.text}</Tag>
}

export function DatasetStatusTag({ status }: { status: string }) {
  if (status === 'FROZEN') return <Tag color="success">已冻结</Tag>
  return <Tag color="orange">草稿可编辑</Tag>
}

export function PageHeader({ title, subTitle, extra }: { title: string; subTitle?: string; extra?: ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {title}
        </Typography.Title>
        {subTitle && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {subTitle}
          </Typography.Text>
        )}
      </div>
      <Space>{extra}</Space>
    </div>
  )
}

export function MetricCard({
  title,
  value,
  suffix,
  precision = 3,
  tone,
}: {
  title: string
  value: number | string | null | undefined
  suffix?: string
  precision?: number
  tone?: 'success' | 'warning' | 'danger'
}) {
  const color = tone === 'danger' ? '#cf1322' : tone === 'warning' ? '#d46b08' : tone === 'success' ? '#389e0d' : undefined
  let display: string | number = '-'
  if (typeof value === 'number') display = value
  else if (value !== null && value !== undefined) display = value
  return (
    <Statistic
      title={title}
      value={display}
      precision={typeof value === 'number' ? precision : undefined}
      suffix={suffix}
      valueStyle={{ color, fontSize: 22 }}
    />
  )
}

export function PercentMetric({ title, value, danger }: { title: string; value: number | null | undefined; danger?: boolean }) {
  return <MetricCard title={title} value={value === null || value === undefined ? null : fmtPercent(value)} tone={danger && (value ?? 0) > 0 ? 'danger' : undefined} />
}

/** Top-K 概率条形展示。 */
export function ProbBars({ topK }: { topK: { label: string; probability: number }[] }) {
  const max = Math.max(...topK.map((t) => t.probability), 0.0001)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {topK.slice(0, 5).map((item) => (
        <div key={item.label} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 110 }}>
            <LabelTag label={item.label} />
          </div>
          <Progress
            percent={Math.round((item.probability / max) * 100)}
            strokeColor={LABEL_COLORS[item.label] === 'default' ? '#d9d9d9' : undefined}
            format={() => item.probability.toFixed(3)}
            size="small"
            style={{ flex: 1, margin: 0 }}
          />
        </div>
      ))}
    </div>
  )
}
