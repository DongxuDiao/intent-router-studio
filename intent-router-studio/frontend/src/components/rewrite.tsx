/** Query 改写组件（修改方案 §13）：三 Query 对照、安全检查、反馈闭环。 */
import { useMutation } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Input, Modal, Select, Space, Spin, Switch, Tag, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { LabelTag } from './common'
import { patchRewriteCache, readPlaygroundCache } from '../utils/playgroundCache'
import {
  LABEL_NAMES,
  REWRITE_MODE_NAMES,
  REWRITE_REASON_NAMES,
  intentName,
  type QueryUnderstanding,
  type RewriteMode,
} from '../types'

/** 三 Query 对照：原始 / 规范化 / 独立可理解（§6.1 核心展示）。 */
export function RewriteDiff({ u }: { u: QueryUnderstanding }) {
  const r = u.rewrite
  const rows: { label: string; value: string; highlight: boolean; note?: string }[] = [
    { label: '原始 Query', value: r.original_query, highlight: false },
    { label: '规范化后', value: r.normalized_query, highlight: r.normalized_query !== r.original_query, note: '去除空白/全角等表达噪声' },
    { label: '独立可理解', value: r.standalone_query, highlight: r.changed, note: '指代解析 / 术语归一 / 省略补全后的文本' },
  ]
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      {rows.map((row) => (
        <div key={row.label} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
          <Typography.Text type="secondary" style={{ width: 84, flexShrink: 0, fontSize: 12 }}>
            {row.label}
          </Typography.Text>
          <Typography.Text
            code={row.highlight}
            style={row.highlight ? { background: '#fff7e6', padding: '1px 6px' } : undefined}
          >
            {row.value}
          </Typography.Text>
          {row.note && row.highlight && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {row.note}
            </Typography.Text>
          )}
        </div>
      ))}
      {r.term_replacements.length > 0 && (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 4 }}>
          {r.term_replacements.map((t, i) => (
            <Tag key={i} color="geekblue" style={{ fontSize: 11 }}>
              {t.source_term} → {t.target_term}
            </Tag>
          ))}
        </div>
      )}
    </Space>
  )
}

const SAFETY_CHECK_NAMES: Record<string, string> = {
  nonempty_and_length: '非空与长度',
  negation_preserved: '否定一致',
  modality_preserved: '语气未强化',
  entities_traceable: '实体可追溯',
  objects_traceable: '动作对象可溯源',
  no_new_high_risk_action: '无新增高风险动作',
  confidence_threshold: '置信度阈值',
  provider_preserved_intent: '模型自报意图保留',
  route_consistency: '路由一致性',
  semantic_checks_skipped: '语义检查（文本未变化，跳过）',
}

/** 八项安全门逐项展示 + reason codes（§7.1）。 */
export function RewriteSafetyChecks({ u }: { u: QueryUnderstanding }) {
  const s = u.safety
  if (!s) {
    return (
      <Alert
        type={u.fallback_reason ? 'warning' : 'info'}
        showIcon
        message={u.fallback_reason ? `已降级：${REWRITE_REASON_NAMES[u.fallback_reason] ?? u.fallback_reason}` : '该模式不执行安全门'}
      />
    )
  }
  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      {s.checks.map((c) => (
        <div key={c.name} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
          <span style={{ width: 14 }}>{c.passed ? '✅' : '❌'}</span>
          <Typography.Text style={{ width: 150, flexShrink: 0, fontSize: 12 }}>
            {SAFETY_CHECK_NAMES[c.name] ?? c.name}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>{c.detail}</Typography.Text>
        </div>
      ))}
      {s.reason_codes.length > 0 && (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 4 }}>
          {s.reason_codes.map((c) => (
            <Tag key={c} color={BLOCKING_CODES.has(c) ? 'red' : 'blue'} style={{ fontSize: 11 }}>
              {c} · {REWRITE_REASON_NAMES[c] ?? c}
            </Tag>
          ))}
        </div>
      )}
    </Space>
  )
}

const BLOCKING_CODES = new Set([
  'NEGATION_CHANGED', 'MODALITY_CHANGED', 'ACTION_INTENSIFIED', 'OBJECT_INVENTED', 'ROUTE_CONFLICT',
])

