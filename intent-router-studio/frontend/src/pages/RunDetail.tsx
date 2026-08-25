/** Run 详情：实时进度（SSE）、指标、阈值调节、错误分析、注册模型。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Descriptions, Modal, Popconfirm, Progress, Radio, Row, Space, Table, Tabs, Tag, Typography, message } from 'antd'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { LabelTag, MetricCard, PageHeader, ProbBars, RunStatusTag } from '../components/common'
import { ChartBoundary, LazyConfusionMatrixChart, LazyDistributionChart, LazyReliabilityChart, LazyThresholdCurveChart } from '../components/lazy-charts'
import { LogStream } from '../components/LogStream'
import { ThresholdSimulator } from '../components/ThresholdSimulator'
import { useProject } from '../store/project'
import { RUN_STAGES, STAGE_NAMES } from '../types'
import type { ErrorSample, ModelVersion, RunMetrics, SplitEval, ThresholdVersionInfo, TrainingRun } from '../types'
import { downloadCsv, fmtMs, fmtPercent, fmtTime, toCsv } from '../utils/format'

function RouteMetricsCards({ m }: { m: SplitEval['routing'] }) {
  return (
    <Row gutter={[12, 12]}>
      <Col span={4}><MetricCard title="coverage" value={fmtPercent(m.coverage)} /></Col>
      <Col span={4}><MetricCard title="safe_coverage" value={fmtPercent(m.safe_coverage)} tone="success" /></Col>
      <Col span={4}><MetricCard title="selective_acc" value={fmtPercent(m.selective_accuracy)} /></Col>
      <Col span={4}><MetricCard title="false_write_rate" value={fmtPercent(m.false_write_rate)} tone={m.false_write_rate && m.false_write_rate > 0.005 ? 'danger' : undefined} /></Col>
      <Col span={4}><MetricCard title="unclear_rate" value={fmtPercent(m.unclear_rate)} /></Col>
      <Col span={4}><MetricCard title="write_precision" value={fmtPercent(m.write_precision)} /></Col>
    </Row>
  )
}

function SplitEvalView({ title, evalData }: { title: string; evalData: SplitEval }) {
  return (
    <Card title={title} size="small" style={{ marginBottom: 16 }}>
      <RouteMetricsCards m={evalData.routing} />
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card type="inner" title="分类指标（raw 预测，不含 unclear 路由）" size="small">
            <Descriptions size="small" column={2}>
              <Descriptions.Item label="accuracy">{fmtNumber3(evalData.classification.accuracy)}</Descriptions.Item>
              <Descriptions.Item label="macro_f1">{fmtNumber3(evalData.classification.macro_f1)}</Descriptions.Item>
            </Descriptions>
            <Table
              size="small"
              pagination={false}
              style={{ marginTop: 8 }}
              dataSource={evalData.classification.per_class}
              rowKey="label"
              columns={[
                { title: '标签', dataIndex: 'label', width: 110, render: (l: string) => <LabelTag label={l} /> },
                { title: 'precision', dataIndex: 'precision', render: fmtNumber3 },
                { title: 'recall', dataIndex: 'recall', render: fmtNumber3 },
                { title: 'f1', dataIndex: 'f1', render: fmtNumber3 },
                { title: 'support', dataIndex: 'support', width: 80 },
              ]}
            />
          </Card>
        </Col>
        <Col span={12}>
          <Card type="inner" title="混淆矩阵" size="small">
            <ChartBoundary height={360}><LazyConfusionMatrixChart labels={evalData.classification.confusion_matrix.labels} matrix={evalData.classification.confusion_matrix.matrix} /></ChartBoundary>
          </Card>
        </Col>
      </Row>
      <Card type="inner" title="误写置信区间（Wilson 95%）" size="small" style={{ marginTop: 12 }}>
        <Typography.Text>
          test 上接受 write_action 且真实非写：{evalData.false_write_confidence_interval.false_write_count} /{' '}
          {evalData.false_write_confidence_interval.non_write_support}
          （rate {fmtPercent(evalData.false_write_confidence_interval.rate)}，CI [
          {fmtPercent(evalData.false_write_confidence_interval.wilson_95?.[0])},{' '}
          {fmtPercent(evalData.false_write_confidence_interval.wilson_95?.[1])}]）
        </Typography.Text>
      </Card>
    </Card>
  )
}

function fmtNumber3(v: number | null | undefined): string {
  return v === null || v === undefined ? '-' : v.toFixed(3)
}

export default function RunDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { projectId } = useProject()
  const [registerOpen, setRegisterOpen] = useState(false)
  const [chosenThreshold, setChosenThreshold] = useState<string | undefined>(undefined)
  const [modelName, setModelName] = useState('')

  const run = useQuery({
    queryKey: ['run', id],
    queryFn: () => api<TrainingRun>(`/runs/${id}`),
    refetchInterval: (q) => (['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'].includes(q.state.data?.status ?? '') ? false : 2500),
  })
  const metrics = useQuery({
    queryKey: ['run-metrics', id],
    queryFn: () => api<RunMetrics>(`/runs/${id}/metrics`),
    enabled: run.data?.status === 'SUCCEEDED',
  })
  const errors = useQuery({
    queryKey: ['run-errors', id],
    queryFn: () => api<{ total: number; errors: ErrorSample[] }>(`/runs/${id}/errors?page_size=100`),
    enabled: run.data?.status === 'SUCCEEDED',
  })
  const thresholdVersions = useQuery({
    queryKey: ['threshold-versions', id],
    queryFn: () => api<{ items: ThresholdVersionInfo[] }>(`/runs/${id}/threshold-versions`),
    enabled: run.data?.status === 'SUCCEEDED',
  })
  const models = useQuery({
    queryKey: ['models', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: ModelVersion[] }>(`/projects/${projectId}/models`),
  })

  const cancel = useMutation({
    mutationFn: () => api(`/runs/${id}/cancel`, { method: 'POST' }),
    onSuccess: () => {
      message.success('已请求取消（QUEUED 直接取消；运行中将在安全点停止）')
      qc.invalidateQueries({ queryKey: ['run', id] })
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '取消失败'),
  })
  const retry = useMutation({
    mutationFn: () => api<TrainingRun>(`/runs/${id}/retry`, { method: 'POST' }),
    onSuccess: (r) => {
      message.success('已创建重试 Run')
      navigate(`/runs/${r.id}`)
    },
  })
  const register = useMutation({
    mutationFn: () =>
      api<ModelVersion>(`/runs/${id}/register-model`, {
        method: 'POST',
        body: JSON.stringify({ threshold_version_id: chosenThreshold ?? null, name: modelName || null }),
      }),
    onSuccess: (m) => {
      setRegisterOpen(false)
      message.success(`模型已注册为 ${m.status}：${m.id}`)
      qc.invalidateQueries({ queryKey: ['models', projectId] })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    },
  })
  const backflow = useMutation({
    mutationFn: () =>
      api<{ id: string }>(`/runs/${id}/errors/draft`, {
        method: 'POST',
        body: JSON.stringify({
          name: `错误回流 ${new Date().toISOString().slice(5, 16)}`,
          changes: (errors.data?.errors ?? []).slice(0, 500).map((e) => ({
            action: 'add' as const,
            text: e.text,
            label: e.true_label,
            context: e.context,
            group_id: e.group_id,
            risk_slice: e.risk_slice,
            source: `error_backflow:${id}`,
          })),
        }),
      }),
    onSuccess: (d: { id: string }) => {
      message.success(`已创建错误回流草稿，可在标注台复核后提交（新数据集 ${d.id}）`)
      navigate(`/datasets/${d.id}/label`)
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '回流失败'),
  })

  if (run.isLoading) return <Card loading />
  const r = run.data
  if (!r) return <Alert type="error" message="Run 不存在" />
  const active = !['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'].includes(r.status)
  const m = metrics.data
  const registeredModelIds = new Set(models.data?.items.filter((mv) => mv.run_id === r.id).map((mv) => mv.id) ?? [])
  const hasRegistered = registeredModelIds.size > 0

  return (
    <div>
      <PageHeader
        title={r.name || r.id}
        subTitle={`${r.id} · 数据集 ${r.dataset_id}`}
        extra={
          <Space>
            {active && <Popconfirm title="确认取消该训练？" onConfirm={() => cancel.mutate()}><Button danger loading={cancel.isPending}>取消</Button></Popconfirm>}
            {['FAILED', 'CANCELLED', 'INTERRUPTED'].includes(r.status) && (
              <Button onClick={() => retry.mutate()} loading={retry.isPending}>重试（新 Run）</Button>
            )}
            {r.status === 'SUCCEEDED' && !hasRegistered && (
              <Button type="primary" onClick={() => setRegisterOpen(true)}>注册模型</Button>
            )}
            {r.status === 'SUCCEEDED' && (
              <Button onClick={() => backflow.mutate()} loading={backflow.isPending} disabled={(errors.data?.total ?? 0) === 0}>
                错误样本回流（{errors.data?.total ?? 0}）
              </Button>
            )}
          </Space>
        }
      />

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          <Space wrap>
            <RunStatusTag status={r.status} />
            <Tag>{r.stage ? STAGE_NAMES[r.stage] ?? r.stage : '未开始'}</Tag>
            {r.cancel_requested && <Tag color="warning">取消已请求</Tag>}
            <Typography.Text type="secondary">
              worker {r.worker_id ?? '-'} · 创建 {fmtTime(r.created_at)} · 开始 {fmtTime(r.started_at)} · 结束 {fmtTime(r.finished_at)}
            </Typography.Text>
          </Space>
          <Progress
            percent={r.progress}
            status={r.status === 'FAILED' ? 'exception' : r.status === 'SUCCEEDED' ? 'success' : 'active'}
          />
          <Space wrap size={4}>
            {RUN_STAGES.map((s, i) => (
              <Tag key={s} color={r.stage === s ? 'processing' : i < (r.stage_index ?? -1) ? 'success' : 'default'} style={{ fontSize: 11 }}>
                {i + 1}.{STAGE_NAMES[s]}
              </Tag>
            ))}
          </Space>
          {r.error && (
            <Alert type="error" message={`[${r.error.code}] ${r.error.message}`} />
          )}
        </Space>
      </Card>

      <Tabs
        items={[
          {
            key: 'log',
            label: '实时日志（SSE）',
            children: (
              <Card size="small">
                <LogStream runId={r.id} active={active} />
              </Card>
            ),
          },
          {
            key: 'config',
            label: '配置',
            children: (
              <Card size="small">
                <pre style={{ fontSize: 12, maxHeight: 500, overflow: 'auto' }}>{JSON.stringify(r.config, null, 2)}</pre>
              </Card>
            ),
          },
          {
            key: 'metrics',
            label: '指标',
            children: m?.available ? (
              <div>
                {m.test && <SplitEvalView title="Test 集评估（final 路由）" evalData={m.test} />}
                {m.validation && (
                  <Card title="Validation 集路由指标" size="small" style={{ marginBottom: 16 }}>
                    <RouteMetricsCards m={m.validation.routing} />
                  </Card>
                )}
                <Row gutter={16} style={{ marginBottom: 16 }}>
                  <Col span={12}>
                    <Card title="温度校准" size="small">
                      {m.calibration ? (
                        <>
                          <Descriptions size="small" column={3}>
                            <Descriptions.Item label="温度 T">{m.calibration.temperature.toFixed(4)}</Descriptions.Item>
                            <Descriptions.Item label="NLL 前→后">
                              {m.calibration.before.nll.toFixed(4)} → {m.calibration.after.nll.toFixed(4)}
                            </Descriptions.Item>
                            <Descriptions.Item label="ECE 前→后">
                              {m.calibration.before.ece.toFixed(4)} → {m.calibration.after.ece.toFixed(4)}
                            </Descriptions.Item>
                          </Descriptions>
                          <ChartBoundary height={300}><LazyReliabilityChart before={m.calibration.reliability_before} after={m.calibration.reliability_after} /></ChartBoundary>
                        </>
                      ) : (
                        <Typography.Text type="secondary">无校准数据</Typography.Text>
                      )}
                    </Card>
                  </Col>
                  <Col span={12}>
                    <Card title="阈值（约束搜索结果）" size="small">
                      {m.thresholds && (
                        <Descriptions size="small" column={2}>
                          <Descriptions.Item label="default_min_confidence">{m.thresholds.default_min_confidence}</Descriptions.Item>
                          <Descriptions.Item label="write_min_confidence">{m.thresholds.write_min_confidence}</Descriptions.Item>
                          <Descriptions.Item label="oos_min_confidence">{m.thresholds.oos_min_confidence}</Descriptions.Item>
                          <Descriptions.Item label="min_margin">{m.thresholds.min_margin}</Descriptions.Item>
                        </Descriptions>
                      )}
                      {m.threshold_search && (
                        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
                          可行组合 {m.threshold_search.n_feasible}/{m.threshold_search.n_candidates}
                          {m.threshold_search.n_retained_candidates !== undefined
                            ? `；保留候选 ${m.threshold_search.n_retained_candidates}`
                            : ''}
                          {m.threshold_search.n_tied !== undefined ? `；并列 ${m.threshold_search.n_tied}` : ''}
                          ；pareto 点 {m.threshold_search.pareto.length}
                          {!m.threshold_search.feasible && '（无可行解，已回退保守默认阈值）'}
                        </Typography.Paragraph>
                      )}
                      {m.latency && (
                        <Descriptions size="small" column={2} style={{ marginTop: 8 }}>
                          <Descriptions.Item label="延迟 p50">{fmtMs(m.latency.p50)}</Descriptions.Item>
                          <Descriptions.Item label="p95">{fmtMs(m.latency.p95)}</Descriptions.Item>
                          <Descriptions.Item label="p99">{fmtMs(m.latency.p99)}</Descriptions.Item>
                          <Descriptions.Item label="mean">{fmtMs(m.latency.mean)}</Descriptions.Item>
                        </Descriptions>
                      )}
                    </Card>
                  </Col>
                </Row>
                {m.threshold_search?.curves && (
                  <Row gutter={16} style={{ marginBottom: 16 }}>
                    {Object.entries(m.threshold_search.curves)
                      .filter(([, pts]) => pts && pts.length > 1)
                      .slice(0, 2)
                      .map(([axis, pts]) => (
                        <Col span={12} key={axis}>
                          <Card title={`阈值敏感度：${axis}`} size="small">
                            <ChartBoundary height={320}><LazyThresholdCurveChart points={pts} /></ChartBoundary>
                          </Card>
                        </Col>
                      ))}
                  </Row>
                )}
                {m.distributions && (
                  <Row gutter={16}>
                    <Col span={12}>
                      <Card size="small">
                        <ChartBoundary height={280}><LazyDistributionChart edges={m.distributions.confidence.edges} counts={m.distributions.confidence.counts} title="置信度分布（test）" /></ChartBoundary>
                      </Card>
                    </Col>
                    <Col span={12}>
                      <Card size="small">
                        <ChartBoundary height={280}><LazyDistributionChart edges={m.distributions.margin.edges} counts={m.distributions.margin.counts} title="Margin 分布（test）" /></ChartBoundary>
                      </Card>
                    </Col>
                  </Row>
                )}
                {m.slices && Object.keys(m.slices).length > 0 && (
                  <Card title="切片分析" size="small" style={{ marginTop: 16 }}>
                    <Table
                      size="small"
                      pagination={false}
                      dataSource={Object.entries(m.slices).map(([k, v]) => ({ key: k, name: k, ...v }))}
                      columns={[
                        { title: '切片', dataIndex: 'name' },
                        { title: 'support', dataIndex: 'support', width: 80 },
                        { title: 'macro_f1', dataIndex: 'macro_f1', render: fmtNumber3 },
                        { title: 'accuracy', dataIndex: 'accuracy', render: fmtNumber3 },
                        { title: 'false_write', dataIndex: 'false_write_count', width: 100 },
                        { title: 'coverage', dataIndex: 'coverage', render: (v: number | null) => fmtPercent(v) },
                      ]}
                    />
                  </Card>
                )}
              </div>
            ) : (
              <Alert type="info" showIcon message="指标在 Run 成功后可用" />
            ),
          },
          {
            key: 'thresholds',
            label: '阈值调节',
            children: m?.available && m.thresholds ? (
              <Card size="small">
                <Typography.Paragraph type="secondary">
                  在 validation 预测上实时模拟；false_write_rate ≤ {fmtPercent(0.005)} 且 write_precision ≥ {fmtPercent(0.95)} 才允许保存。
                </Typography.Paragraph>
                <ThresholdSimulator
                  runId={r.id}
                  initial={m.thresholds}
                  constraints={{ max_false_write_rate: 0.005, min_write_precision: 0.95 }}
                  onSaved={() => {
                    qc.invalidateQueries({ queryKey: ['threshold-versions', id] })
                    qc.invalidateQueries({ queryKey: ['run-metrics', id] })
                  }}
                />
                <Typography.Title level={5} style={{ marginTop: 24 }}>版本历史</Typography.Title>
                <Table
                  size="small"
                  dataSource={thresholdVersions.data?.items ?? []}
                  rowKey="id"
                  pagination={false}
                  columns={[
                    { title: '版本', dataIndex: 'version', width: 60 },
                    { title: '来源', dataIndex: 'source', width: 90 },
                    { title: 'config', render: (_, v: ThresholdVersionInfo) => JSON.stringify(v.config) },
                    { title: 'safe_coverage', width: 110, render: (_, v: ThresholdVersionInfo) => fmtPercent(v.metrics?.safe_coverage) },
                    { title: 'false_write_rate', width: 110, render: (_, v: ThresholdVersionInfo) => fmtPercent(v.metrics?.false_write_rate) },
                    { title: '创建', dataIndex: 'created_at', width: 140, render: fmtTime },
                  ]}
                />
              </Card>
            ) : (
              <Alert type="info" showIcon message="Run 成功后可调节阈值" />
            ),
          },
          {
            key: 'errors',
            label: `错误分析（${errors.data?.total ?? 0}）`,
            children: (
              <Card size="small">
                {errors.data && errors.data.total > 0 ? (
                  <>
                    <Space style={{ marginBottom: 8 }}>
                      <Typography.Text type="secondary">
                        raw 错判或最终路由错误的样本，按 margin 升序（最危险的排前）：
                      </Typography.Text>
                      <Button
                        size="small"
                        onClick={() =>
                          downloadCsv(
                            `errors-${r.id}.csv`,
                            toCsv(errors.data!.errors as unknown as Record<string, unknown>[], [
                              'sample_id', 'text', 'true_label', 'raw_prediction', 'final_route', 'decision', 'margin', 'reason_codes', 'risk_slice', 'split',
                            ]),
                          )
                        }
                      >
                        导出 CSV
                      </Button>
                    </Space>
                    <Table
                      size="small"
                      dataSource={errors.data.errors}
                      rowKey="sample_id"
                      pagination={{ pageSize: 20, total: errors.data.total }}
                      columns={[
                        { title: '文本', dataIndex: 'text', ellipsis: true },
                        { title: '真实', dataIndex: 'true_label', width: 100, render: (l: string) => <LabelTag label={l} /> },
                        { title: 'raw', dataIndex: 'raw_prediction', width: 100, render: (l: string) => <LabelTag label={l} /> },
                        { title: '最终路由', dataIndex: 'final_route', width: 100, render: (l: string) => (l === 'unclear' ? <Tag color="purple">unclear</Tag> : <LabelTag label={l} />) },
                        { title: 'margin', dataIndex: 'margin', width: 80, render: (v: number) => v.toFixed(3) },
                        { title: '原因码', dataIndex: 'reason_codes', width: 160, render: (cs: string[]) => cs.map((c) => <Tag key={c} style={{ fontSize: 10 }}>{c}</Tag>) },
                        { title: 'split', dataIndex: 'split', width: 90 },
                      ]}
                      expandable={{
                        expandedRowRender: (e: ErrorSample) => (
                          <div style={{ maxWidth: 520 }}>
                            {e.context && <Typography.Paragraph type="secondary">context：{e.context}</Typography.Paragraph>}
                            <ProbBars topK={e.top_k} />
                          </div>
                        ),
                      }}
                    />
                  </>
                ) : (
                  <Alert type="success" showIcon message="无错误样本 🎉" />
                )}
              </Card>
            ),
          },
        ]}
      />

      <Modal
        title="注册模型（进入模型注册表）"
        open={registerOpen}
        onCancel={() => setRegisterOpen(false)}
        onOk={() => register.mutate()}
        confirmLoading={register.isPending}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          注册时会校验制品哈希（manifest verify）并复制到模型库；激活前还会做冒烟预测。write_action 仍只是候选，不授予执行权。
        </Typography.Paragraph>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="模型名">
            <input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="如 bge-small-2026-08-21" style={{ padding: '4px 8px', width: 260 }} />
          </Descriptions.Item>
          <Descriptions.Item label="阈值版本">
            <Radio.Group
              value={chosenThreshold ?? thresholdVersions.data?.items[0]?.id}
              onChange={(e) => setChosenThreshold(e.target.value)}
            >
              <Space direction="vertical">
                {(thresholdVersions.data?.items ?? []).map((v) => (
                  <Radio key={v.id} value={v.id}>
                    v{v.version}（{v.source}）· safe_coverage {fmtPercent(v.metrics?.safe_coverage)} · fwr {fmtPercent(v.metrics?.false_write_rate)}
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
          </Descriptions.Item>
        </Descriptions>
      </Modal>
    </div>
  )
}
