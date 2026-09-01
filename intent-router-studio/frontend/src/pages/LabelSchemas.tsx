/** 意图标签管理（自定义意图标签 §7.1）：生效版本卡片 + 版本历史 + 草稿编辑器 +
 * 影响分析抽屉 + 发布确认弹窗。effect_type 是固定枚举且决定全部安全语义；
 * 修改 effect type 或删除被引用标签属破坏性变更，发布需二次确认。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { EffectTypeTag, PageHeader } from '../components/common'
import { useProject } from '../store/project'
import {
  EFFECT_TYPES,
  EFFECT_TYPE_NAMES,
  type LabelDefinitionInput,
  type LabelSchemaDetail,
  type LabelSchemaInfo,
  type SchemaImpact,
} from '../types'
import { fmtTime } from '../utils/format'

interface DraftRow extends LabelDefinitionInput {
  rowKey: string // 前端行标识，与 key 解耦，便于新增行编辑
}

const STATUS_NAMES: Record<string, string> = { DRAFT: '草稿', ACTIVE: '生效中', SUPERSEDED: '已归档' }

interface LabelFormValues {
  key: string
  name: string
  effect_type: string
  description?: string
}

export default function LabelSchemas() {
  const { projectId } = useProject()
  const qc = useQueryClient()
  const [rows, setRows] = useState<DraftRow[]>([])
  const [summary, setSummary] = useState('')
  const [impact, setImpact] = useState<SchemaImpact | null>(null)
  const [impactOpen, setImpactOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [publishImpact, setPublishImpact] = useState<SchemaImpact | null>(null)
  const [confirmBreaking, setConfirmBreaking] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [editIndex, setEditIndex] = useState<number | null>(null)

  const schemas = useQuery({
    queryKey: ['label-schemas', projectId],
    enabled: !!projectId,
    queryFn: () => api<{ items: LabelSchemaInfo[] }>(`/projects/${projectId}/label-schemas`),
  })
  const items = schemas.data?.items ?? []
  const active = items.find((s) => s.status === 'ACTIVE') ?? null
  const draftInfo = items.find((s) => s.status === 'DRAFT') ?? null

  // 草稿详情（含 document.labels）——存在草稿时才拉取
  const draftDetail = useQuery({
    queryKey: ['label-schema-draft', projectId, draftInfo?.id],
    enabled: !!projectId && !!draftInfo,
    queryFn: () => api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/${draftInfo!.id}`),
  })
  const draft = draftDetail.data ?? null
  useEffect(() => {
    const detail = draftDetail.data
    if (!detail) return
    setSummary(detail.change_summary || '')
    setRows(
      detail.document.labels.map((d, i) => ({
        ...d,
        positive_examples: d.positive_examples ?? [],
        negative_examples: d.negative_examples ?? [],
        rowKey: `${d.key}-${i}`,
      })),
    )
  }, [draftDetail.data])

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['label-schemas', projectId] })
    qc.invalidateQueries({ queryKey: ['label-schema-draft', projectId] })
  }

  const showApiError = (e: unknown) => {
    if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    else message.error(e instanceof Error ? e.message : '操作失败')
  }

  const createDraft = useMutation({
    mutationFn: (changeSummary: string) =>
      api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/drafts`, {
        method: 'POST',
        body: JSON.stringify({ change_summary: changeSummary }),
      }),
    onSuccess: () => {
      message.success('已基于当前生效版本创建草稿')
      setCreateOpen(false)
      refresh()
    },
    onError: showApiError,
  })

  /** 保存草稿：表格行 → labels 数组（key 顺序即分类头候选顺序） */
  const saveDraft = useMutation({
    mutationFn: () =>
      api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/${draftInfo!.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          expected_hash: draft!.hash,
          labels: rows.map(({ rowKey: _k, ...rest }) => rest),
          change_summary: summary,
        }),
      }),
    onSuccess: () => {
      message.success('草稿已保存（发布前不影响线上行为）')
      refresh()
    },
    onError: showApiError,
  })

  const deleteDraft = useMutation({
    mutationFn: () => api(`/projects/${projectId}/label-schemas/${draftInfo!.id}`, { method: 'DELETE' }),
    onSuccess: () => {
      message.success('草稿已删除')
      refresh()
    },
    onError: showApiError,
  })

  const fetchImpact = async () => {
    try {
      return await api<SchemaImpact>(`/projects/${projectId}/label-schemas/${draftInfo!.id}/impact`, { method: 'POST' })
    } catch (e) {
      showApiError(e)
      return null
    }
  }

  const publish = useMutation({
    mutationFn: (payload: { expected_hash: string; confirm_breaking_changes: boolean }) =>
      api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/${draftInfo!.id}/publish`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: (detail) => {
      message.success(`已发布 v${detail.version} 并生效；此后新导入的数据集将绑定该版本`)
      setPublishOpen(false)
      refresh()
    },
    onError: (e) => {
      // 未勾选破坏性确认：弹窗保持打开，提示后勾选重试
      if (e instanceof ApiError && e.code === 'LABEL_SCHEMA_CONFLICT') {
        const embedded = (e.details?.impact as SchemaImpact | undefined) ?? publishImpact
        if (embedded?.breaking) {
          setPublishImpact(embedded)
          setConfirmBreaking(false)
          message.warning(e.message)
          return
        }
      }
      showApiError(e)
    },
  })

  // ---- 草稿行操作 ----
  const applyRow = (index: number, patch: Partial<DraftRow>) => {
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)))
  }
  const moveRow = (index: number, delta: -1 | 1) => {
    setRows((prev) => {
      const next = [...prev]
      const target = index + delta
      ;[next[index], next[target]] = [next[target], next[index]]
      return next.map((r, i) => ({ ...r, order: i * 10 }))
    })
  }
  const toggleDeprecated = (index: number) => {
    const row = rows[index]
    if (row.status === 'deprecated') {
      applyRow(index, { status: 'active' })
      return
    }
    Modal.confirm({
      title: `停用标签「${row.name}」？`,
      content: '停用后不再出现在新数据集的标注选项中；已有数据与模型制品不受影响。',
      okText: '停用',
      onOk: () => applyRow(index, { status: 'deprecated' }),
    })
  }

  // ---- 发布流程：先把表格当前内容存入草稿，再基于保存后内容取影响分析、按最新 hash 发布 ----
  const [publishHash, setPublishHash] = useState<string | null>(null)
  const startPublish = async () => {
    if (saveDraft.isPending) return
    const saved = await saveDraft.mutateAsync().catch(() => null)
    if (!saved) return
    const report = await fetchImpact()
    if (!report) return
    setPublishHash(saved.hash)
    setPublishImpact(report)
    setConfirmBreaking(false)
    setPublishOpen(true)
  }
  const confirmPublish = () => {
    if (!publishHash) return
    publish.mutate({ expected_hash: publishHash, confirm_breaking_changes: confirmBreaking })
  }

  const versionColumns = [
    { title: '版本', dataIndex: 'version', key: 'version', render: (v: number) => `v${v}` },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: string) => (
        <Tag color={s === 'ACTIVE' ? 'success' : s === 'DRAFT' ? 'processing' : 'default'}>{STATUS_NAMES[s] ?? s}</Tag>
      ),
    },
    { title: '启用标签', dataIndex: 'active_label_count', key: 'active' },
    { title: '停用标签', dataIndex: 'deprecated_label_count', key: 'deprecated' },
    { title: '变更说明', dataIndex: 'change_summary', key: 'summary', ellipsis: true },
    {
      title: '引用',
      key: 'refs',
      render: (_: unknown, r: LabelSchemaInfo) =>
        `数据集 ${r.references?.datasets ?? 0} · Run ${r.references?.runs ?? 0} · 模型 ${r.references?.models ?? 0}`,
    },
    {
      title: '发布时间',
      dataIndex: 'published_at',
      key: 'published_at',
      render: (v: string | null) => (v ? fmtTime(v) : '-'),
    },
  ]

  const labelColumns = [
    {
      title: 'key（分类头顺序）',
      dataIndex: 'key',
      key: 'key',
      width: 180,
      render: (v: string, row: DraftRow) =>
        row.status === 'deprecated' ? <Typography.Text delete>{v}</Typography.Text> : <Typography.Text code>{v}</Typography.Text>,
    },
    {
      title: '业务名称',
      dataIndex: 'name',
      key: 'name',
      render: (v: string, row: DraftRow) =>
        row.status === 'deprecated' ? (
          <Space size={4}>
            <Typography.Text type="secondary">{v}</Typography.Text>
            <Tag>已停用</Tag>
          </Space>
        ) : (
          v
        ),
    },
      {
      title: '系统效果类型',
      dataIndex: 'effect_type',
      key: 'effect_type',
      width: 210,
      render: (v: string) => <EffectTypeTag effect={v} />,
    },
    { title: '排序', dataIndex: 'order', key: 'order', width: 70 },
    {
      title: '操作',
      key: 'ops',
      width: 230,
      render: (_: unknown, row: DraftRow, index: number) => {
        const deprecated = row.status === 'deprecated'
        return (
          <Space size={0}>
            <Button size="small" type="text" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveRow(index, -1)} />
            <Button
              size="small"
              type="text"
              icon={<ArrowDownOutlined />}
              disabled={index === rows.length - 1}
              onClick={() => moveRow(index, 1)}
            />
            <Button size="small" type="text" onClick={() => setEditIndex(index)}>
              编辑
            </Button>
            <Button size="small" type="text" danger={!deprecated} onClick={() => toggleDeprecated(index)}>
              {deprecated ? '恢复' : '停用'}
            </Button>
          </Space>
        )
      },
    },
  ]

  return (
    <div>
      <PageHeader
        title="意图标签"
        subTitle="业务意图标签 → 系统效果类型；已发布版本不可变，历史数据集与模型始终使用训练时绑定的版本"
        extra={
          !draftInfo ? (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)} disabled={!projectId}>
              新建变更草稿
            </Button>
          ) : undefined
        }
      />

      {active ? (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space size="large" wrap>
            <Typography.Text strong>当前生效 v{active.version}</Typography.Text>
            <Typography.Text type="secondary">发布于 {active.published_at ? fmtTime(active.published_at) : '-'}</Typography.Text>
            <Typography.Text>启用标签 {active.active_label_count} 个</Typography.Text>
            <Typography.Text type="secondary">
              引用：数据集 {active.references?.datasets ?? 0} · Run {active.references?.runs ?? 0} · 模型 {active.references?.models ?? 0}
            </Typography.Text>
            <Typography.Text type="secondary" code>
              {active.hash.slice(0, 12)}
            </Typography.Text>
          </Space>
        </Card>
      ) : (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="项目尚无生效 Schema"
          description="新项目通常在创建时自动生成兼容五分类 Schema；请刷新或联系管理员。"
        />
      )}

      {draftInfo && (
        <Card
          size="small"
          title={
            <Space>
              <span>编辑草稿 v{draftInfo.version}</span>
              <Tag color="processing">未发布 · 不影响线上</Tag>
            </Space>
          }
          extra={
            <Space>
              <Input
                style={{ width: 320 }}
                placeholder="变更说明（如：新增工单类意图）"
                value={summary}
                onChange={(e) => setSummary(e.target.value)}
              />
              <Button
                onClick={async () => {
                  const report = await fetchImpact()
                  if (report) {
                    setImpact(report)
                    setImpactOpen(true)
                  }
                }}
              >
                影响分析
              </Button>
              <Popconfirm title="删除草稿？" description="草稿未发布，删除不影响任何数据" onConfirm={() => deleteDraft.mutate()}>
                <Button danger>删除草稿</Button>
              </Popconfirm>
              <Button type="primary" icon={<ThunderboltOutlined />} onClick={() => void startPublish()}>
                发布
              </Button>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="效果类型决定安全语义"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                <li>key：小写字母开头，2~64 位，仅 a-z0-9_，发布后作为历史版本不可改；</li>
                <li>系统效果类型决定阈值、效果上限与下游门禁——write_action 只授予「写候选资格」，仍需 Skill 匹配 + 用户确认；</li>
                <li>修改任何标签的效果类型、或移除被引用标签，均属破坏性变更，发布时需二次确认。</li>
              </ul>
            }
          />
          <Table
            size="small"
            rowKey="rowKey"
            columns={labelColumns}
            dataSource={rows}
            pagination={false}
            loading={draftDetail.isLoading}
            footer={() => (
              <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
                添加标签
              </Button>
            )}
          />
          <Space style={{ marginTop: 12 }}>
            <Button loading={saveDraft.isPending} onClick={() => saveDraft.mutate()}>
              保存草稿
            </Button>
            <Typography.Text type="secondary">表格改动需「保存草稿」后才进入草稿；「发布」会自动保存当前表格再走确认。</Typography.Text>
          </Space>
        </Card>
      )}

      <Card size="small" title="版本历史">
        <Table size="small" rowKey="id" columns={versionColumns} dataSource={items} pagination={false} loading={schemas.isLoading} />
      </Card>

      <CreateDraftModal open={createOpen} onCancel={() => setCreateOpen(false)} onOk={(s) => createDraft.mutate(s)} loading={createDraft.isPending} />

      <Modal title="添加标签" open={addOpen} onCancel={() => setAddOpen(false)} footer={null} destroyOnClose>
        <LabelEditForm
          onCancel={() => setAddOpen(false)}
          onSubmit={(values) => {
            setRows((prev) => [
              ...prev,
              { ...values, status: 'active', order: prev.length * 10, rowKey: `new-${Date.now()}` },
            ])
            setAddOpen(false)
          }}
        />
      </Modal>

      <Modal title={editIndex !== null ? `编辑标签 ${rows[editIndex]?.key ?? ''}` : '编辑标签'} open={editIndex !== null} onCancel={() => setEditIndex(null)} footer={null} destroyOnClose>
        {editIndex !== null && (
          <LabelEditForm
            initial={rows[editIndex]}
            lockKey
            onCancel={() => setEditIndex(null)}
            onSubmit={(values) => {
              applyRow(editIndex, values)
              setEditIndex(null)
            }}
          />
        )}
      </Modal>

      <Drawer title={`影响分析 · 草稿 v${draftInfo?.version ?? '-'}`} width={520} open={impactOpen} onClose={() => setImpactOpen(false)}>
        {impact ? <ImpactReport report={impact} /> : null}
      </Drawer>

      <Modal
        title={`发布草稿 v${draftInfo?.version ?? '-'}`}
        open={publishOpen}
        onCancel={() => setPublishOpen(false)}
        okText={publishImpact?.breaking ? '确认发布（破坏性变更）' : '发布'}
        okButtonProps={{ danger: publishImpact?.breaking, disabled: !!publishImpact?.breaking && !confirmBreaking }}
        confirmLoading={publish.isPending}
        onOk={confirmPublish}
      >
        {publishImpact ? <ImpactReport report={publishImpact} /> : null}
        {publishImpact?.breaking && (
          <Checkbox style={{ marginTop: 12 }} checked={confirmBreaking} onChange={(e) => setConfirmBreaking(e.target.checked)}>
            我已知晓上述破坏性变更（效果类型变化 / 移除被引用标签），确认发布
          </Checkbox>
        )}
      </Modal>
    </div>
  )
}

/** 影响分析报告（§7.1 抽屉与发布弹窗共用）。 */
function ImpactReport({ report }: { report: SchemaImpact }) {
  return (
    <div>
      {report.breaking ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="破坏性变更"
          description="包含安全语义变化或移除被引用标签，发布需勾选二次确认。历史数据集与模型仍按各自绑定版本运行，不受本次发布影响。"
        />
      ) : (
        <Alert type="success" showIcon style={{ marginBottom: 12 }} message="非破坏性变更" description="仅新增标签或停用无引用标签，可直接发布。" />
      )}
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Typography.Text>新增标签：{report.added.length ? report.added.join('、') : '无'}</Typography.Text>
        <Typography.Text>
          移除标签：{report.removed.length ? <span style={{ color: '#cf1322' }}>{report.removed.join('、')}</span> : '无'}
        </Typography.Text>
        <Typography.Text>停用标签：{report.deprecated.length ? report.deprecated.join('、') : '无'}</Typography.Text>
        <div>
          效果类型变化：
          {report.effect_type_changed.length ? (
            <ul style={{ margin: '4px 0 0', paddingLeft: 18, color: '#cf1322' }}>
              {report.effect_type_changed.map((c) => (
                <li key={c.key}>
                  {c.key}：<EffectTypeTag effect={c.from} /> → <EffectTypeTag effect={c.to} />
                </li>
              ))}
            </ul>
          ) : (
            '无'
          )}
        </div>
        <Typography.Text type="secondary">
          受影响：数据集 {report.affected_datasets} · Run {report.affected_runs} · 模型 {report.affected_models}
          {report.requires_retraining ? '；标签集合变化，发布后需用新数据重新训练' : ''}
        </Typography.Text>
      </Space>
    </div>
  )
}

/** 新建草稿弹窗（输入变更说明）。 */
function CreateDraftModal({
  open,
  onCancel,
  onOk,
  loading,
}: {
  open: boolean
  onCancel: () => void
  onOk: (summary: string) => void
  loading: boolean
}) {
  const [form] = Form.useForm<{ change_summary: string }>()
  useEffect(() => {
    if (open) form.setFieldsValue({ change_summary: '' })
  }, [open, form])
  return (
    <Modal title="新建变更草稿" open={open} onCancel={onCancel} confirmLoading={loading} okText="创建" onOk={() => form.submit()} destroyOnClose>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        草稿基于当前生效版本创建；发布前不影响导入、训练与线上推理。每个项目同时只能有一个草稿。
      </Typography.Paragraph>
      <Form form={form} layout="vertical" onFinish={(v) => onOk(v.change_summary ?? '')}>
        <Form.Item name="change_summary" label="变更说明" rules={[{ required: true, message: '请填写变更说明' }]}>
          <Input placeholder="如：新增工单类意图" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

/** 标签新增/编辑表单（Modal 内容）。 */
function LabelEditForm({
  initial,
  lockKey,
  onSubmit,
  onCancel,
}: {
  initial?: LabelDefinitionInput | null
  lockKey?: boolean
  onSubmit: (values: LabelFormValues) => void
  onCancel: () => void
}) {
  const [form] = Form.useForm()
  useEffect(() => {
    form.setFieldsValue({
      key: initial?.key ?? '',
      name: initial?.name ?? '',
      effect_type: initial?.effect_type ?? 'information',
      description: initial?.description ?? '',
    })
  }, [initial, form])
  return (
    <Form form={form} layout="vertical" onFinish={onSubmit} preserve={false}>
      <Form.Item
        name="key"
        label="标签 key"
        rules={[
          { required: true, message: '请输入 key' },
          { pattern: /^[a-z][a-z0-9_]{1,63}$/, message: '小写字母开头，2~64 位，仅 a-z0-9_' },
        ]}
      >
        <Input placeholder="如 create_task" disabled={lockKey} />
      </Form.Item>
      <Form.Item name="name" label="业务名称" rules={[{ required: true, message: '请输入名称' }, { max: 100, message: '≤100 字' }]}>
        <Input placeholder="如 创建任务" />
      </Form.Item>
      <Form.Item
        name="effect_type"
        label="系统效果类型"
        rules={[{ required: true }]}
        extra="效果类型决定阈值与门禁：write_action 只授予写候选资格，永不自动执行。"
      >
        <Select options={EFFECT_TYPES.map((t) => ({ value: t, label: `${t} — ${EFFECT_TYPE_NAMES[t]}` }))} />
      </Form.Item>
      <Form.Item name="description" label="描述（可选）">
        <Input.TextArea rows={2} placeholder="该意图覆盖哪些说法" />
      </Form.Item>
      <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button onClick={onCancel}>取消</Button>
        <Button type="primary" htmlType="submit">
          确定
        </Button>
      </Space>
    </Form>
  )
}
