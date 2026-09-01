/** 标注台：键盘 1-9 快速打标，支持过滤未标注；仅 DRAFT 数据集可编辑。
 * 标签选项来自该数据集绑定的 Schema 版本（§7.2），展示「业务名称 + 效果徽标」，
 * 不再使用前端固定五分类。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Checkbox, Input, Space, Spin, Tag, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { EffectTypeTag, LabelTag, PageHeader } from '../components/common'
import { activeSchemaLabels, useSchemaDetail } from '../hooks/labelSchema'
import { useProject } from '../store/project'
import type { DatasetVersion, Sample } from '../types'

export default function LabelWorkbench() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { projectId } = useProject()
  const [unlabeledOnly, setUnlabeledOnly] = useState(true)
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [cursor, setCursor] = useState(0)
  const [busy, setBusy] = useState(false)

  const dataset = useQuery({
    queryKey: ['dataset', id],
    queryFn: () => api<DatasetVersion>(`/datasets/${id}`),
  })
  const samples = useQuery({
    queryKey: ['samples-label', id, unlabeledOnly, q, page],
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), page_size: '50' })
      if (unlabeledOnly) params.set('unlabeled_only', 'true')
      if (q) params.set('q', q)
      return api<{ samples: Sample[]; total: number; page: number }>(`/datasets/${id}/samples?${params}`)
    },
  })

  const list = samples.data?.samples ?? []
  const current = list[cursor]

  // 数据集绑定的 Schema（Review 修复 §3.1）：历史数据集按导入时版本展示标签，
  // 不跟随项目当前生效版本
  const schemaQuery = useSchemaDetail(
    dataset.data?.project_id,
    dataset.data?.schema_id ?? (dataset.data?.manifest?.label_schema_id as string | undefined),
  )
  const schemaLabels = activeSchemaLabels(schemaQuery.data)

  const patch = useMutation({
    mutationFn: (payload: { label?: string; is_hard_negative?: boolean }) =>
      api(`/datasets/${id}/samples/${current!.sample_id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    onSuccess: (_d, payload) => {
      const labelName = payload.label ? schemaLabels.find((item) => item.key === payload.label)?.name ?? payload.label : '难负例'
      message.success({ content: `已标注 → ${labelName}（${current!.sample_id.slice(0, 10)}…）`, duration: 0.8 })
      qc.invalidateQueries({ queryKey: ['samples-label', id] })
      qc.invalidateQueries({ queryKey: ['dataset', id] })
      if (cursor >= list.length - 1) {
        if ((samples.data?.total ?? 0) > page * 50) setPage((p) => p + 1)
        setCursor(0)
      } else {
        setCursor((c) => c + 1)
      }
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
    },
  })

  // 键盘快捷键 1-9 → 数据集 Schema 的第 N 个启用标签
  const keyToLabel = (key: string): string | null => {
    const idx = Number(key) - 1
    return Number.isInteger(idx) && idx >= 0 && idx < schemaLabels.length ? schemaLabels[idx].key : null
  }
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (busy || !current) return
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      const label = keyToLabel(e.key)
      if (label) {
        setBusy(true)
        patch.mutate({ label }, { onSettled: () => setBusy(false) })
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, busy, schemaLabels])

  const d = dataset.data
  if (dataset.isLoading) return <Card loading />
  if (!d) return <Alert type="error" message="数据集不存在" />
  if (d.status !== 'DRAFT') {
    return (
      <Alert
        type="warning"
        showIcon
        message="该数据集已冻结（FROZEN），不可直接编辑"
        description="冻结保证可复现性。如需修正，可在详情页基于错误样本创建 DRAFT 新版本（错误回流）后再标注。"
        action={<Button onClick={() => navigate(`/datasets/${d.id}`)}>返回详情</Button>}
      />
    )
  }

  return (
    <div>
      <PageHeader
        title={`标注台 · ${d.name}`}
        subTitle={`共 ${d.sample_count} 样本，未标注 ${d.unlabeled_count}；快捷键 1-${Math.min(schemaLabels.length, 9)} 打标并自动跳下一条`}
        extra={<Button onClick={() => navigate(`/datasets/${d.id}`)}>返回详情</Button>}
      />
      <Space style={{ marginBottom: 12 }} wrap>
        <Checkbox
          checked={unlabeledOnly}
          onChange={(e) => {
            setUnlabeledOnly(e.target.checked)
            setPage(1)
            setCursor(0)
          }}
        >
          只看未标注
        </Checkbox>
        <Input.Search
          placeholder="过滤文本…"
          style={{ width: 240 }}
          allowClear
          onSearch={(v) => {
            setQ(v)
            setPage(1)
            setCursor(0)
          }}
        />
        <Typography.Text type="secondary">
          第 {page} 页 · 匹配 {samples.data?.total ?? 0} 条 · 当前第 {cursor + 1}/{list.length} 条
        </Typography.Text>
      </Space>

      <Card>
        {samples.isLoading ? (
          <Spin />
        ) : !current ? (
          <Alert type="success" showIcon message="没有更多样本了 🎉" description="可返回详情页校验并创建切分" />
        ) : (
          <>
            {current.context && (
              <div style={{ marginBottom: 8 }}>
                <Tag color="geekblue">context</Tag>
                <Typography.Text type="secondary">{current.context}</Typography.Text>
              </div>
            )}
            <Typography.Paragraph style={{ fontSize: 18 }}>{current.text}</Typography.Paragraph>
            {schemaQuery.isError ? (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 12 }}
                message="Schema 加载失败"
                description="无法取得该数据集绑定的标签定义；刷新重试，切勿凭记忆手工输入标签。"
              />
            ) : (
              <Space wrap style={{ marginBottom: 16 }}>
                {schemaLabels.map((l) => (
                  <Button
                    key={l.key}
                    type={current.label === l.key ? 'primary' : 'default'}
                    loading={busy && patch.variables?.label === l.key}
                    onClick={() => {
                      setBusy(true)
                      patch.mutate({ label: l.key }, { onSettled: () => setBusy(false) })
                    }}
                  >
                    {l.index + 1} · {l.name || l.key}
                    {l.effect_type ? <EffectTypeTag effect={l.effect_type} /> : null}
                  </Button>
                ))}
              </Space>
            )}
            <div>
              <Space>
                <Checkbox
                  checked={current.is_hard_negative}
                  onChange={(e) => patch.mutate({ is_hard_negative: e.target.checked })}
                >
                  难负例（hard negative）
                </Checkbox>
                {current.risk_slice && <Tag color="volcano">risk_slice: {current.risk_slice}</Tag>}
                {current.label && <LabelTag label={current.label} />}
                <Button size="small" onClick={() => setCursor((c) => Math.min(c + 1, list.length - 1))}>跳过</Button>
              </Space>
            </div>
            <Typography.Paragraph type="secondary" style={{ fontSize: 11, marginTop: 12 }}>
              sample_id {current.sample_id} · hash {current.normalized_hash.slice(0, 16)}…
              {current.group_id ? ` · group ${current.group_id}` : ''}
            </Typography.Paragraph>
          </>
        )}
      </Card>
      <div style={{ marginTop: 12 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          提示：所有修改即时保存；标注完成后在详情页「重新校验」确认无错误即可切分训练。
          {projectId ? '' : ''}
        </Typography.Text>
      </div>
    </div>
  )
}
