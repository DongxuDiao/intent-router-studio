/** 改写设置：模式与阈值（版本化）、改写模型连接、术语表、健康与指标。
 * 外部模型 V1 §9：连接管理（密钥只写不读）、项目级模型选择、egress 确认。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, AutoComplete, Button, Card, Col, Descriptions, Drawer, Input, InputNumber, Popconfirm, Row, Select, Space, Switch,
  Table, Tag, Typography, message,
} from 'antd'
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { PageHeader } from '../components/common'
import { useProject } from '../store/project'
import {
  REWRITE_MODE_NAMES,
  type ProviderConnection,
  type ProviderConnectionListResponse,
  type ProviderConnectionTestResult,
  type RewriteConfigPayload,
  type RewriteConfigResponse,
  type RewriteHealth,
  type RewriteMode,
  type TerminologyResponse,
  type TerminologyTerm,
} from '../types'
import { fmtTime } from '../utils/format'

/** V2 §4.3 方案A：项目配置只保存策略字段；V1 起追加 provider_connection_id（连接引用） */
const POLICY_KEYS: (keyof RewriteConfigPayload)[] = [
  'mode', 'timeout_ms', 'min_rewrite_confidence', 'require_route_consistency', 'fallback', 'store_raw_text',
  'provider_connection_id',
]

function policyOf(config: Record<string, unknown>): RewriteConfigPayload {
  const out: Record<string, unknown> = {}
  for (const key of POLICY_KEYS) out[key] = config[key]
  if (!out.provider_connection_id) out.provider_connection_id = 'builtin:local_qwen'
  return out as unknown as RewriteConfigPayload
}

const MODE_OPTIONS = (['off', 'normalize_only', 'shadow', 'safe_apply'] as const).map((m) => ({
  value: m,
  label: REWRITE_MODE_NAMES[m],
}))

const BUILTIN_ID = 'builtin:local_qwen'
const GLM_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
/** 智谱 Chat Completions 官方模型代码；AutoComplete 仍允许输入后续新增模型。 */
const GLM_MODEL_OPTIONS = [
  'glm-5.2', 'glm-5.1', 'glm-5-turbo', 'glm-5', 'glm-4.7', 'glm-4.7-flash',
  'glm-4.7-flashx', 'glm-4.6', 'glm-4.5-air', 'glm-4.5-airx', 'glm-4.5-flash',
  'glm-4-flash-250414', 'glm-4-flashx-250414',
].map((value) => ({ value }))

const TYPE_NAMES: Record<string, string> = {
  local_qwen: '本地 Qwen',
  glm: '智谱 GLM',
  openai_compatible: 'OpenAI 兼容',
}

/** 连接抽屉草稿：api_key 只存在于该本地状态，保存后立即清空（§9.2） */
interface ConnDraft {
  name: string
  provider_type: 'glm' | 'openai_compatible'
  base_url: string
  model_id: string
  api_key: string
  temperature: number
  max_tokens: number
  thinking_disabled: boolean
  json_mode: boolean
  egress: boolean
}

interface SaveConnectionResult {
  saved: ProviderConnection
  test: ProviderConnectionTestResult | null
}

function emptyDraft(type: 'glm' | 'openai_compatible' = 'glm'): ConnDraft {
  return {
    name: '',
    provider_type: type,
    base_url: type === 'glm' ? GLM_BASE_URL : '',
    model_id: type === 'glm' ? 'glm-5.2' : '',
    api_key: '',
    temperature: 0.1,
    max_tokens: 256,
    thinking_disabled: true,
    json_mode: true,
    egress: false,
  }
}

function draftFrom(c: ProviderConnection): ConnDraft {
  const gc = c.generation_config ?? {}
  return {
    name: c.name,
    provider_type: (c.provider_type === 'openai_compatible' ? 'openai_compatible' : 'glm'),
    base_url: c.base_url ?? '',
    model_id: c.model_id ?? '',
    api_key: '',
    temperature: typeof gc.temperature === 'number' ? gc.temperature : 0.1,
    max_tokens: typeof gc.max_tokens === 'number' ? gc.max_tokens : 256,
    thinking_disabled: gc.thinking !== true,
    json_mode: gc.json_mode !== false,
    egress: true,
  }
}

