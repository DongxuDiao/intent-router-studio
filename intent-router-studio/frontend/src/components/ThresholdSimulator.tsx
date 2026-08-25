/** 阈值调节器：拖动滑块 → 防抖 simulate → 展示安全指标 → 满足约束才可保存。 */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Slider, Space, Table, Typography, message } from 'antd'
import { api, ApiError } from '../api/client'
import type { RouteMetrics, Thresholds } from '../types'
import { fmtPercent } from '../utils/format'

const DEFAULTS: Thresholds = {
  default_min_confidence: 0.65,
  write_min_confidence: 0.85,
  oos_min_confidence: 0.7,
  min_margin: 0.15,
}

export function ThresholdSimulator({
  runId,
  initial,
  constraints,
  onSaved,
}: {
  runId: string
  initial: Thresholds | null
  constraints: { max_false_write_rate: number; min_write_precision: number }
  onSaved?: () => void
}) {
  const [values, setValues] = useState<Thresholds>(initial ?? DEFAULTS)
  const [metrics, setMetrics] = useState<RouteMetrics | null>(null)
  const [violations, setViolations] = useState<{ code: string; message: string }[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout>>()

  useEffect(() => {
    if (initial) setValues(initial)
  }, [runId])

  // 拖动防抖：300ms 后在 validation 预测上模拟
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(async () => {
      setLoading(true)
      try {
        const res = await api<{ thresholds: Thresholds; metrics: RouteMetrics; violations: { code: string; message: string }[]; n: number }>(
          `/runs/${runId}/thresholds/simulate`,
          { method: 'POST', body: JSON.stringify(values) },
        )
        setMetrics(res.metrics)
        setViolations(res.violations)
      } catch (e) {
        if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
      } finally {
        setLoading(false)
      }
    }, 300)
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [runId, values])

  const fwOk = metrics == null || metrics.false_write_rate == null || metrics.false_write_rate <= constraints.max_false_write_rate
  const wpOk = metrics == null || metrics.write_precision == null || metrics.write_precision >= constraints.min_write_precision
  const canSave = violations.length === 0 && fwOk && wpOk

  const rows = metrics
    ? [
        { key: 'cov', name: 'coverage（接受率）', value: fmtPercent(metrics.coverage), ok: true },
        { key: 'safe', name: 'safe_coverage（安全接受率）', value: fmtPercent(metrics.safe_coverage), ok: true },
        {
          key: 'fw',
          name: `false_write_rate（要求 ≤ ${fmtPercent(constraints.max_false_write_rate)}）`,
          value: fmtPercent(metrics.false_write_rate),
          ok: fwOk,
        },
        {
          key: 'wp',
          name: `write_precision（要求 ≥ ${fmtPercent(constraints.min_write_precision)}）`,
          value: fmtPercent(metrics.write_precision),
          ok: wpOk,
        },
        { key: 'unclear', name: 'unclear_rate（转人工率）', value: fmtPercent(metrics.unclear_rate), ok: true },
        { key: 'acc', name: 'selective_accuracy', value: fmtPercent(metrics.selective_accuracy), ok: true },
      ]
    : []

  const save = async () => {
    setSaving(true)
    try {
      await api(`/runs/${runId}/threshold-versions`, { method: 'POST', body: JSON.stringify(values) })
      message.success('阈值版本已保存，可在「注册模型」时选用')
      onSaved?.()
    } catch (e) {
      if (e instanceof ApiError) {
        message.error(`${e.code}: ${e.message}`)
        const vs = (e.details?.violations as { code: string; message: string }[] | undefined) ?? []
        setViolations(vs)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      {violations.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="违反安全约束，禁止保存"
          description={violations.map((v, i) => (
            <div key={i}>
              • [{v.code}] {v.message}
            </div>
          ))}
        />
      )}
      <Space size="large" wrap style={{ width: '100%', marginBottom: 16 }}>
        <div style={{ width: 250 }}>
          <Typography.Text type="secondary">default_min_confidence（{values.default_min_confidence.toFixed(2)}）</Typography.Text>
          <Slider min={0.3} max={0.99} step={0.01} value={values.default_min_confidence} onChange={(v) => setValues((s) => ({ ...s, default_min_confidence: v }))} />
        </div>
        <div style={{ width: 250 }}>
          <Typography.Text type="secondary">write_min_confidence（{values.write_min_confidence.toFixed(2)}）</Typography.Text>
          <Slider min={0.5} max={0.995} step={0.005} value={values.write_min_confidence} onChange={(v) => setValues((s) => ({ ...s, write_min_confidence: v }))} />
        </div>
        <div style={{ width: 250 }}>
          <Typography.Text type="secondary">oos_min_confidence（{values.oos_min_confidence.toFixed(2)}）</Typography.Text>
          <Slider min={0.3} max={0.99} step={0.01} value={values.oos_min_confidence} onChange={(v) => setValues((s) => ({ ...s, oos_min_confidence: v }))} />
        </div>
        <div style={{ width: 250 }}>
          <Typography.Text type="secondary">min_margin（{values.min_margin.toFixed(2)}）</Typography.Text>
          <Slider min={0} max={0.5} step={0.01} value={values.min_margin} onChange={(v) => setValues((s) => ({ ...s, min_margin: v }))} />
        </div>
      </Space>
      <Table
        size="small"
        pagination={false}
        loading={loading}
        dataSource={rows}
        columns={[
          { title: '指标', dataIndex: 'name' },
          {
            title: '值',
            dataIndex: 'value',
            width: 110,
            render: (v: string, r: { ok: boolean }) => (
              <Typography.Text type={r.ok ? undefined : 'danger'} strong>
                {v}
              </Typography.Text>
            ),
          },
        ]}
      />
      <div style={{ marginTop: 16 }}>
        <Button type="primary" disabled={!canSave} loading={saving} onClick={save}>
          保存为新阈值版本
        </Button>
        {!canSave && <Typography.Text type="secondary" style={{ marginLeft: 12 }}>存在红色违规项时禁止保存</Typography.Text>}
      </div>
    </div>
  )
}