const ERROR_REASON_OPTIONS = [
  '改变了意图', '改变了否定关系', '创造了对象或参数', '指代解析错误',
  '术语归一错误', '遗漏重要信息', '表达不自然', '不需要改写',
]

function decisionMeta(u: QueryUnderstanding): { color: string; text: string } {
  switch (u.safety_decision) {
    case 'allow_rewrite':
      return { color: 'success', text: '安全门通过 · 已采用改写' }
    case 'allow_rewrite_shadow':
      return { color: 'blue', text: '安全门通过（影子模式，不替换下游）' }
    case 'blocked':
      return { color: 'error', text: '安全门拦截 · 下游使用原文' }
    case 'fallback_original':
      return { color: 'warning', text: `改写降级 · 下游使用原文（${REWRITE_REASON_NAMES[u.fallback_reason ?? ''] ?? u.fallback_reason ?? ''}）` }
    case 'mode_off':
      return { color: 'default', text: '改写已关闭' }
    default:
      return { color: 'default', text: u.safety_decision }
  }
}

/** Playground「Query 理解」面板：输入 → 双路路由 + 三 Query + 安全门 + 反馈。 */
/** 外部模型 V1 §9.4：Provider 调用 Trace（只含元信息，不含密钥/原文/完整错误体） */
export function ProviderTraceCard({ u }: { u: QueryUnderstanding }) {
  const t = u.provider_trace
  if (!t) return null
  return (
    <Card title="Provider Trace（改写模型调用；不含密钥与原文）" size="small">
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>连接</Typography.Text>
          <Typography.Text code style={{ fontSize: 11 }}>
            {t.connection_id === 'builtin:local_qwen' ? '本地 Qwen' : t.connection_id ?? '-'}
          </Typography.Text>
          {t.connection_revision != null && <Tag style={{ fontSize: 11 }}>rev {t.connection_revision}</Tag>}
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>模型</Typography.Text>
          <Typography.Text style={{ fontSize: 12 }}>
            {t.provider ?? '-'} / {t.model_id ?? '-'}
          </Typography.Text>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>调用</Typography.Text>
          <Tag style={{ fontSize: 11 }}>
            provider {t.provider_latency_ms != null ? `${Math.round(t.provider_latency_ms)}ms` : '-'}
          </Tag>
          {t.usage?.total_tokens != null && (
            <Tag style={{ fontSize: 11 }}>
              tokens {t.usage.total_tokens}
              {t.usage.completion_tokens != null ? `（生成 ${t.usage.completion_tokens}）` : ''}
            </Tag>
          )}
          {u.fallback_reason && <Tag color="orange" style={{ fontSize: 11 }}>降级 {u.fallback_reason}</Tag>}
          {u.cache_hit && <Tag color="blue" style={{ fontSize: 11 }}>缓存命中（无实际调用）</Tag>}
        </div>
        {t.provider_request_id && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>请求 ID</Typography.Text>
            <Typography.Text code style={{ fontSize: 11 }}>{t.provider_request_id}</Typography.Text>
          </div>
        )}
      </Space>
    </Card>
  )
}

