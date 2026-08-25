/** 数据集详情：质量报告、样本浏览（筛选）、切分管理、冻结/草稿操作。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Input, Modal, Space, Table, Tabs, Tag, Typography, message } from 'antd'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { DatasetStatusTag, LabelTag, PageHeader } from '../components/common'
import { ChartBoundary, LazyLabelDistributionPie } from '../components/lazy-charts'
import type { DatasetVersion, QualityIssue, Sample, SamplesPage, SplitInfo } from '../types'
import { fmtPercent, fmtTime } from '../utils/format'

function IssueList({ issues, type }: { issues: QualityIssue[]; type: 'error' | 'warning' }) {
  if (issues.length === 0) return <Typography.Text type="secondary">无</Typography.Text>
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }}>
      {issues.slice(0, 30).map((i, idx) => (
        <li key={idx}>
          <Tag color={type === 'error' ? 'error' : 'warning'}>{i.code}</Tag>
          {i.message}
          {i.details && Object.keys(i.details).length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {' '}
              {JSON.stringify(i.details)}
            </Typography.Text>
          )}
        </li>
      ))}
      {issues.length > 30 && <li>… 共 {issues.length} 条</li>}
    </ul>
  )
}

export default function DatasetDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [labelFilter, setLabelFilter] = useState<string | null>(null)
  const [unlabeledOnly, setUnlabeledOnly] = useState(false)
  const [page, setPage] = useState(1)
  const [splitOpen, setSplitOpen] = useState(false)
  const [ratios, setRatios] = useState('{"train":0.7,"validation":0.15,"test":0.15}')
  const [seed, setSeed] = useState(42)

  const dataset = useQuery({
    queryKey: ['dataset', id],
    queryFn: () => api<DatasetVersion>(`/datasets/${id}`),
  })
  const samples = useQuery({
    queryKey: ['samples', id, q, labelFilter, unlabeledOnly, page],
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), page_size: '20' })
      if (q) params.set('q', q)
      if (labelFilter) params.set('label', labelFilter)
      if (unlabeledOnly) params.set('unlabeled_only', 'true')
      return api<SamplesPage>(`/datasets/${id}/samples?${params}`)
    },
  })
  const splits = useQuery({
    queryKey: ['splits', id],
    queryFn: () => api<{ items: SplitInfo[] }>(`/datasets/${id}/splits`),
  })

  const validate = useMutation({
    mutationFn: () => api(`/datasets/${id}/validate`, { method: 'POST' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['dataset', id] })
      message.success('校验完成，报告已刷新')
    },
  })

  const split = useMutation({
    mutationFn: () =>
      api<SplitInfo>(`/datasets/${id}/split`, {
        method: 'POST',
        body: JSON.stringify({ ratios: JSON.parse(ratios), seed }),
      }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ['splits', id] })
      setSplitOpen(false)
      const warnCount = s.stats.warnings?.length ?? 0
      message.success(`切分完成（risk_test ${s.stats.risk_test_rows ?? 0} 行${warnCount ? `，${warnCount} 条警告` : ''}）`)
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '切分失败'),
  })

  const d = dataset.data
  if (dataset.isLoading) return <Card loading />
  if (!d) return <Alert type="error" message="数据集不存在" />

  return (
    <div>
      <PageHeader
        title={d.name}
        subTitle={`${d.id} · ${d.origin}`}
        extra={
          <Space>
            <Button onClick={() => validate.mutate()} loading={validate.isPending}>重新校验</Button>
            <Button onClick={() => setSplitOpen(true)} disabled={(d.unlabeled_count ?? 0) > 0}>
              创建切分
            </Button>
            {d.status === 'DRAFT' && (
              <Button type="primary" onClick={() => navigate(`/datasets/${d.id}/label`)}>
                进入标注台
              </Button>
            )}
            <Link to="/training/new">
              <Button disabled={!!d.quality_report?.errors.length || (d.unlabeled_count ?? 0) > 0}>用此数据集训练</Button>
            </Link>
          </Space>
        }
      />
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card size="small">
          <Descriptions size="small" column={4}>
            <Descriptions.Item label="状态"><DatasetStatusTag status={d.status} /></Descriptions.Item>
            <Descriptions.Item label="版本">v{d.version}{d.parent_id && <Typography.Text type="secondary">（父 {d.parent_id.slice(0, 12)}…）</Typography.Text>}</Descriptions.Item>
            <Descriptions.Item label="样本">{d.sample_count}（未标注 {d.unlabeled_count}）</Descriptions.Item>
            <Descriptions.Item label="创建">{fmtTime(d.created_at)}</Descriptions.Item>
          </Descriptions>
          {(d.quality_report?.errors.length ?? 0) > 0 && (
            <Alert type="error" showIcon style={{ marginTop: 8 }} message={`存在 ${d.quality_report!.errors.length} 个数据错误：将阻断训练。可在标注台修正后提交新版本。`} />
          )}
        </Card>

        <Tabs
          items={[
            {
              key: 'quality',
              label: '质量报告',
              children: d.quality_report ? (
                <div style={{ display: 'flex', gap: 24 }}>
                  <div style={{ flex: 1 }}>
                    <Card size="small" title={`错误（${d.quality_report.errors.length}）— 阻断训练`} style={{ marginBottom: 12 }}>
                      <IssueList issues={d.quality_report.errors} type="error" />
                    </Card>
                    <Card size="small" title={`警告（${d.quality_report.warnings.length}）`}>
                      <IssueList issues={d.quality_report.warnings} type="warning" />
                    </Card>
                    <Card size="small" title="统计" style={{ marginTop: 12 }}>
                      <Descriptions size="small" column={2}>
                        <Descriptions.Item label="总行数">{d.quality_report.stats.rows}</Descriptions.Item>
                        <Descriptions.Item label="已标注">{d.quality_report.stats.labeled}</Descriptions.Item>
                        <Descriptions.Item label="唯一 hash">{d.quality_report.stats.unique_hashes}</Descriptions.Item>
                        <Descriptions.Item label="含 group_id">{d.quality_report.stats.has_group_id}</Descriptions.Item>
                        <Descriptions.Item label="难负例">{d.quality_report.stats.hard_negative}</Descriptions.Item>
                        <Descriptions.Item label="非写支撑集">{d.quality_report.stats.non_write_support}</Descriptions.Item>
                      </Descriptions>
                    </Card>
                  </div>
                  <div style={{ width: 320 }}>
                    <Card size="small" title="标签分布">
                      <ChartBoundary height={260}><LazyLabelDistributionPie distribution={d.quality_report.stats.label_distribution} /></ChartBoundary>
                    </Card>
                  </div>
                </div>
              ) : (
                <Typography.Text type="secondary">尚未生成质量报告，点击「重新校验」</Typography.Text>
              ),
            },
            {
              key: 'samples',
              label: '样本',
              children: (
                <Card size="small">
                  <Space style={{ marginBottom: 12 }} wrap>
                    <Input.Search
                      placeholder="搜索文本…"
                      allowClear
                      style={{ width: 260 }}
                      onSearch={(v) => {
                        setQ(v)
                        setPage(1)
                      }}
                    />
                    <select
                      value={labelFilter ?? ''}
                      onChange={(e) => {
                        setLabelFilter(e.target.value || null)
                        setPage(1)
                      }}
                      style={{ padding: '4px 8px' }}
                    >
                      <option value="">全部标签</option>
                      {Object.entries(d.label_distribution).map(([k, v]) => (
                        <option key={k} value={k}>{k}（{v}）</option>
                      ))}
                      <option value="__none__">未标注</option>
                    </select>
                    <Button
                      type={unlabeledOnly ? 'primary' : 'default'}
                      size="small"
                      onClick={() => {
                        setUnlabeledOnly((s) => !s)
                        setPage(1)
                      }}
                    >
                      只看未标注
                    </Button>
                  </Space>
                  <Table
                    size="small"
                    loading={samples.isLoading}
                    dataSource={samples.data?.samples ?? []}
                    rowKey="sample_id"
                    pagination={{
                      current: page,
                      pageSize: 20,
                      total: samples.data?.total ?? 0,
                      onChange: setPage,
                      showTotal: (t) => `共 ${t} 条`,
                    }}
                    columns={[
                      { title: '文本', dataIndex: 'text', ellipsis: true },
                      { title: '标签', dataIndex: 'label', width: 110, render: (l: string | null) => <LabelTag label={l} /> },
                      { title: 'context', dataIndex: 'context', ellipsis: true, width: 160, render: (c: string | null) => c ?? '-' },
                      { title: 'group', dataIndex: 'group_id', width: 90, ellipsis: true, render: (g: string | null) => g ?? '-' },
                      { title: '风险切片', dataIndex: 'risk_slice', width: 90, render: (r: string | null) => (r ? <Tag color="volcano">{r}</Tag> : '-') },
                      { title: '难负例', dataIndex: 'is_hard_negative', width: 70, render: (b: boolean) => (b ? <Tag color="red">是</Tag> : '-') },
                    ]}
                  />
                </Card>
              ),
            },
            {
              key: 'splits',
              label: `切分（${splits.data?.items.length ?? 0}）`,
              children: (
                <Table
                  size="small"
                  dataSource={splits.data?.items ?? []}
                  rowKey="id"
                  pagination={false}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 240 },
                    { title: '算法', dataIndex: 'algorithm', width: 140 },
                    { title: 'seed', dataIndex: 'seed', width: 70 },
                    {
                      title: '行数',
                      dataIndex: 'stats',
                      render: (s: SplitInfo['stats']) => JSON.stringify(s.rows),
                    },
                    { title: 'risk_test', width: 90, render: (_, r: SplitInfo) => r.stats.risk_test_rows ?? 0 },
                    {
                      title: '警告',
                      width: 80,
                      render: (_, r: SplitInfo) => (r.stats.warnings?.length ? <Tag color="warning">{r.stats.warnings.length}</Tag> : '-'),
                    },
                    { title: '创建', dataIndex: 'created_at', width: 140, render: fmtTime },
                  ]}
                />
              ),
            },
            {
              key: 'manifest',
              label: 'Manifest',
              children: d.manifest ? (
                <Card size="small">
                  <pre style={{ maxHeight: 400, overflow: 'auto', fontSize: 12 }}>{JSON.stringify(d.manifest, null, 2)}</pre>
                </Card>
              ) : (
                <Typography.Text type="secondary">无</Typography.Text>
              ),
            },
          ]}
        />
      </Space>

      <Modal
        title="创建数据切分（group-stratified，防泄漏）"
        open={splitOpen}
        onCancel={() => setSplitOpen(false)}
        onOk={() => split.mutate()}
        confirmLoading={split.isPending}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          相同 group_id 的样本保证同折；近似重复文本（normalized_hash）同折并产生警告；risk_slice / 难负例按比例进入 test 形成 risk_test。
        </Typography.Paragraph>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="比例（JSON）">
            <Input.TextArea rows={2} value={ratios} onChange={(e) => setRatios(e.target.value)} style={{ width: 300 }} />
          </Descriptions.Item>
          <Descriptions.Item label="seed">
            <Input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} style={{ width: 120 }} />
          </Descriptions.Item>
        </Descriptions>
        {(d.unlabeled_count ?? 0) > 0 && <Alert type="error" message={`存在 ${d.unlabeled_count} 个未标注样本，无法切分`} />}
      </Modal>
    </div>
  )
}
