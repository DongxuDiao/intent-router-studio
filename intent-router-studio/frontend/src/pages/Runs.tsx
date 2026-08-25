/** 训练运行列表：轮询刷新进行中的 run。 */
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Progress, Table, Tag } from 'antd'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { PageHeader, RunStatusTag } from '../components/common'
import { useProject } from '../store/project'
import type { TrainingRun } from '../types'
import { fmtTime } from '../utils/format'

export default function Runs() {
  const { projectId } = useProject()
  const { data, isLoading } = useQuery({
    queryKey: ['runs', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: TrainingRun[] }>(`/projects/${projectId}/runs`),
    refetchInterval: (q) => ((q.state.data?.items ?? []).some((r) => !['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'].includes(r.status)) ? 3000 : 15000),
  })

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />

  return (
    <div>
      <PageHeader
        title="训练运行"
        subTitle="进行中的 Run 每 3 秒轮询；详情页使用 SSE 实时事件流"
        extra={<Link to="/training/new"><Button type="primary">新建训练</Button></Link>}
      />
      <Card loading={isLoading}>
        <Table
          rowKey="id"
          dataSource={data?.items ?? []}
          pagination={{ pageSize: 15 }}
          columns={[
            {
              title: 'Run',
              dataIndex: 'name',
              render: (n: string, r: TrainingRun) => (
                <Link to={`/runs/${r.id}`}>{n || r.id.slice(0, 16)}</Link>
              ),
            },
            { title: '状态', dataIndex: 'status', width: 110, render: (s: string) => <RunStatusTag status={s} /> },
            {
              title: '进度',
              dataIndex: 'progress',
              width: 180,
              render: (p: number, r: TrainingRun) => (
                <Progress
                  percent={p}
                  size="small"
                  status={r.status === 'FAILED' ? 'exception' : r.status === 'SUCCEEDED' ? 'success' : 'active'}
                  format={(pct) => `${pct}%${r.stage ? ` · ${r.stage}` : ''}`}
                />
              ),
            },
            {
              title: '数据集',
              dataIndex: 'dataset_id',
              width: 150,
              render: (v: string) => <Link to={`/datasets/${v}`}>{v.slice(0, 12)}…</Link>,
            },
            { title: 'epochs/iters', width: 120, render: (_, r: TrainingRun) => `${r.config.train.num_epochs}/${r.config.train.num_iterations}` },
            { title: '取消请求', dataIndex: 'cancel_requested', width: 90, render: (b: boolean) => (b ? <Tag color="warning">已请求</Tag> : '-') },
            { title: '开始', dataIndex: 'started_at', width: 130, render: fmtTime },
            { title: '结束', dataIndex: 'finished_at', width: 130, render: fmtTime },
          ]}
        />
      </Card>
    </div>
  )
}
