/** 项目总览：标签 schema、数据集概要、最近运行、激活模型。 */
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Empty, Row, Space, Table, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { DatasetStatusTag, EffectTypeTag, PageHeader, RunStatusTag } from '../components/common'
import { useActiveSchema } from '../hooks/labelSchema'
import { useProject } from '../store/project'
import { EFFECT_CEILING_NAMES } from '../types'
import type { DatasetVersion, ModelVersion, TrainingRun } from '../types'
import { fmtTime } from '../utils/format'

export default function Overview() {
  const { projectId } = useProject()

  const schema = useActiveSchema(projectId)
  const datasets = useQuery({
    queryKey: ['datasets', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: DatasetVersion[] }>(`/projects/${projectId}/datasets`),
  })
  const runs = useQuery({
    queryKey: ['runs', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: TrainingRun[] }>(`/projects/${projectId}/runs`),
    refetchInterval: 5000,
  })
  const models = useQuery({
    queryKey: ['models', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: ModelVersion[] }>(`/projects/${projectId}/models`),
  })

  if (!projectId) {
    return <Alert type="info" showIcon message="请先在「项目」页选择或创建一个项目" />
  }

  const activeModel = models.data?.items.find((m) => m.status === 'ACTIVE')
  const recentRuns = (runs.data?.items ?? []).slice(0, 8)

  return (
    <div>
      <PageHeader
        title="项目总览"
        subTitle={projectId}
        extra={
          <Space>
            <Link to="/datasets/upload"><Button>导入数据</Button></Link>
            <Link to="/training/new"><Button type="primary">发起训练</Button></Link>
          </Space>
        }
      />
      <Row gutter={[16, 16]}>
        <Col span={14}>
          <Card title="标签 Schema" size="small" extra={<Link to="/labels">管理 Schema</Link>}>
            <Table
              size="small"
              pagination={false}
              loading={schema.isLoading}
              dataSource={schema.data?.document.labels ?? []}
              rowKey="key"
              columns={[
                { title: '标签 Key', dataIndex: 'key', width: 140 },
                { title: '名称', dataIndex: 'name', width: 110 },
                { title: '定义', dataIndex: 'description' },
                {
                  title: '系统效果',
                  dataIndex: 'effect_type',
                  width: 180,
                  render: (effectType: string) => <EffectTypeTag effect={effectType} />,
                },
                {
                  title: '效果上限',
                  width: 140,
                  render: (_, r: { effect_type: string }) => EFFECT_CEILING_NAMES[
                    r.effect_type === 'write_action' ? 'external_write_candidate' : r.effect_type === 'read_only' ? 'read_only' : 'none'
                  ],
                },
              ]}
            />
          </Card>
          <Card title={`数据集（${datasets.data?.items.length ?? 0}）`} size="small" style={{ marginTop: 16 }}>
            <Table
              size="small"
              pagination={{ pageSize: 5 }}
              loading={datasets.isLoading}
              dataSource={datasets.data?.items ?? []}
              rowKey="id"
              columns={[
                {
                  title: '名称',
                  dataIndex: 'name',
                  render: (n: string, r: DatasetVersion) => <Link to={`/datasets/${r.id}`}>{n}</Link>,
                },
                { title: '状态', dataIndex: 'status', width: 110, render: (s: string) => <DatasetStatusTag status={s} /> },
                { title: '样本', dataIndex: 'sample_count', width: 80 },
                { title: '版本', dataIndex: 'version', width: 70, render: (v: number) => `v${v}` },
                { title: '创建', dataIndex: 'created_at', width: 130, render: fmtTime },
              ]}
            />
          </Card>
        </Col>
        <Col span={10}>
          <Card title="激活模型" size="small">
            {activeModel ? (
              <div>
                <Typography.Title level={5} style={{ margin: 0 }}>
                  {activeModel.name}
                </Typography.Title>
                <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                  {activeModel.id} · 来自 run {activeModel.run_id} · 激活于 {fmtTime(activeModel.activated_at)}
                </Typography.Paragraph>
                {activeModel.metrics_summary && (
                  <Space size="large">
                    <span>macro_f1: {(activeModel.metrics_summary.macro_f1 ?? 0).toFixed(3)}</span>
                    <span>safe_coverage: {((activeModel.metrics_summary.safe_coverage ?? 0) * 100).toFixed(1)}%</span>
                  </Space>
                )}
              </div>
            ) : (
              <Empty description="尚无激活模型——完成一次训练并注册激活后，Playground 与 /predict 才可用" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
          <Card title="最近训练" size="small" style={{ marginTop: 16 }}>
            {recentRuns.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无训练运行" />
            ) : (
              <Table
                size="small"
                pagination={false}
                dataSource={recentRuns}
                rowKey="id"
                columns={[
                  {
                    title: 'Run',
                    dataIndex: 'name',
                    render: (n: string, r: TrainingRun) => <Link to={`/runs/${r.id}`}>{n || r.id}</Link>,
                  },
                  { title: '状态', dataIndex: 'status', width: 100, render: (s: string) => <RunStatusTag status={s} /> },
                  { title: '进度', dataIndex: 'progress', width: 70, render: (p: number) => `${p}%` },
                  { title: '创建', dataIndex: 'created_at', width: 130, render: fmtTime },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
