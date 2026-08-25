/** 改写设置：模式与阈值（版本化）、术语表、rewriter 健康与指标（§13）。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Button, Card, Col, Descriptions, Input, InputNumber, Popconfirm, Row, Select, Space, Switch,
  Table, Tag, Typography, message,
} from 'antd'
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { PageHeader } from '../components/common'
import { useProject } from '../store/project'
import {
  REWRITE_MODE_NAMES,
  type RewriteConfigPayload,
  type RewriteConfigResponse,
  type RewriteHealth,
  type RewriteMode,
  type TerminologyResponse,
  type TerminologyTerm,
} from '../types'

/** V2 §4.3 方案A：项目配置只保存策略字段，部署字段（provider/model 等）只读展示 */
const POLICY_KEYS: (keyof RewriteConfigPayload)[] = [
  'mode', 'timeout_ms', 'min_rewrite_confidence', 'require_route_consistency', 'fallback', 'store_raw_text',
]

function policyOf(config: Record<string, unknown>): RewriteConfigPayload {
  const out: Record<string, unknown> = {}
  for (const key of POLICY_KEYS) out[key] = config[key]
  return out as unknown as RewriteConfigPayload
}
import { fmtTime } from '../utils/format'

const MODE_OPTIONS = (['off', 'normalize_only', 'shadow', 'safe_apply'] as const).map((m) => ({
  value: m,
  label: REWRITE_MODE_NAMES[m],
}))

export default function RewriteSettings() {
  const { projectId } = useProject()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<RewriteConfigPayload | null>(null)
  const [termsText, setTermsText] = useState('')

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

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />
  if (!draft) return <Card size="small" loading />

  const h = health.data
  const dirty = JSON.stringify(draft) !== JSON.stringify(policyOf(config.data?.active.config as unknown as Record<string, unknown>))
  const deployment = config.data?.deployment

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

  return (
    <div>
      <PageHeader
        title="改写设置"
        subTitle="Query 改写模式 / 安全阈值 / 术语表 · 全部版本化保存，可随时一键切回 off"
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

      <Row gutter={16}>
        <Col span={12}>
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
            title="部署配置（只读）"
            size="small"
            style={{ marginTop: 16 }}
            extra={<Tag color="blue">部署环境管理</Tag>}
          >
            {deployment ? (
              <Descriptions size="small" column={2}>
                <Descriptions.Item label="服务状态">
                  <Tag color={deployment.available ? 'green' : 'red'}>{deployment.available ? '正常' : '不可用'}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="provider">{deployment.provider ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="模型">{deployment.model_id ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="device">{deployment.device ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="max_new_tokens">{deployment.max_new_tokens ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="prompt 版本">{deployment.prompt_version ?? '-'}</Descriptions.Item>
              </Descriptions>
            ) : (
              <Typography.Text type="secondary">加载中…</Typography.Text>
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
              生成模型与资源参数由 rewriter 部署的 REWRITE_* 环境变量决定，项目级不可修改；上方"模式与阈值"仅控制改写策略。
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

        <Col span={12}>
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
                <Descriptions size="small" column={2}>
                  <Descriptions.Item label="熔断状态">
                    <Tag color={h.breaker_state === 'closed' ? 'green' : h.breaker_state === 'open' ? 'red' : 'orange'}>
                      {h.breaker_state}
                    </Tag>
                  </Descriptions.Item>
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
    </div>
  )
}