export function QueryRewritePanel({ projectId }: { projectId: string }) {
  const initial = readPlaygroundCache(projectId)?.rewrite
  const [text, setText] = useState(initial?.text ?? '这个怎么停？')
  const [context, setContext] = useState(initial?.context ?? '当前讨论实验 123')
  const [mode, setMode] = useState<RewriteMode | 'project_default'>(initial?.mode ?? 'project_default')
  const [understanding, setUnderstanding] = useState<QueryUnderstanding | null>(initial?.understanding ?? null)
  const [edited, setEdited] = useState(initial?.edited ?? '')
  const [editOpen, setEditOpen] = useState(false)
  const [rejectReasons, setRejectReasons] = useState<string[]>(initial?.rejectReasons ?? [])
  const [saveText, setSaveText] = useState(initial?.saveText ?? false)

  useEffect(() => {
    const saved = readPlaygroundCache(projectId)?.rewrite
    setText(saved?.text ?? '这个怎么停？')
    setContext(saved?.context ?? '当前讨论实验 123')
    setMode(saved?.mode ?? 'project_default')
    setUnderstanding(saved?.understanding ?? null)
    setEdited(saved?.edited ?? '')
    setRejectReasons(saved?.rejectReasons ?? [])
    setSaveText(saved?.saveText ?? false)
    understand.reset()
  // projectId is the cache hydration boundary.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  const understand = useMutation({
    mutationFn: () =>
      api<QueryUnderstanding>('/inference/rewrite', {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          text,
          context: context || null,
          mode: mode === 'project_default' ? null : mode,
        }),
      }),
    onSuccess: (data) => {
      setUnderstanding(data)
      setEdited(data.rewrite.standalone_query)
      patchRewriteCache(projectId, { understanding: data, edited: data.rewrite.standalone_query })
    },
    onError: (e) => message.error(e instanceof ApiError ? `${e.code}: ${e.message}` : '请求失败'),
  })

  const sendFeedback = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api(`/projects/${projectId}/rewrite-feedback`, { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => message.success('反馈已记录（默认仅哈希，不含原文）'),
    onError: (e) => message.error(e instanceof Error ? e.message : '反馈失败'),
  })

  const submitFeedback = (verdict: 'accept' | 'reject' | 'edit', reasons: string[] = [], editedText?: string) => {
    if (!understanding) return
    sendFeedback.mutate({
      text,
      context: context || null,
      proposed_rewrite: understanding.rewrite.standalone_query,
      edited_rewrite: editedText,
      verdict,
      reason_codes: reasons,
      original_route: understanding.original_route?.route,
      rewrite_route: understanding.rewrite_route?.route,
      store_raw_text: saveText,
    })
  }

  const u = understanding
  const meta = u ? decisionMeta(u) : null

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="输入" size="small">
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Input.TextArea rows={2} value={text} onChange={(e) => { setText(e.target.value); patchRewriteCache(projectId, { text: e.target.value }) }} placeholder="用户 query（可含指代 / 省略 / 术语别名）" />
          <Input.TextArea rows={2} value={context} onChange={(e) => { setContext(e.target.value); patchRewriteCache(projectId, { context: e.target.value }) }} placeholder="上下文（多轮上文，指代解析依据；不提供则无法解析指代）" />
          <Space wrap>
            <Select
              style={{ width: 420 }}
              value={mode}
              onChange={(v) => { setMode(v); patchRewriteCache(projectId, { mode: v }) }}
              options={[
                { value: 'project_default', label: '跟随项目配置' },
                ...(['off', 'normalize_only', 'shadow', 'safe_apply'] as const).map((m) => ({
                  value: m,
                  label: REWRITE_MODE_NAMES[m],
                })),
              ]}
            />
            <Button type="primary" loading={understand.isPending} onClick={() => understand.mutate()}>
              理解 Query
            </Button>
          </Space>
        </Space>
      </Card>

      {understand.isPending && <Card size="small"><Spin tip="解析中（含改写与双路分类）…" /></Card>}

      {u && (
        <>
          {u.fallback_reason && (
            <Alert
              type="warning"
              showIcon
              message={`改写服务不可用，已自动降级（${REWRITE_REASON_NAMES[u.fallback_reason] ?? u.fallback_reason}）`}
              description="路由结果与下游 Query 均不受影响：正式路由永远来自原文预测，降级时下游使用原文。"
            />
          )}
          {u.safety?.escalation && (
            <Alert
              type="error"
              showIcon
              message="检测到「非写意图 → 写操作」升级，已强制拦截"
              description="无论项目配置如何，效果等级升级（none/read_only → write_action）都是硬性禁止项。"
            />
          )}

          <Card
            title="Query 理解结果"
            size="small"
            extra={meta ? <Tag color={meta.color}>{meta.text}</Tag> : null}
          >
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <RewriteDiff u={u} />
              <Descriptions size="small" column={3} bordered>
                <Descriptions.Item label="模式">{REWRITE_MODE_NAMES[u.mode] ?? u.mode}</Descriptions.Item>
                <Descriptions.Item label="改写类型">{u.rewrite.rewrite_type}</Descriptions.Item>
                <Descriptions.Item label="置信度">{u.rewrite.confidence.toFixed(2)}</Descriptions.Item>
              </Descriptions>
            </Space>
          </Card>

          <Card title="双路路由（正式路由恒为原文路由）" size="small">
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>原文路由（正式）</Typography.Text>
                <LabelTag label={u.original_route?.route} />
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>final_route 恒等于此结果</Typography.Text>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>改写文本路由</Typography.Text>
                {u.rewrite_route ? <LabelTag label={u.rewrite_route.route} /> : <Tag>未评估</Tag>}
                <Tag color={u.route_consistent ? 'success' : 'warning'} style={{ fontSize: 11 }}>
                  {u.route_consistent ? '一致' : '不一致（保留原文）'}
                </Tag>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>业务意图</Typography.Text>
                <Tag style={{ fontSize: 11 }}>{intentName(u.original_route) ?? '-'}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>→</Typography.Text>
                <Tag style={{ fontSize: 11 }}>{u.rewrite_route ? intentName(u.rewrite_route) ?? '-' : '未评估'}</Tag>
                <Tag color={u.intent_consistent === false ? 'orange' : 'default'} style={{ fontSize: 11 }}>
                  {u.intent_consistent === false ? '意图漂移（仅记录，效果层见上）' : '意图一致'}
                </Tag>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Typography.Text type="secondary" style={{ width: 120, fontSize: 12 }}>下游 Query</Typography.Text>
                <Typography.Text code style={{ background: u.downstream_query_source === 'rewrite' ? '#f6ffed' : undefined, padding: '1px 6px' }}>
                  {u.downstream_query}
                </Typography.Text>
                <Tag color={u.downstream_query_source === 'rewrite' ? 'green' : 'default'} style={{ fontSize: 11 }}>
                  来源：{u.downstream_query_source === 'rewrite' ? '改写' : '原文'}
                </Tag>
                {u.cache_hit && <Tag color="blue" style={{ fontSize: 11 }}>缓存命中</Tag>}
              </div>
            </Space>
          </Card>

          {u.provider_trace && <ProviderTraceCard u={u} />}

          <Card title="Rewrite Safety Gate（八项检查）" size="small">
            <RewriteSafetyChecks u={u} />
          </Card>

          <Card title="反馈（帮助改进改写质量）" size="small">
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              <Space wrap>
                <Button type="primary" size="small" onClick={() => submitFeedback('accept')} loading={sendFeedback.isPending}>
                  ✅ 采用
                </Button>
                <Button danger size="small" onClick={() => submitFeedback('reject', rejectReasons)} loading={sendFeedback.isPending}>
                  ❌ 拒绝
                </Button>
                <Button size="small" onClick={() => setEditOpen(true)}>
                  ✏️ 编辑后采用
                </Button>
                <span>
                  <Switch size="small" checked={saveText} onChange={(v) => { setSaveText(v); patchRewriteCache(projectId, { saveText: v }) }} />{' '}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>保存原文（默认只存哈希）</Typography.Text>
                </span>
              </Space>
              <Select
                mode="multiple"
                style={{ width: '100%' }}
                placeholder="拒绝原因（可多选，随拒绝反馈提交）"
                value={rejectReasons}
                onChange={(v) => { setRejectReasons(v); patchRewriteCache(projectId, { rejectReasons: v }) }}
                options={ERROR_REASON_OPTIONS.map((r) => ({ value: r, label: r }))}
              />
            </Space>
          </Card>
        </>
      )}

      <Modal
        title="编辑改写结果"
        open={editOpen}
        onOk={() => {
          submitFeedback('edit', rejectReasons, edited)
          setEditOpen(false)
        }}
        onCancel={() => setEditOpen(false)}
        okText="提交编辑版"
        cancelText="取消"
      >
        <Input.TextArea rows={3} value={edited} onChange={(e) => { setEdited(e.target.value); patchRewriteCache(projectId, { edited: e.target.value }) }} />
      </Modal>
    </Space>
  )
}
