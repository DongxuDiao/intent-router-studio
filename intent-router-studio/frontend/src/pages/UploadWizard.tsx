/** 导入向导：上传 → 预览 → 列映射 → 标签映射 → 导入。 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Steps, Table, Typography, Upload as AntUpload, message } from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, uploadFile } from '../api/client'
import { LabelTag, PageHeader } from '../components/common'
import { useProject } from '../store/project'
import { LABELS, LABEL_NAMES } from '../types'
import type { DatasetVersion, PreviewData, Upload as UploadInfo } from '../types'
import { fmtBytes } from '../utils/format'
import { isLabelMappingComplete, resolveLabelMapping } from '../utils/importMapping'

const TARGET_FIELDS = [
  { key: 'text', label: 'text *', required: true },
  { key: 'label', label: 'label', required: false },
  { key: 'context', label: 'context', required: false },
  { key: 'group_id', label: 'group_id', required: false },
  { key: 'source', label: 'source', required: false },
  { key: 'risk_slice', label: 'risk_slice', required: false },
  { key: 'is_hard_negative', label: 'is_hard_negative', required: false },
]

export default function UploadWizard() {
  const { projectId } = useProject()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [upload, setUpload] = useState<UploadInfo | null>(null)
  const [preview, setPreview] = useState<PreviewData | null>(null)
  const [mode, setMode] = useState<'prelabeled' | 'unlabeled' | 'single_label'>('prelabeled')
  const [columns, setColumns] = useState<Record<string, string | null>>({})
  const [labelMapping, setLabelMapping] = useState<Record<string, string>>({})
  const [defaultLabel, setDefaultLabel] = useState<string>('information')
  const [datasetName, setDatasetName] = useState('')
  const [mappingError, setMappingError] = useState<string | null>(null)

  const distinctLabels = useMemo(() => {
    if (!preview) return []
    const col = columns.label
    if (!col) return []
    const set = new Set<string>()
    for (const row of preview.rows.slice(0, 100)) {
      const v = row[col]
      if (v) set.add(v)
    }
    return [...set].sort()
  }, [preview, columns.label])

  const doUpload = useMutation({
    mutationFn: (file: File) => uploadFile<UploadInfo>(`/projects/${projectId}/uploads`, file),
    onSuccess: async (u) => {
      setUpload(u)
      message.success(`上传成功 ${fmtBytes(u.size_bytes)}，正在预览…`)
      try {
        const p = await api<PreviewData>(`/uploads/${u.id}/preview`)
        setPreview(p)
        setColumns({
          text: p.suggested_columns.text ?? null,
          label: mode === 'unlabeled' ? null : p.suggested_columns.label ?? null,
        })
        setDatasetName(p.original_name.replace(/\.[^.]+$/, ''))
        setStep(1)
      } catch (e) {
        message.error(e instanceof Error ? e.message : '预览失败')
      }
    },
    onError: (e) => message.error(e instanceof Error ? e.message : '上传失败'),
  })

  const doImport = useMutation({
    mutationFn: () =>
      api<DatasetVersion>(`/uploads/${upload!.id}/import`, {
        method: 'POST',
        body: JSON.stringify({
          mode,
          columns,
          label_mapping: labelMapping,
          default_label: mode === 'single_label' ? defaultLabel : null,
          name: datasetName || null,
        }),
      }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ['datasets', projectId] })
      const errs = d.quality_report?.errors ?? []
      if (errs.length > 0) {
        message.warning(`导入完成，但存在 ${errs.length} 个错误（不可发起训练），进入详情查看`)
      } else {
        message.success('导入成功')
      }
      navigate(`/datasets/${d.id}`)
    },
    onError: (e) => {
      if (e instanceof ApiError) message.error(`${e.code}: ${e.message}`)
      else message.error('导入失败')
    },
  })

  const applyMode = (m: typeof mode) => {
    setMode(m)
    if (m === 'unlabeled') setColumns((c) => ({ ...c, label: null }))
    else if (preview) setColumns((c) => ({ ...c, label: c.label ?? preview.suggested_columns.label ?? null }))
  }

  const canImport = !!preview && !!columns.text && (mode !== 'single_label' || defaultLabel) &&
    isLabelMappingComplete(distinctLabels, labelMapping)

  if (!projectId) return <Alert type="info" showIcon message="请先选择项目" />

  return (
    <div>
      <PageHeader title="导入数据" subTitle="支持 CSV / JSONL / XLSX / TXT，≤100MB；服务端解码（UTF-8/GBK 自动探测）" />
      <Steps
        current={step}
        items={[{ title: '上传' }, { title: '预览与列映射' }, { title: '标签映射' }, { title: '导入' }]}
        style={{ marginBottom: 24 }}
      />

      {step === 0 && (
        <Card>
          <AntUpload.Dragger
            accept=".csv,.jsonl,.xlsx,.txt"
            showUploadList={false}
            maxCount={1}
            beforeUpload={(file) => {
              doUpload.mutate(file)
              return false
            }}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">点击或拖拽文件到此处</p>
            <p className="ant-upload-hint">
              列建议：text/label/context/group_id/source/risk_slice/is_hard_negative（中英文列名会自动识别）
            </p>
          </AntUpload.Dragger>
          {doUpload.isPending && <Typography.Text type="secondary">上传中…</Typography.Text>}
        </Card>
      )}

      {step === 1 && preview && (
        <Card
          title={`预览：${preview.original_name}（${preview.row_count} 行，编码 ${preview.used_encoding}）`}
          extra={<Button onClick={() => setStep(2)} disabled={!columns.text} type="primary">下一步</Button>}
        >
          {!columns.text && <Alert type="warning" showIcon message="必须为 text 指定一个列" style={{ marginBottom: 12 }} />}
          <Table
            size="small"
            pagination={{ pageSize: 8 }}
            dataSource={preview.rows.map((r, i) => ({ key: i, ...r }))}
            scroll={{ x: 'max-content' }}
            columns={preview.columns.map((c) => ({
              title: c,
              dataIndex: c,
              ellipsis: true,
              width: Math.max(100, Math.min(260, c.length * 14)),
            }))}
          />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginTop: 16 }}>
            {TARGET_FIELDS.map((f) => (
              <div key={f.key}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {f.label}
                </Typography.Text>
                <div>
                  <select
                    value={columns[f.key] ?? ''}
                    onChange={(e) => setColumns((c) => ({ ...c, [f.key]: e.target.value || null }))}
                    style={{ width: 160, padding: '4px 8px' }}
                  >
                    <option value="">（不映射）</option>
                    {preview.columns.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {step === 2 && preview && (
        <Card
          title="导入模式与标签映射"
          extra={
            <span>
              <Button style={{ marginRight: 8 }} onClick={() => setStep(1)}>上一步</Button>
              <Button type="primary" disabled={!canImport} loading={doImport.isPending} onClick={() => doImport.mutate()}>
                执行导入
              </Button>
            </span>
          }
        >
          <div style={{ marginBottom: 16, display: 'flex', gap: 24, alignItems: 'center' }}>
            <span>
              <Typography.Text type="secondary">模式：</Typography.Text>
              <select value={mode} onChange={(e) => applyMode(e.target.value as typeof mode)} style={{ padding: '4px 8px' }}>
                <option value="prelabeled">prelabeled（已有标签列）</option>
                <option value="unlabeled">unlabeled（无标签，稍后在标注台标注）</option>
                <option value="single_label">single_label（整批同标签）</option>
              </select>
            </span>
            {mode === 'single_label' && (
              <span>
                <Typography.Text type="secondary">统一标签：</Typography.Text>
                <select value={defaultLabel} onChange={(e) => setDefaultLabel(e.target.value)} style={{ padding: '4px 8px' }}>
                  {LABELS.map((l) => (
                    <option key={l} value={l}>{l} · {LABEL_NAMES[l]}</option>
                  ))}
                </select>
              </span>
            )}
            <span>
              <Typography.Text type="secondary">数据集名称：</Typography.Text>
              <input value={datasetName} onChange={(e) => setDatasetName(e.target.value)} style={{ padding: '4px 8px', width: 220 }} />
            </span>
          </div>

          {mode === 'prelabeled' && (
            <>
              {mappingError && <Alert type="error" showIcon message={mappingError} style={{ marginBottom: 12 }} />}
              <Table
                size="small"
                pagination={false}
                dataSource={distinctLabels.map((l) => ({ key: l, raw: l }))}
                columns={[
                  { title: '原始标签值', dataIndex: 'raw' },
                  {
                    title: '映射到',
                    width: 220,
                    render: (_, r: { raw: string }) => (
                      <select
                        value={resolveLabelMapping(r.raw, labelMapping)}
                        onChange={(e) => {
                          setLabelMapping((m) => ({ ...m, [r.raw]: e.target.value }))
                          setMappingError(null)
                        }}
                        style={{ padding: '4px 8px', width: 200 }}
                      >
                        <option value="">（未映射 → 将报 INVALID_LABEL 错误）</option>
                        {LABELS.map((l) => (
                          <option key={l} value={l}>{l} · {LABEL_NAMES[l]}</option>
                        ))}
                        <option value="__skip__">__skip__（跳过该行）</option>
                      </select>
                    ),
                  },
                  {
                    title: '预览',
                    width: 110,
                    render: (_, r: { raw: string }) => {
                      const v = resolveLabelMapping(r.raw, labelMapping)
                      return v === '__skip__' ? <span>跳过</span> : v ? <LabelTag label={v} /> : <Typography.Text type="danger">未映射</Typography.Text>
                    },
                  },
                ]}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                非法标签将作为数据错误记录（不阻断导入，但会阻断训练）；同一文本冲突标签将直接报错。
              </Typography.Text>
            </>
          )}
          {mode !== 'prelabeled' && (
            <Alert
              type="info"
              showIcon
              message={
                mode === 'unlabeled'
                  ? '将以 DRAFT 草稿导入，可进入「标注台」打标后再冻结；未标注样本会阻断切分与训练'
                  : `全部 ${preview.row_count} 行将标注为 ${defaultLabel}`
              }
            />
          )}
        </Card>
      )}
    </div>
  )
}
