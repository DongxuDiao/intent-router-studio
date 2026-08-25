/** 发起训练：选择数据集 → 预设/参数 → 阈值约束 → 确认提交。 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Divider, Form, InputNumber, Segmented, Select, Space, Switch, Typography, message } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { DatasetStatusTag, PageHeader } from '../components/common'
import { useProject } from '../store/project'
import type { DatasetVersion, TrainingRun } from '../types'

const PRESETS: Record<string, { label: string; note: string; cfg: Record<string, number> }> = {
  quick: { label: 'quick（冒烟/演示）', note: 'epochs=2, iterations=10，几分钟内完成', cfg: { num_epochs: 2, num_iterations: 10, batch_size: 16 } },
  standard: { label: 'standard（常规）', note: 'epochs=5, iterations=20', cfg: { num_epochs: 5, num_iterations: 20, batch_size: 16 } },
  strict: { label: 'strict（更高质量）', note: 'epochs=10, iterations=30，耗时更长', cfg: { num_epochs: 10, num_iterations: 30, batch_size: 16 } },
}

const PARAM_BOUNDS = {
  batch_size: [4, 64],
  num_epochs: [1, 20],
  num_iterations: [1, 50],
  max_length: [64, 512],
  seed: [0, 2147483647],
} as const

export default function NewRun() {
  const { projectId } = useProject()
  const navigate = useNavigate()
  const [datasetId, setDatasetId] = useState<string | null>(null)
  const [preset, setPreset] = useState('quick')
  const [numEpochs, setNumEpochs] = useState(2)
  const [numIterations, setNumIterations] = useState(10)
  const [batchSize, setBatchSize] = useState(16)
  const [maxLength, setMaxLength] = useState(256)
  const [seed, setSeed] = useState(42)
  const [learningRate, setLearningRate] = useState(2e-5)
  const [maxFwr, setMaxFwr] = useState(0.005)
  const [minWp, setMinWp] = useState(0.95)
  const [name, setName] = useState('')

  const datasets = useQuery({
    queryKey: ['datasets', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: DatasetVersion[] }>(`/projects/${projectId}/datasets`),
  })

  const trainable = (d: DatasetVersion) =>
    d.status === 'FROZEN' && (d.quality_report?.errors.length ?? 0) === 0 && (d.unlabeled_count ?? 0) === 0
  const selected = datasets.data?.items.find((d) => d.id === datasetId)

  const submit = useMutation({
    mutationFn: () =>
      api<TrainingRun>(`/projects/${projectId}/runs`, {
        method: 'POST',
        body: JSON.stringify({
          dataset_version_id: datasetId,
          name: name || `run-${new Date().toISOString().slice(5, 16).replace('T', ' ')}`,
          config: {
            train: {
              seed,
              max_length: maxLength,
              batch_size: batchSize,
              num_epochs: numEpochs,
              body_learning_rate: learningRate,
              num_iterations: numIterations,
            },
            threshold_search: {
              constraints: { max_false_write_rate: maxFwr, min_write_precision: minWp },
            },
          },
        }),
      }),
    onSuccess: (run) => {
      message.success('已加入训练队列')
      navigate(`/runs/${run.id}`)
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
      else message.error('创建失败')
    },
  })

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />

  return (
    <div>
      <PageHeader title="发起训练" subTitle="SetFit（bge-small-zh-v1.5）→ 温度校准 → 约束阈值搜索 → 评估 → 制品打包" />
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card title="1. 选择数据集（仅 FROZEN、无错误、全标注的版本可训练）" size="small">
          <Select
            style={{ width: 480 }}
            placeholder="选择数据集版本"
            value={datasetId}
            onChange={setDatasetId}
            options={(datasets.data?.items ?? []).map((d) => ({
              value: d.id,
              label: `${d.name} v${d.version} · ${d.sample_count} 样本${trainable(d) ? '' : ' · ⚠️ 不可训练'}`,
              disabled: !trainable(d),
            }))}
          />
          {selected && (
            <Descriptions size="small" column={3} style={{ marginTop: 12 }}>
              <Descriptions.Item label="状态"><DatasetStatusTag status={selected.status} /></Descriptions.Item>
              <Descriptions.Item label="样本">{selected.sample_count}</Descriptions.Item>
              <Descriptions.Item label="标签分布">
                {Object.entries(selected.label_distribution).map(([k, v]) => `${k}:${v}`).join(' · ')}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Card>

        <Card title="2. 训练参数" size="small">
          <Segmented
            options={Object.entries(PRESETS).map(([k, p]) => ({ label: p.label, value: k }))}
            value={preset}
            onChange={(v) => {
              const key = v as string
              setPreset(key)
              const cfg = PRESETS[key].cfg
              setNumEpochs(cfg.num_epochs)
              setNumIterations(cfg.num_iterations)
              setBatchSize(cfg.batch_size)
            }}
            style={{ marginBottom: 16 }}
          />
          <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            {PRESETS[preset].note}。所有参数受白名单范围约束（超出将 422 拒绝）。
          </Typography.Text>
          <Space wrap size="large">
            <div>
              <Typography.Text type="secondary">num_epochs [{PARAM_BOUNDS.num_epochs.join(', ')}]</Typography.Text>
              <div><InputNumber min={PARAM_BOUNDS.num_epochs[0]} max={PARAM_BOUNDS.num_epochs[1]} value={numEpochs} onChange={(v) => setNumEpochs(v ?? 2)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">num_iterations [{PARAM_BOUNDS.num_iterations.join(', ')}]</Typography.Text>
              <div><InputNumber min={PARAM_BOUNDS.num_iterations[0]} max={PARAM_BOUNDS.num_iterations[1]} value={numIterations} onChange={(v) => setNumIterations(v ?? 10)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">batch_size [{PARAM_BOUNDS.batch_size.join(', ')}]</Typography.Text>
              <div><InputNumber min={PARAM_BOUNDS.batch_size[0]} max={PARAM_BOUNDS.batch_size[1]} value={batchSize} onChange={(v) => setBatchSize(v ?? 16)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">max_length [{PARAM_BOUNDS.max_length.join(', ')}]</Typography.Text>
              <div><InputNumber min={PARAM_BOUNDS.max_length[0]} max={PARAM_BOUNDS.max_length[1]} step={32} value={maxLength} onChange={(v) => setMaxLength(v ?? 256)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">seed</Typography.Text>
              <div><InputNumber min={0} max={PARAM_BOUNDS.seed[1]} value={seed} onChange={(v) => setSeed(v ?? 42)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">body_learning_rate [1e-6, 1e-4]</Typography.Text>
              <div><InputNumber min={1e-6} max={1e-4} step={1e-6} value={learningRate} onChange={(v) => setLearningRate(v ?? 2e-5)} style={{ width: 140 }} /></div>
            </div>
          </Space>
        </Card>

        <Card title="3. 阈值搜索安全约束（硬约束，不满足则回退保守阈值）" size="small">
          <Space size="large">
            <div>
              <Typography.Text type="secondary">max_false_write_rate（默认 0.005）</Typography.Text>
              <div><InputNumber min={0} max={0.05} step={0.001} value={maxFwr} onChange={(v) => setMaxFwr(v ?? 0.005)} /></div>
            </div>
            <div>
              <Typography.Text type="secondary">min_write_precision（默认 0.95）</Typography.Text>
              <div><InputNumber min={0.5} max={1} step={0.01} value={minWp} onChange={(v) => setMinWp(v ?? 0.95)} /></div>
            </div>
          </Space>
        </Card>

        <Card title="4. 确认提交" size="small">
          <pre style={{ background: '#fafafa', padding: 12, borderRadius: 6, fontSize: 12, maxHeight: 260, overflow: 'auto' }}>
{JSON.stringify(
  {
    dataset_version_id: datasetId,
    config: {
      train: { seed, max_length: maxLength, batch_size: batchSize, num_epochs: numEpochs, body_learning_rate: learningRate, num_iterations: numIterations },
      threshold_search: { constraints: { max_false_write_rate: maxFwr, min_write_precision: minWp } },
    },
  },
  null,
  2,
)}
          </pre>
          <Button type="primary" size="large" disabled={!datasetId} loading={submit.isPending} onClick={() => submit.mutate()}>
            提交训练（进入队列，Worker 异步执行）
          </Button>
        </Card>
      </Space>
    </div>
  )
}
