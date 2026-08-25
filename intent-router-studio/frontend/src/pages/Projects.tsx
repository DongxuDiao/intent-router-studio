/** 项目列表：创建 / 选择当前项目。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Form, Input, List, Modal, Tag, Typography, message } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { PageHeader } from '../components/common'
import { useProject } from '../store/project'
import type { Project } from '../types'
import { fmtTime } from '../utils/format'

export default function Projects() {
  const [open, setOpen] = useState(false)
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
