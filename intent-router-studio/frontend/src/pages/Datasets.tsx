/** 数据集版本列表。 */
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Table } from 'antd'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { DatasetStatusTag, LabelTag, PageHeader } from '../components/common'
import { useProject } from '../store/project'
import type { DatasetVersion } from '../types'
import { fmtTime } from '../utils/format'

export default function Datasets() {
  const { projectId } = useProject()
  const { data, isLoading } = useQuery({
    queryKey: ['datasets', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: DatasetVersion[] }>(`/projects/${projectId}/datasets`),
  })

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />

  return (
    <div>
      <PageHeader
        title="数据集版本"
        subTitle="FROZEN 版本不可变；草稿（DRAFT）可标注修正后提交为新版本"
        extra={<Link to="/datasets/upload"><Button type="primary">导入新数据</Button></Link>}
      />
      <Card loading={isLoading}>
        <Table
          rowKey="id"
          dataSource={data?.items ?? []}
          pagination={{ pageSize: 10 }}
          columns={[
            {
              title: '名称',
              dataIndex: 'name',
              render: (n: string, r: DatasetVersion) => <Link to={`/datasets/${r.id}`}>{n}</Link>,
            },
            { title: '版本', dataIndex: 'version', width: 70, render: (v: number) => `v${v}` },
            { title: '来源', dataIndex: 'origin', width: 110 },
            { title: '状态', dataIndex: 'status', width: 110, render: (s: string) => <DatasetStatusTag status={s} /> },
            { title: '样本', dataIndex: 'sample_count', width: 80 },
            {
              title: '标签分布',
              dataIndex: 'label_distribution',
              render: (dist: Record<string, number>) =>
                Object.entries(dist)
                  .map(([k, v]) => (
                    <span key={k} style={{ marginRight: 8 }}>
                      <LabelTag label={k} /> {v}
                    </span>
                  )),
            },
            {
              title: '错误',
              width: 70,
              render: (_, r: DatasetVersion) => {
                const n = r.quality_report?.errors.length ?? 0
                return n > 0 ? <span style={{ color: '#cf1322' }}>{n}</span> : '0'
              },
            },
            { title: '创建', dataIndex: 'created_at', width: 140, render: fmtTime },
          ]}
        />
      </Card>
    </div>
  )
}
