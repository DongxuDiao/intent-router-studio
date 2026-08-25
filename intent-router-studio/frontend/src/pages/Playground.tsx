/** Playground：单条推理 / 批量 / A-B 对比，案例沉淀（默认不存原文）。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Descriptions, Input, Row, Segmented, Select, Space, Switch, Table, Tag, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { LabelTag, PageHeader, ProbBars } from '../components/common'
import { QueryRewritePanel } from '../components/rewrite'
import { useProject } from '../store/project'
import { EFFECT_CEILING_NAMES, GATE_NAMES, LABELS, LABEL_NAMES } from '../types'
import type { ModelVersion, PredictResult } from '../types'
import { fmtMs, fmtTime } from '../utils/format'
import { patchPlaygroundCache, readPlaygroundCache, type PlaygroundTab } from '../utils/playgroundCache'

function ResultPanel({ title, r }: { title: string; r: PredictResult | null }) {
  if (!r) return <Card title={title} size="small"><Typography.Text type="secondary">待推理</Typography.Text></Card>
  const accept = r.decision === 'accept'
  return (
    <Card
      title={title}
      size="small"
      extra={accept ? <Tag color={r.route === 'write_action' ? 'warning' : 'success'}>accept · {LABEL_NAMES[r.route] ?? r.route}</Tag> : <Tag color="purple">unclear · 转人工</Tag>}
    >
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <ProbBars topK={r.top_k} />
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="confidence">{r.confidence.toFixed(4)}</Descriptions.Item>
          <Descriptions.Item label="margin">{r.margin.toFixed(4)}</Descriptions.Item>
          <Descriptions.Item label="效果上限">{EFFECT_CEILING_NAMES[r.effect_ceiling] ?? r.effect_ceiling}</Descriptions.Item>
          <Descriptions.Item label="required_next_gate">{GATE_NAMES[r.required_next_gate] ?? r.required_next_gate}</Descriptions.Item>
          <Descriptions.Item label="延迟">{fmtMs(r.latency_ms)}{r.cache_hit ? '（缓存命中）' : ''}</Descriptions.Item>
          <Descriptions.Item label="模型">{r.model_version}</Descriptions.Item>
        </Descriptions>
        {r.reason_codes.length > 0 && (
          <div>
            {r.reason_codes.map((c) => (
              <Tag key={c} color={c.includes('THRESHOLD') || c.includes('CONFIDENCE') || c.includes('MARGIN') ? 'orange' : 'default'}>{c}</Tag>
            ))}
          </div>
        )}
        {r.route === 'write_action' && accept && (
          <Alert type="warning" showIcon message="write_action 仅为候选：需用户显式确认后才可能进入写流程，本系统不执行任何外部写入" />
        )}
      </Space>
    </Card>
  )
}

export default function Playground() {
  const { projectId } = useProject()
  const qc = useQueryClient()
  const initial = readPlaygroundCache(projectId)
  const [tab, setTab] = useState<PlaygroundTab>(initial?.tab ?? 'single')
  const [text, setText] = useState(initial?.text ?? '帮我把这个任务的负责人改成张三')
  const [context, setContext] = useState(initial?.context ?? '')
  const [expected, setExpected] = useState<string | null>(initial?.expected ?? null)
  const [saveText, setSaveText] = useState(initial?.saveText ?? false)
  const [debug, setDebug] = useState(initial?.debug ?? false)
  const [batchText, setBatchText] = useState(initial?.batchText ?? '查看今天的日程\n删除所有已完成任务\n这个…那个…算了\n今天天气怎么样')
  const [modelB, setModelB] = useState<string | null>(initial?.modelB ?? null)
  const [lastPredict, setLastPredict] = useState<PredictResult | null>(initial?.singleResult ?? null)
  const [lastBatch, setLastBatch] = useState<{ count: number; results: PredictResult[] } | null>(initial?.batchResult ?? null)
  const [lastCompare, setLastCompare] = useState<{ a: PredictResult; b: PredictResult } | null>(initial?.compareResult ?? null)

  const models = useQuery({
    queryKey: ['models', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: ModelVersion[] }>(`/projects/${projectId}/models`),
  })
  const cases = useQuery({
    queryKey: ['playground-cases', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: { id: string; text: string | null; text_hash: string; expected_label: string | null; predicted_route: string | null; is_correct: boolean | null; created_at: string }[] }>(`/projects/${projectId}/playground-cases`),
  })

  const predict = useMutation({
    mutationFn: () =>
      api<PredictResult>('/inference/predict', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, text, context: context || null, debug }),
      }),
    onSuccess: (data) => {
      setLastPredict(data)
      patchPlaygroundCache(projectId, { singleResult: data })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    },
  })
  const batch = useMutation({
    mutationFn: () =>
      api<{ count: number; results: PredictResult[] }>('/inference/batch', {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          items: batchText.split('\n').map((t) => t.trim()).filter(Boolean).map((t) => ({ text: t })),
        }),
      }),
    onSuccess: (data) => {
      setLastBatch(data)
      patchPlaygroundCache(projectId, { batchResult: data })
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '批量失败'),
  })
  const compare = useMutation({
    mutationFn: () =>
      api<{ a: PredictResult; b: PredictResult }>('/inference/compare', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, text, context: context || null, model_b: modelB }),
      }),
    onSuccess: (data) => {
      setLastCompare(data)
      patchPlaygroundCache(projectId, { compareResult: data })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    },
  })
  const saveCase = useMutation({
    mutationFn: (predictedRoute: string) =>
      api(`/projects/${projectId}/playground-cases`, {
        method: 'POST',
        body: JSON.stringify({
          text,
          context: context || null,
          expected_label: expected,
          predicted_route: predictedRoute,
          save_text: saveText,
        }),
      }),
    onSuccess: () => {
      message.success(`案例已沉淀${saveText ? '（含原文）' : '（仅哈希，不含原文）'}`)
      qc.invalidateQueries({ queryKey: ['playground-cases', projectId] })
    },
  })

  useEffect(() => {
    const saved = readPlaygroundCache(projectId)
    setTab(saved?.tab ?? 'single')
    setText(saved?.text ?? '帮我把这个任务的负责人改成张三')
    setContext(saved?.context ?? '')
    setExpected(saved?.expected ?? null)
    setSaveText(saved?.saveText ?? false)
    setDebug(saved?.debug ?? false)
    setBatchText(saved?.batchText ?? '查看今天的日程\n删除所有已完成任务\n这个…那个…算了\n今天天气怎么样')
    setModelB(saved?.modelB ?? null)
    setLastPredict(saved?.singleResult ?? null)
    setLastBatch(saved?.batchResult ?? null)
    setLastCompare(saved?.compareResult ?? null)
    predict.reset()
    batch.reset()
    compare.reset()
  // mutations are stable for the component lifetime; projectId is the hydration boundary.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />
  const activeModel = models.data?.items.find((m) => m.status === 'ACTIVE')
  if (!activeModel) {
    return (
      <Alert
        type="warning"
        showIcon
        message="尚无激活模型"
        description="Playground 依赖激活模型。请先完成一次训练 → 注册 → 激活。"
      />
    )
  }

  return (
    <div>
      <PageHeader
        title="Playground"
        subTitle={`激活模型 ${activeModel.name} · 单条推理带缓存与原因码；批量逐行；A/B 对比；Query 理解含改写双路评估`}
      />
      <Segmented
        options={[
          { label: '单条', value: 'single' },
          { label: '批量', value: 'batch' },
          { label: 'A / B 对比', value: 'ab' },
          { label: 'Query 理解', value: 'rewrite' },
        ]}
        value={tab}
        onChange={(v) => {
          const next = v as PlaygroundTab
          setTab(next)
          patchPlaygroundCache(projectId, { tab: next })
        }}
        style={{ marginBottom: 16 }}
      />

      {tab === 'single' && (
        <Row gutter={16}>
          <Col span={12}>
            <Card title="输入" size="small">
              <Space direction="vertical" style={{ width: '100%' }} size={12}>
                <Input.TextArea rows={3} value={text} onChange={(e) => { setText(e.target.value); patchPlaygroundCache(projectId, { text: e.target.value }) }} placeholder="用户 query" />
                <Input.TextArea rows={2} value={context} onChange={(e) => { setContext(e.target.value); patchPlaygroundCache(projectId, { context: e.target.value }) }} placeholder="context（多轮上文，可选）" />
                <Space wrap>
                  <Button type="primary" loading={predict.isPending} onClick={() => predict.mutate()}>
                    推理
                  </Button>
                  <span>
                    <Switch size="small" checked={debug} onChange={(v) => { setDebug(v); patchPlaygroundCache(projectId, { debug: v }) }} /> <Typography.Text type="secondary">debug 信息</Typography.Text>
                  </span>
                </Space>
                <Card type="inner" title="沉淀案例（进入后续评测集）" size="small">
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <select value={expected ?? ''} onChange={(e) => { const v = e.target.value || null; setExpected(v); patchPlaygroundCache(projectId, { expected: v }) }} style={{ padding: '4px 8px' }}>
                      <option value="">期望标签（可选）</option>
                      {LABELS.map((l) => (
                        <option key={l} value={l}>{l} · {LABEL_NAMES[l]}</option>
                      ))}
                    </select>
                    <span>
                      <Switch size="small" checked={saveText} onChange={(v) => { setSaveText(v); patchPlaygroundCache(projectId, { saveText: v }) }} /> <Typography.Text type="secondary">保存原文（默认只存哈希）</Typography.Text>
                    </span>
                    <Button
                      size="small"
                      disabled={!lastPredict || predict.isPending}
                      onClick={() => lastPredict && saveCase.mutate(lastPredict.route)}
                      loading={saveCase.isPending}
                    >
                      保存案例
                    </Button>
                  </Space>
                </Card>
              </Space>
            </Card>
          </Col>
          <Col span={12}>
            <ResultPanel title="路由结果" r={lastPredict} />
            {lastPredict?.debug && (
              <Card title="debug" size="small" style={{ marginTop: 12 }}>
                <pre style={{ fontSize: 11 }}>{JSON.stringify(lastPredict.debug, null, 2)}</pre>
              </Card>
            )}
          </Col>
        </Row>
      )}

      {tab === 'batch' && (
        <Row gutter={16}>
          <Col span={10}>
            <Card title="批量输入（每行一条）" size="small">
              <Input.TextArea rows={12} value={batchText} onChange={(e) => { setBatchText(e.target.value); patchPlaygroundCache(projectId, { batchText: e.target.value }) }} />
              <Button type="primary" block style={{ marginTop: 12 }} loading={batch.isPending} onClick={() => batch.mutate()}>
                批量推理
              </Button>
            </Card>
          </Col>
          <Col span={14}>
            <Card title={`结果（${lastBatch?.count ?? 0} 条）`} size="small">
              <Table
                size="small"
                pagination={{ pageSize: 20 }}
                dataSource={(lastBatch?.results ?? []).map((r, i) => ({ key: i, ...r }))}
                columns={[
                  {
                    title: 'route',
                    dataIndex: 'route',
                    width: 110,
                    render: (l: string, r: PredictResult) => (r.decision === 'unclear' ? <Tag color="purple">unclear</Tag> : <LabelTag label={l} />),
                  },
                  { title: 'confidence', dataIndex: 'confidence', width: 100, render: (v: number) => v.toFixed(3) },
                  { title: 'margin', dataIndex: 'margin', width: 80, render: (v: number) => v.toFixed(3) },
                  { title: '延迟', dataIndex: 'latency_ms', width: 90, render: fmtMs },
                  { title: '原因码', dataIndex: 'reason_codes', render: (cs: string[]) => cs.map((c) => <Tag key={c} style={{ fontSize: 10 }}>{c}</Tag>) },
                ]}
              />
            </Card>
          </Col>
        </Row>
      )}

      {tab === 'rewrite' && <QueryRewritePanel projectId={projectId} />}

      {tab === 'ab' && (
        <div>
          <Card size="small" style={{ marginBottom: 16 }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input value={text} onChange={(e) => { setText(e.target.value); patchPlaygroundCache(projectId, { text: e.target.value }) }} placeholder="query" />
              <Input value={context} onChange={(e) => { setContext(e.target.value); patchPlaygroundCache(projectId, { context: e.target.value }) }} placeholder="context（可选）" />
              <Space>
                <Typography.Text type="secondary">A = 激活模型（{activeModel.name}）</Typography.Text>
                <Select
                  style={{ width: 320 }}
                  placeholder="B 模型（对比目标）"
                  value={modelB ?? undefined}
                  onChange={(v) => { setModelB(v); patchPlaygroundCache(projectId, { modelB: v }) }}
                  options={(models.data?.items ?? [])
                    .filter((m) => m.id !== activeModel.id)
                    .map((m) => ({ value: m.id, label: `${m.name}（${m.status}）` }))}
                />
                <Button type="primary" disabled={!modelB} loading={compare.isPending} onClick={() => compare.mutate()}>
                  对比推理
                </Button>
              </Space>
            </Space>
          </Card>
          <Row gutter={16}>
            <Col span={12}><ResultPanel title="A（激活）" r={lastCompare?.a ?? null} /></Col>
            <Col span={12}><ResultPanel title="B（对比）" r={lastCompare?.b ?? null} /></Col>
          </Row>
        </div>
      )}

      <Card title={`已沉淀案例（${cases.data?.items.length ?? 0}）`} size="small" style={{ marginTop: 16 }}>
        <Table
          size="small"
          pagination={{ pageSize: 8 }}
          dataSource={cases.data?.items ?? []}
          rowKey="id"
          columns={[
            { title: '文本/哈希', dataIndex: 'text', ellipsis: true, render: (t: string | null, r: { text_hash: string }) => t ?? `${r.text_hash.slice(0, 20)}…（原文未存）` },
            { title: '期望', dataIndex: 'expected_label', width: 110, render: (l: string | null) => (l ? <LabelTag label={l} /> : '-') },
            { title: '预测', dataIndex: 'predicted_route', width: 110, render: (l: string | null) => (l ? (l === 'unclear' ? <Tag color="purple">unclear</Tag> : <LabelTag label={l} />) : '-') },
            { title: '正确', dataIndex: 'is_correct', width: 80, render: (b: boolean | null) => (b === null ? '-' : b ? <Tag color="success">✓</Tag> : <Tag color="error">✗</Tag>) },
            { title: '时间', dataIndex: 'created_at', width: 140, render: fmtTime },
          ]}
        />
      </Card>
    </div>
  )
}
