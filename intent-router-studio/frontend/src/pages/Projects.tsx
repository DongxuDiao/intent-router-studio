/** 项目列表：创建 / 选择当前项目。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Form, Input, List, Modal, Tag, Typography, message } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { PageHeader } from '../components/common'
import { useProject } from '../store/project'
import type { Project } from '../types'
import { fmtTime } from '../utils/format'
import { clearPlaygroundCache } from '../utils/playgroundCache'

interface ProjectDeletionImpact {
  project_id: string
  project_name: string
  is_empty: boolean
  can_delete: boolean
  counts: Record<string, number>
  active_runs: { id: string; name: string; status: string }[]
}

export default function Projects() {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState<{ project: Project; impact: ProjectDeletionImpact } | null>(null)
  const [confirmName, setConfirmName] = useState('')
  const [form] = Form.useForm()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { projectId, setProjectId } = useProject()

  const { data, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api<{ items: Project[] }>('/projects'),
  })

  const create = useMutation({
    mutationFn: (values: { name: string; description: string }) =>
      api<Project>('/projects', { method: 'POST', body: JSON.stringify(values) }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ['projects'] })
      setOpen(false)
      form.resetFields()
      message.success(`项目 ${p.name} 已创建（含默认 5 分类标签 schema）`)
      setProjectId(p.id)
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '创建失败'),
  })

  const inspectDelete = useMutation({
    mutationFn: (project: Project) =>
      api<ProjectDeletionImpact>(`/projects/${project.id}/deletion-impact`),
    onSuccess: (impact, project) => {
      setConfirmName('')
      setDeleting({ project, impact })
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '无法获取删除影响范围'),
  })

  const remove = useMutation({
    mutationFn: ({ project, confirmation }: { project: Project; confirmation?: string }) => {
      const init: RequestInit = confirmation
        ? { method: 'DELETE', body: JSON.stringify({ confirm_name: confirmation }) }
        : { method: 'DELETE' }
      return api<{ deleted: boolean; project_id: string; counts: Record<string, number> }>(
        `/projects/${project.id}`,
        init,
      )
    },
    onSuccess: (_result, { project }) => {
      clearPlaygroundCache(project.id)
      if (projectId === project.id) setProjectId(null)
      qc.invalidateQueries({ queryKey: ['projects'] })
      setDeleting(null)
      setConfirmName('')
      message.success(`项目 ${project.name} 及其关联内容已删除`)
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '删除失败'),
  })

  return (
    <div>
      <PageHeader
        title="项目"
        subTitle="所有数据集、训练与模型都归属于某个项目；切换当前项目后其余页面生效"
        extra={<Button type="primary" onClick={() => setOpen(true)}>新建项目</Button>}
      />
      <Card loading={isLoading}>
        {!data || data.items.length === 0 ? (
          <Empty description="还没有项目，先创建一个" />
        ) : (
          <List
            dataSource={data.items}
            renderItem={(p) => (
              <List.Item
                actions={[
                  <Button
                    key="use"
                    type={projectId === p.id ? 'primary' : 'default'}
                    size="small"
                    onClick={() => {
                      setProjectId(p.id)
                      message.success(`当前项目：${p.name}`)
                      navigate('/overview')
                    }}
                  >
                    {projectId === p.id ? '当前项目' : '设为当前'}
                  </Button>,
                  <Button
                    key="go"
                    size="small"
                    onClick={() => {
                      // V2 §4.5：进入前必须先切换当前项目，否则 Overview 仍读旧项目
                      setProjectId(p.id)
                      navigate('/overview')
                    }}
                  >
                    进入
                  </Button>,
                  <Button
                    key="delete"
                    danger
                    size="small"
                    loading={inspectDelete.isPending && inspectDelete.variables?.id === p.id}
                    onClick={() => inspectDelete.mutate(p)}
                  >
                    删除
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <span>
                      {p.name} {p.active_model_id && <Tag color="green">激活模型 {p.active_model_name ?? p.active_model_id}</Tag>}
                    </span>
                  }
                  description={p.description || '（无描述）'}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {p.dataset_count} 数据集 · {p.run_count} 训练 · 创建于 {fmtTime(p.created_at)} · {p.id}
                </Typography.Text>
              </List.Item>
            )}
          />
        )}
      </Card>

      <Modal
        title={deleting?.impact.is_empty ? '确认删除空项目' : '永久删除项目及全部数据'}
        open={Boolean(deleting)}
        okText="确认删除"
        okButtonProps={{
          danger: true,
          disabled: Boolean(deleting && (!deleting.impact.can_delete || (!deleting.impact.is_empty && confirmName !== deleting.project.name))),
        }}
        cancelText="取消"
        confirmLoading={remove.isPending}
        onCancel={() => { setDeleting(null); setConfirmName('') }}
        onOk={() => {
          if (!deleting) return
          remove.mutate({
            project: deleting.project,
            confirmation: deleting.impact.is_empty ? undefined : confirmName,
          })
        }}
      >
        {deleting && (
          <div>
            {!deleting.impact.can_delete ? (
              <Alert
                type="error"
                showIcon
                message="项目仍有排队中或运行中的训练"
                description="请先取消训练并等待任务结束，再删除项目。"
              />
            ) : deleting.impact.is_empty ? (
              <Alert type="warning" showIcon message="此操作不可撤销" description="将删除项目及其默认标签配置。" />
            ) : (
              <>
                <Alert
                  type="error"
                  showIcon
                  message="此操作将级联删除全部项目数据和本地制品，且不可恢复"
                />
                <Typography.Paragraph style={{ marginTop: 16, marginBottom: 8 }}>
                  影响范围：{Object.entries(deleting.impact.counts)
                    .filter(([, count]) => count > 0)
                    .map(([name, count]) => `${name} ${count}`)
                    .join('、')}
                </Typography.Paragraph>
                <Typography.Paragraph style={{ marginBottom: 8 }}>
                  请输入项目名 <Typography.Text strong>{deleting.project.name}</Typography.Text> 确认：
                </Typography.Paragraph>
                <Input
                  autoFocus
                  value={confirmName}
                  placeholder={deleting.project.name}
                  onChange={(event) => setConfirmName(event.target.value)}
                />
              </>
            )}
          </div>
        )}
      </Modal>

      <Modal
        title="新建项目"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={create.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(v) => create.mutate(v)}>
          <Form.Item name="name" label="项目名" rules={[{ required: true, min: 1, max: 200 }]}>
            <Input placeholder="e.g. 统一助手意图路由" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="业务背景、期望效果等" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