export default function RewriteSettings() {
  const { projectId } = useProject()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<RewriteConfigPayload | null>(null)
  const [termsText, setTermsText] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<ProviderConnection | null>(null)
  const [connDraft, setConnDraft] = useState<ConnDraft>(emptyDraft())

  const config = useQuery({
    queryKey: ['rewrite-config', projectId],
    enabled: !!projectId,
    queryFn: () => api<RewriteConfigResponse>(`/projects/${projectId}/rewrite-config`),
  })
  const terminology = useQuery({
    queryKey: ['terminology', projectId],
    enabled: !!projectId,
    queryFn: () => api<TerminologyResponse>(`/projects/${projectId}/terminology`),
  })
  const health = useQuery({
    queryKey: ['rewrite-health'],
    refetchInterval: 15_000,
    queryFn: () => api<RewriteHealth>('/inference/rewrite/health'),
  })
  const connections = useQuery({
    queryKey: ['provider-connections'],
    queryFn: () => api<ProviderConnectionListResponse>('/rewrite/provider-connections'),
  })

  useEffect(() => {
    if (config.data && !draft) setDraft(policyOf(config.data.active.config as unknown as Record<string, unknown>))
  }, [config.data, draft])

  useEffect(() => {
    if (terminology.data) {
      setTermsText(
        terminology.data.active.terms
          .map((t) => `${t.canonical} | ${(t.aliases ?? []).join(', ')}`)
          .join('\n'),
      )
    }
  }, [terminology.data])

  const invalidateConnections = () => qc.invalidateQueries({ queryKey: ['provider-connections'] })

  const saveConfig = useMutation({
    mutationFn: (payload: RewriteConfigPayload) =>
      api(`/projects/${projectId}/rewrite-config`, {
        method: 'PUT',
        body: JSON.stringify({ config: payload }),
      }),
    onSuccess: () => {
      message.success('配置已保存为新版本并激活（旧版本自动归档，改写缓存已失效）')
      qc.invalidateQueries({ queryKey: ['rewrite-config', projectId] })
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '保存失败'),
  })

  const saveTerminology = useMutation({
    mutationFn: (terms: TerminologyTerm[]) =>
      api(`/projects/${projectId}/terminology`, {
        method: 'PUT',
        body: JSON.stringify({ terms: { terms } }),
      }),
    onSuccess: () => {
      message.success('术语表已保存为新版本')
      qc.invalidateQueries({ queryKey: ['terminology', projectId] })
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '保存失败'),
  })

  // ---- 连接管理（§9.1/§9.2）：保存、保存并测试、测试、删除 ----
  const connPayload = (d: ConnDraft, isCreate: boolean) => ({
    name: d.name,
    provider_type: d.provider_type,
    model_id: d.model_id,
    base_url: d.provider_type === 'openai_compatible' ? d.base_url : undefined,
    api_key: d.api_key || undefined, // 编辑时空值 = 保留旧 Key
    generation_config: {
      temperature: d.temperature,
      max_tokens: d.max_tokens,
      thinking: !d.thinking_disabled,
      json_mode: d.json_mode,
    },
    ...(isCreate ? { egress_acknowledged: d.egress } : {}),
  })

  const saveConnection = useMutation<SaveConnectionResult, Error, { d: ConnDraft; thenTest: boolean }>({
    mutationFn: async ({ d, thenTest }) => {
      const isCreate = editing === null
      const body = connPayload(d, isCreate)
      const request = isCreate
        ? api<ProviderConnection>('/rewrite/provider-connections', { method: 'POST', body: JSON.stringify(body) })
        : api<ProviderConnection>(`/rewrite/provider-connections/${editing.id}`, { method: 'PATCH', body: JSON.stringify(body) })
      const saved = await request
      if (!thenTest) return { saved, test: null }
      // 保存并测试：串行调用（§9.2）
      return {
        saved,
        test: await api<ProviderConnectionTestResult>(`/rewrite/provider-connections/${saved.id}/test`, { method: 'POST' }),
      }
    },
    onSuccess: (result, { thenTest }) => {
      const { saved, test } = result
      setConnDraft((d) => ({ ...d, api_key: '' })) // 密钥即用即弃，不留在前端状态
      invalidateConnections()
      if (thenTest && test?.status === 'FAILED') {
        message.error(`连接已保存，但测试失败：${test.error_code ?? '未知错误'}${test.message ? ` · ${test.message}` : ''}`)
        return
      }
      message.success(thenTest ? `连接已保存，测试通过（${saved.name}）` : '连接已保存；尚未测试，测试通过前不能选为项目改写模型')
      if (thenTest && test?.status === 'SUCCESS') setDrawerOpen(false)
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '保存失败'),
  })

  const testConnection = useMutation({
    mutationFn: (id: string) =>
      api<ProviderConnectionTestResult>(`/rewrite/provider-connections/${id}/test`, { method: 'POST' }),
    onSuccess: (r) => {
      invalidateConnections()
      if (r.status === 'SUCCESS') message.success(`测试通过（${Math.round(r.latency_ms ?? 0)}ms）`)
      else message.error(`测试失败：${r.error_code ?? '未知错误'}${r.message ? ` · ${r.message}` : ''}`)
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '测试失败'),
  })

  const removeConnection = useMutation({
    mutationFn: (id: string) => api(`/rewrite/provider-connections/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      message.success('连接已删除')
      invalidateConnections()
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '删除失败'),
  })

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />
  if (!draft) return <Card size="small" loading />

  const h = health.data
  const dirty = JSON.stringify(draft) !== JSON.stringify(policyOf(config.data?.active.config as unknown as Record<string, unknown>))
  const selected = config.data?.selected_provider

  const connItems = connections.data?.items ?? []
  /** 项目可选模型：内置 + 启用且测试成功的远程连接（§9.3） */
  const selectableModels = connItems.filter(
    (c) => c.builtin || (c.enabled && c.last_test_status === 'SUCCESS' && c.has_api_key !== false),
  )
  const currentConnection = connItems.find((c) => c.id === draft.provider_connection_id)
  const switchingFromBuiltin = draft.provider_connection_id !== BUILTIN_ID
    && policyOf(config.data?.active.config as unknown as Record<string, unknown>).provider_connection_id === BUILTIN_ID

  const parseTerms = (): TerminologyTerm[] | null => {
    const out: TerminologyTerm[] = []
    for (const [i, line] of termsText.split('\n').entries()) {
      const trimmed = line.trim()
      if (!trimmed) continue
      const [canonical, aliases] = trimmed.split('|')
      if (!canonical?.trim()) {
        message.error(`第 ${i + 1} 行缺少规范术语`)
        return null
      }
      out.push({
        canonical: canonical.trim(),
        aliases: (aliases ?? '')
          .split(',')
          .map((a) => a.trim())
          .filter(Boolean),
      })
    }
    return out
  }

  const openCreate = () => {
    setEditing(null)
    setConnDraft(emptyDraft())
    setDrawerOpen(true)
  }

  const openEdit = (c: ProviderConnection) => {
    setEditing(c)
    setConnDraft(draftFrom(c))
    setDrawerOpen(true)
  }

  const drawerValid =
    connDraft.name.trim().length > 0 &&
    connDraft.model_id.trim().length > 0 &&
    (connDraft.provider_type === 'glm' || /^https:\/\/.+/.test(connDraft.base_url.trim())) &&
    (editing !== null || connDraft.api_key.length >= 8) &&
    connDraft.egress

  return (
    <div>
      <PageHeader
        title="改写设置"
        subTitle="Query 改写模式 / 改写模型连接 / 安全阈值 / 术语表 · 全部版本化保存，可随时一键切回 off"
      />

      {h && h.rewriter && h.rewriter.ok === false && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="rewriter 服务当前不可用"
          description="生成式改写会自动降级（下游使用原文，路由不受影响）。L0 术语归一不受影响。可在下方健康面板查看详情。"
        />
      )}

      <Row gutter={[16, 16]}>
        <Col xs={24} xxl={14}>
          <Card
            title="模式与阈值"
            size="small"
            extra={dirty ? <Tag color="orange">有未保存修改</Tag> : <Tag color="green">与生效版本一致</Tag>}
          >
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>模式</Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  value={draft.mode}
                  onChange={(mode) => setDraft({ ...draft, mode: mode as RewriteMode })}
                  options={MODE_OPTIONS}
                />
                {draft.mode === 'safe_apply' && (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginTop: 8 }}
                    message="safe_apply 仅在八项安全门全部通过时替换下游 Query；正式路由永远使用原文预测。"
                  />
                )}
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>改写模型</Typography.Text>
                <Select
                  style={{ width: '100%' }}
                  value={draft.provider_connection_id ?? BUILTIN_ID}
                  onChange={(id) => setDraft({ ...draft, provider_connection_id: id })}
                  options={selectableModels.map((c) => ({
                    value: c.id,
                    label: `${c.name}（${TYPE_NAMES[c.provider_type] ?? c.provider_type}${c.model_id ? ` · ${c.model_id}` : ''}）`,
                    disabled: !c.builtin && !(c.enabled && c.last_test_status === 'SUCCESS'),
                  }))}
                />
                {switchingFromBuiltin && currentConnection && !currentConnection.builtin && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 8 }}
                    message="外部数据传输提示"
                    description={`启用 ${currentConnection.name} 后，改写请求中的 Query、上下文和术语将发送到该外部模型服务。正式路由仍使用本地原文预测。`}
                  />
                )}
                {switchingFromBuiltin && draft.mode === 'safe_apply' && (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginTop: 8 }}
                    message="建议先回到 shadow 观察新模型，再放开 safe_apply"
                    description="切换生成模型会改变改写文本分布；后端允许直接保存，但推荐先以 shadow 模式观察若干天。"
                  />
                )}
              </div>
              <Row gutter={12}>
                <Col span={12}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>最小改写置信度</Typography.Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={0}
                    max={1}
                    step={0.05}
                    value={draft.min_rewrite_confidence}
                    onChange={(v) => setDraft({ ...draft, min_rewrite_confidence: v ?? 0.8 })}
                  />
                </Col>
                <Col span={12}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>生成超时（ms）</Typography.Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={200}
                    max={60000}
                    step={100}
                    value={draft.timeout_ms}
                    onChange={(v) => setDraft({ ...draft, timeout_ms: v ?? 90000 })}
                  />
                </Col>
              </Row>
              <Space direction="vertical" size={4}>
                <span>
                  <Switch
                    size="small"
                    checked={draft.require_route_consistency}
                    onChange={(v) => setDraft({ ...draft, require_route_consistency: v })}
                  />{' '}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>要求路由一致（关闭后仍拦截非写→写升级）</Typography.Text>
                </span>
                <span>
                  <Switch
                    size="small"
                    checked={draft.store_raw_text}
                    onChange={(v) => setDraft({ ...draft, store_raw_text: v })}
                  />{' '}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>反馈允许保存原文（默认仅哈希）</Typography.Text>
                </span>
              </Space>
              <Popconfirm
                title="保存为新版本并激活？"
                description="旧版本自动归档；改写缓存按项目失效。"
                onConfirm={() => saveConfig.mutate(draft)}
                disabled={!dirty}
              >
                <Button type="primary" disabled={!dirty} loading={saveConfig.isPending}>
                  保存为新版本
                </Button>
              </Popconfirm>
            </Space>
          </Card>

          <Card
            title="改写模型连接"
            size="small"
            style={{ marginTop: 16 }}
            styles={{ body: { minWidth: 0, overflow: 'hidden' } }}
            extra={(
              <Space wrap>
                {selected && (
                  <Tag color={selected.available ? 'green' : 'red'}>
                    当前：{selected.name}
                  </Tag>
                )}
                <Button size="small" type="primary" onClick={openCreate}>新建连接</Button>
              </Space>
            )}
          >
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              tableLayout="fixed"
              scroll={{ x: 800 }}
              loading={connections.isLoading}
              dataSource={connItems}
              columns={[
                {
                  title: '名称', dataIndex: 'name', width: 180, ellipsis: true,
                  render: (n: string, c) => (
                    <Space size={4} wrap={false} style={{ maxWidth: '100%' }}>
                      <Typography.Text ellipsis style={{ maxWidth: c.builtin ? 125 : 160 }}>{n}</Typography.Text>
                      {c.builtin && <Tag style={{ fontSize: 10 }}>内置</Tag>}
                    </Space>
                  ),
                },
                { title: '类型', dataIndex: 'provider_type', width: 110, render: (t: string) => TYPE_NAMES[t] ?? t },
                { title: '模型', dataIndex: 'model_id', width: 130, ellipsis: true },
                {
                  title: '密钥', width: 90,
                  render: (_, c) => (c.builtin ? '-' : <Typography.Text code style={{ fontSize: 11 }}>{c.api_key_hint ?? '****'}</Typography.Text>),
                },
                {
                  title: '测试', width: 120,
                  render: (_, c) => {
                    if (c.builtin) return <Tag color={c.available ? 'green' : 'default'}>{c.available ? '可用' : '未就绪'}</Tag>
                    if (c.last_test_status === 'SUCCESS') return <Tag color="green">通过 {Math.round(c.last_test_latency_ms ?? 0)}ms</Tag>
                    if (c.last_test_status === 'FAILED') return <Tag color="red">失败</Tag>
                    return <Tag color="orange">尚未测试</Tag>
                  },
                },
                {
                  title: '操作', width: 190,
                  render: (_, c) => (
                    <Space size={[4, 4]} wrap>
                      {!c.builtin && <Button size="small" onClick={() => openEdit(c)}>编辑</Button>}
                      {!c.builtin && (
                        <Button size="small" loading={testConnection.isPending} onClick={() => testConnection.mutate(c.id)}>
                          测试
                        </Button>
                      )}
                      {!c.builtin && (
                        <Popconfirm
                          title="删除该连接？"
                          description={c.in_use_by_projects ? `被 ${c.in_use_by_projects} 个项目引用，无法删除` : '密文将一并删除，不可恢复。'}
                          onConfirm={() => removeConnection.mutate(c.id)}
                          disabled={(c.in_use_by_projects ?? 0) > 0}
                        >
                          <Button size="small" danger disabled={(c.in_use_by_projects ?? 0) > 0}>删除</Button>
                        </Popconfirm>
                      )}
                    </Space>
                  ),
                },
              ]}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
              API Key 加密存储（AES-256-GCM）、只写不回显；GLM 端点固定为官方通用开放平台地址。远程连接需测试通过后才能选为项目改写模型。
            </Typography.Text>
          </Card>

          <Card title="配置版本历史" size="small" style={{ marginTop: 16 }}>
            <Table
              size="small"
              rowKey="id"
              pagination={{ pageSize: 5 }}
              dataSource={config.data?.versions ?? []}
              columns={[
                { title: '版本', dataIndex: 'version', width: 60 },
                { title: '模式', dataIndex: ['config', 'mode'], width: 130, render: (m: string) => <Tag>{m}</Tag> },
                { title: '状态', dataIndex: 'status', width: 90, render: (s: string) => (s === 'ACTIVE' ? <Tag color="green">生效中</Tag> : <Tag>已归档</Tag>) },
                { title: '时间', dataIndex: 'created_at', width: 150, render: fmtTime },
              ]}
            />
          </Card>
        </Col>

        <Col xs={24} xxl={10}>
          <Card
            title="术语表（L0 确定性归一）"
            size="small"
            extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>每行：规范术语 | 别名1, 别名2</Typography.Text>}
          >
            <Space direction="vertical" style={{ width: '100%' }} size={10}>
              <Input.TextArea
                rows={10}
                value={termsText}
                onChange={(e) => setTermsText(e.target.value)}
                placeholder={'Libra 实验 | libra exp, libra实验\n审批中心 | approval hub'}
              />
              <Button
                type="primary"
                loading={saveTerminology.isPending}
                onClick={() => {
                  const terms = parseTerms()
                  if (terms) saveTerminology.mutate(terms)
                }}
              >
                保存术语表
              </Button>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                别名在改写前被最长匹配替换为规范术语（可设 never_replace_when 守卫）；替换 trace 在 Playground「Query 理解」中逐条展示。
              </Typography.Text>
            </Space>
          </Card>

          <Card title="rewriter 服务健康" size="small" style={{ marginTop: 16 }}>
            {h ? (
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
                  <Descriptions.Item label="rewriter">
                    <Tag color={h.rewriter?.ok ? 'green' : 'red'}>{h.rewriter?.ok ? '正常' : '不可用'}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="请求总数">{h.metrics.requests_total}</Descriptions.Item>
                  <Descriptions.Item label="缓存命中">{h.metrics.cache_hit_total}</Descriptions.Item>
                  <Descriptions.Item label="降级次数">
                    {Object.entries(h.metrics.fallback_total).map(([k, v]) => (
                      <Tag key={k} color="orange" style={{ fontSize: 11 }}>{k}: {v}</Tag>
                    )) || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="安全拦截">
                    {Object.entries(h.metrics.safety_reject_total).map(([k, v]) => (
                      <Tag key={k} color="red" style={{ fontSize: 11 }}>{k}: {v}</Tag>
                    )) || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="改写延迟 P50">{h.metrics.rewrite_latency_ms.p50?.toFixed(1) ?? '-'} ms</Descriptions.Item>
                  <Descriptions.Item label="P95">{h.metrics.rewrite_latency_ms.p95?.toFixed(1) ?? '-'} ms</Descriptions.Item>
                </Descriptions>
                {h.connections && Object.keys(h.connections).length > 0 && (
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>按连接熔断（外部模型 V1）：</Typography.Text>
                    <div style={{ marginTop: 4 }}>
                      {Object.entries(h.connections).map(([id, c]) => (
                        <Tag
                          key={id}
                          color={c.state === 'closed' ? 'green' : c.state === 'unhealthy' ? 'red' : 'orange'}
                          style={{ fontSize: 11 }}
                        >
                          {id === BUILTIN_ID ? '本地 Qwen' : id.slice(0, 12)}：{c.state}
                          {c.unhealthy_code ? `（${c.unhealthy_code}）` : ''}
                        </Tag>
                      ))}
                    </div>
                  </div>
                )}
                {Object.keys(h.metrics.route_conflict_total).length > 0 && (
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>路由冲突分布：</Typography.Text>
                    {Object.entries(h.metrics.route_conflict_total).map(([k, v]) => (
                      <Tag key={k} style={{ fontSize: 11 }}>{k}: {v}</Tag>
                    ))}
                  </div>
                )}
              </Space>
            ) : (
              <Typography.Text type="secondary">加载中…</Typography.Text>
            )}
          </Card>
        </Col>
      </Row>

      <Drawer
        title={editing ? `编辑连接 · ${editing.name}` : '新建模型连接'}
        width={520}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>Provider 类型</Typography.Text>
            <Select
              style={{ width: '100%' }}
              value={connDraft.provider_type}
              disabled={editing !== null}
              onChange={(t) => setConnDraft({
                ...connDraft,
                provider_type: t,
                base_url: t === 'glm' ? GLM_BASE_URL : '',
                model_id: t === 'glm' ? 'glm-5.2' : connDraft.model_id,
              })}
              options={[
                { value: 'glm', label: '智谱 GLM（官方端点）' },
                { value: 'openai_compatible', label: 'OpenAI 兼容 API（自定义 Base URL）' },
              ]}
            />
          </div>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>连接名称</Typography.Text>
            <Input
              value={connDraft.name}
              onChange={(e) => setConnDraft({ ...connDraft, name: e.target.value })}
              placeholder="如：生产 GLM"
              maxLength={100}
            />
          </div>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              Base URL{connDraft.provider_type === 'glm' ? '（GLM 固定官方端点，不可修改）' : '（仅允许 https 公网地址）'}
            </Typography.Text>
            <Input
              value={connDraft.provider_type === 'glm' ? GLM_BASE_URL : connDraft.base_url}
              disabled={connDraft.provider_type === 'glm'}
              onChange={(e) => setConnDraft({ ...connDraft, base_url: e.target.value })}
              placeholder={connDraft.provider_type === 'glm' ? GLM_BASE_URL : 'https://api.example.com/v1'}
            />
          </div>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              Model ID{connDraft.provider_type === 'glm' ? '（请选择官方代码，也可输入新模型）' : ''}
            </Typography.Text>
            {connDraft.provider_type === 'glm' ? (
              <AutoComplete
                style={{ width: '100%' }}
                value={connDraft.model_id}
                options={GLM_MODEL_OPTIONS}
                onChange={(model_id) => setConnDraft({ ...connDraft, model_id })}
                placeholder="如 glm-5.2 或 glm-4.5-flash"
                filterOption={(input, option) => String(option?.value ?? '').includes(input.toLowerCase())}
              />
            ) : (
              <Input
                value={connDraft.model_id}
                onChange={(e) => setConnDraft({ ...connDraft, model_id: e.target.value })}
                placeholder="模型 ID"
                maxLength={200}
              />
            )}
          </div>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              API Key{editing ? '（留空表示保留原值）' : '（只写不回显，加密存储）'}
            </Typography.Text>
            <Input.Password
              value={connDraft.api_key}
              onChange={(e) => setConnDraft({ ...connDraft, api_key: e.target.value })}
              placeholder={editing ? '留空保持不变' : '粘贴开放平台 API Key'}
              autoComplete="new-password"
            />
          </div>
          <Row gutter={12}>
            <Col span={12}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>Temperature（0~1）</Typography.Text>
              <InputNumber
                style={{ width: '100%' }}
                min={0} max={1} step={0.05}
                value={connDraft.temperature}
                onChange={(v) => setConnDraft({ ...connDraft, temperature: v ?? 0.1 })}
              />
            </Col>
            <Col span={12}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>Max tokens（64~1024）</Typography.Text>
              <InputNumber
                style={{ width: '100%' }}
                min={64} max={1024} step={32}
                value={connDraft.max_tokens}
                onChange={(v) => setConnDraft({ ...connDraft, max_tokens: v ?? 256 })}
              />
            </Col>
          </Row>
          <Space direction="vertical" size={4}>
            <span>
              <Switch
                size="small"
                checked={connDraft.thinking_disabled}
                onChange={(v) => setConnDraft({ ...connDraft, thinking_disabled: v })}
              />{' '}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>关闭 Thinking（GLM-4.5+ 默认开思考，改写任务建议关闭）</Typography.Text>
            </span>
            <span>
              <Switch
                size="small"
                checked={connDraft.json_mode}
                onChange={(v) => setConnDraft({ ...connDraft, json_mode: v })}
              />{' '}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>JSON mode（response_format=json_object）</Typography.Text>
            </span>
          </Space>
          {!editing && (
            <Alert
              type="warning"
              showIcon
              message={(
                <span>
                  <input
                    type="checkbox"
                    checked={connDraft.egress}
                    onChange={(e) => setConnDraft({ ...connDraft, egress: e.target.checked })}
                    style={{ marginRight: 8 }}
                  />
                  我确认改写请求中的 Query、上下文和术语可能被发送到该外部模型服务。
                </span>
              )}
            />
          )}
          <Space>
            <Button
              type="primary"
              disabled={!drawerValid}
              loading={saveConnection.isPending}
              onClick={() => saveConnection.mutate({ d: connDraft, thenTest: true })}
            >
              保存并测试
            </Button>
            <Button
              disabled={!drawerValid}
              loading={saveConnection.isPending}
              onClick={() => saveConnection.mutate({ d: connDraft, thenTest: false })}
            >
              仅保存
            </Button>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            保存后显示「尚未测试」；测试通过前该连接不能选为项目改写模型。测试会发起一次真实（收费）的最小改写请求。
          </Typography.Text>
        </Space>
      </Drawer>
    </div>
  )
}
