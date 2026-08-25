/** 模型注册表：激活（原子切换 + 冒烟校验）、回滚、归档、manifest 查看。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Drawer, Modal, Space, Table, Tag, Typography, message } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { MetricCard, ModelStatusTag, PageHeader } from '../components/common'
import { useProject } from '../store/project'
import type { ModelVersion } from '../types'
import { fmtPercent, fmtTime } from '../utils/format'

export default function Models() {
  const { projectId } = useProject()
  const qc = useQueryClient()
  // V2 §4.5：激活/回滚共用确认弹窗，按目标状态分派——ARCHIVED 模型只能走
  // /rollback（后端 /activate 拒绝归档模型），普通候选才走 /activate。
  const [confirmTarget, setConfirmTarget] = useState<ModelVersion | null>(null)
  const [manifestFor, setManifestFor] = useState<ModelVersion | null>(null)
  const isRollback = confirmTarget?.status === 'ARCHIVED'

  const models = useQuery({
    queryKey: ['models', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: ModelVersion[] }>(`/projects/${projectId}/models`),
  })

  const activate = useMutation({
    mutationFn: (id: string) => api<ModelVersion>(`/models/${id}/activate`, { method: 'POST' }),
    onSuccess: (m) => {
      setConfirmTarget(null)
      message.success(`已激活 ${m.name}（旧模型自动归档，切换为原子操作）`)
      qc.invalidateQueries({ queryKey: ['models', projectId] })
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    },
  })
  const rollback = useMutation({
    mutationFn: (id: string) => api<ModelVersion>(`/models/${id}/rollback`, { method: 'POST' }),
    onSuccess: (m) => {
      setConfirmTarget(null)
      message.success(`已回滚并重新激活 ${m.name}`)
      qc.invalidateQueries({ queryKey: ['models', projectId] })
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
      else message.error(e instanceof Error ? e.message : '回滚失败')
    },
  })
  const archive = useMutation({
    mutationFn: (id: string) => api(`/models/${id}/archive`, { method: 'POST' }),
    onSuccess: () => {
      message.success('已归档')
      qc.invalidateQueries({ queryKey: ['models', projectId] })
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
      else message.error(e instanceof Error ? e.message : '归档失败')
    },
  })

  const manifest = useQuery({
    queryKey: ['model-manifest', manifestFor?.id],
    enabled: !!manifestFor,
    queryFn: () => api<Record<string, unknown>>(`/models/${manifestFor!.id}/manifest`),
  })

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />

  const activeModel = models.data?.items.find((m) => m.status === 'ACTIVE')
  const oldActive = confirmTarget && activeModel && confirmTarget.id !== activeModel.id ? activeModel : null
  // 最近一个归档模型 = 回滚目标
  const lastArchived = models.data?.items.find((m) => m.status === 'ARCHIVED')

  return (
    <div>
      <PageHeader
        title="模型注册表"
        subTitle="制品哈希可验证；激活 = 验证 manifest → 冒烟预测 → 原子切换项目指向"
        extra={
          lastArchived ? (
            <Button onClick={() => setConfirmTarget(lastArchived)} loading={rollback.isPending}>
              回滚到 {lastArchived.name}
            </Button>
          ) : undefined
        }
      />
      {activeModel && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space size="large" align="center">
            <Tag color="success">当前激活</Tag>
            <Typography.Text strong>{activeModel.name}</Typography.Text>
            <MetricCard title="macro_f1" value={activeModel.metrics_summary?.macro_f1 ?? null} />
            <MetricCard title="false_write_rate" value={activeModel.metrics_summary ? fmtPercent(activeModel.metrics_summary.false_write_rate) : '-'} />
            <MetricCard title="safe_coverage" value={activeModel.metrics_summary ? fmtPercent(activeModel.metrics_summary.safe_coverage) : '-'} />
          </Space>
        </Card>
      )}
      <Card loading={models.isLoading}>
        <Table
          rowKey="id"
          dataSource={models.data?.items ?? []}
          pagination={{ pageSize: 10 }}
          columns={[
            {
              title: '模型',
              dataIndex: 'name',
              render: (n: string, m: ModelVersion) => (
                <span>
                  {n} <Typography.Text type="secondary" style={{ fontSize: 11 }}>{m.id}</Typography.Text>
                </span>
              ),
            },
            { title: '状态', dataIndex: 'status', width: 90, render: (s: string) => <ModelStatusTag status={s} /> },
            {
              title: '来源 Run',
              dataIndex: 'run_id',
              width: 150,
              render: (v: string) => <Link to={`/runs/${v}`}>{v.slice(0, 14)}…</Link>,
            },
            { title: 'macro_f1', width: 100, render: (_, m: ModelVersion) => m.metrics_summary?.macro_f1?.toFixed(3) ?? '-' },
            { title: 'safe_cov', width: 90, render: (_, m: ModelVersion) => fmtPercent(m.metrics_summary?.safe_coverage) },
            { title: 'fwr', width: 90, render: (_, m: ModelVersion) => fmtPercent(m.metrics_summary?.false_write_rate) },
            { title: '激活时间', dataIndex: 'activated_at', width: 140, render: fmtTime },
            {
              title: '操作',
              width: 220,
              render: (_, m: ModelVersion) => (
                <Space>
                  {m.status !== 'ACTIVE' && m.status !== 'ARCHIVED' && (
                    <Button size="small" type="primary" onClick={() => setConfirmTarget(m)}>激活</Button>
                  )}
                  {m.status === 'ARCHIVED' && (
                    <Button size="small" onClick={() => setConfirmTarget(m)}>回滚激活</Button>
                  )}
                  <Button size="small" onClick={() => setManifestFor(m)}>Manifest</Button>
                  {m.status !== 'ACTIVE' && m.status !== 'ARCHIVED' && (
                    <Button size="small" onClick={() => archive.mutate(m.id)}>归档</Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={isRollback ? '确认回滚' : '确认激活'}
        open={!!confirmTarget}
        onCancel={() => setConfirmTarget(null)}
        onOk={() => {
          if (!confirmTarget) return
          if (isRollback) rollback.mutate(confirmTarget.id)
          else activate.mutate(confirmTarget.id)
        }}
        okText={isRollback ? '回滚' : '激活'}
        cancelText="取消"
        confirmLoading={activate.isPending || rollback.isPending}
      >
        {confirmTarget && (
          <div>
            <Typography.Paragraph>
              {isRollback ? '即将回滚并重新激活 ' : '即将激活 '}
              <Typography.Text strong>{confirmTarget.name}</Typography.Text>
            </Typography.Paragraph>
            {oldActive ? (
              <Alert
                type="warning"
                showIcon
                message="新旧对比"
                description={
                  <table style={{ width: '100%', fontSize: 12 }}>
                    <thead>
                      <tr><th /><th>旧（当前激活）</th><th>新（待激活）</th></tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td>macro_f1</td>
                        <td>{oldActive.metrics_summary?.macro_f1?.toFixed(3) ?? '-'}</td>
                        <td><b>{confirmTarget.metrics_summary?.macro_f1?.toFixed(3) ?? '-'}</b></td>
                      </tr>
                      <tr>
                        <td>false_write_rate</td>
                        <td>{fmtPercent(oldActive.metrics_summary?.false_write_rate)}</td>
                        <td><b>{fmtPercent(confirmTarget.metrics_summary?.false_write_rate)}</b></td>
                      </tr>
                      <tr>
                        <td>safe_coverage</td>
                        <td>{fmtPercent(oldActive.metrics_summary?.safe_coverage)}</td>
                        <td><b>{fmtPercent(confirmTarget.metrics_summary?.safe_coverage)}</b></td>
                      </tr>
                    </tbody>
                  </table>
                }
              />
            ) : (
              <Alert type="info" message="该项目尚无激活模型，本次为首次激活" />
            )}
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
              {isRollback
                ? '回滚流程：verify manifest（哈希校验）→ 临时加载 + 冒烟预测 → 原子切换项目指向（当前激活模型自动归档）。'
                : '激活流程：verify manifest（哈希校验）→ 临时加载 + 冒烟预测 → 事务内归档旧模型并切换 → 运行时热替换。'}
            </Typography.Paragraph>
          </div>
        )}
      </Modal>

      <Drawer title={`Manifest · ${manifestFor?.name ?? ''}`} open={!!manifestFor} onClose={() => setManifestFor(null)} width={620}>
        <pre style={{ fontSize: 11, maxHeight: '70vh', overflow: 'auto' }}>{JSON.stringify(manifest.data, null, 2)}</pre>
      </Drawer>
    </div>
  )
}
