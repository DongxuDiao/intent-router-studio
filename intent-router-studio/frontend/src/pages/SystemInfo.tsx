/** 系统信息：健康、环境、配置、清理。 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Popconfirm, Space, Typography, message } from 'antd'
import { api } from '../api/client'
import { PageHeader } from '../components/common'
import type { SystemInfo } from '../types'

export default function SystemInfo() {
  const info = useQuery({
    queryKey: ['system-info'],
    queryFn: () => api<SystemInfo>('/system/info'),
    refetchInterval: 15000,
  })
  const config = useQuery({
    queryKey: ['system-config'],
    queryFn: () => api<Record<string, unknown>>('/system/config'),
  })
  const cleanup = useMutation({
    mutationFn: (target: string) => api('/system/cleanup', { method: 'POST', body: JSON.stringify({ target }) }),
    onSuccess: (d) => message.success(`清理完成：${JSON.stringify(d)}`),
    onError: (e) => message.error(e instanceof Error ? e.message : '清理失败'),
  })

  const d = info.data

  return (
    <div>
      <PageHeader
        title="系统信息"
        subTitle="本地优先部署；日志不记录 query 原文（LOG_RAW_TEXT=false）"
        extra={
          <Space>
            <Popconfirm title="清理由上传产生的临时文件？" onConfirm={() => cleanup.mutate('uploads_tmp')}>
              <Button>清理上传临时文件</Button>
            </Popconfirm>
          </Space>
        }
      />
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type={d ? 'success' : 'info'}
          showIcon
          message={d ? '服务健康' : '加载中…'}
          description={
            d
              ? `Python ${d.python} · ${d.platform} · CPU ${d.cpu_count} 核 · 内存可用 ${Math.round(d.memory_available_mb)}MB / ${Math.round(d.memory_total_mb)}MB · 制品盘剩余 ${d.artifact_root_free_gb.toFixed(1)}GB`
              : ''
          }
        />
        <Card title="运行环境" size="small">
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="torch">{String(d?.torch ?? '-')}</Descriptions.Item>
            <Descriptions.Item label="CUDA 可用">{String(d?.cuda_available ?? false)}</Descriptions.Item>
            <Descriptions.Item label="MPS 可用">{String(d?.mps_available ?? false)}</Descriptions.Item>
            <Descriptions.Item label="设备说明">Docker 内为 CPU；Mac 原生训练可用 MPS</Descriptions.Item>
          </Descriptions>
        </Card>
        <Card title="服务配置（脱敏）" size="small">
          <pre style={{ fontSize: 12, maxHeight: 400, overflow: 'auto' }}>{JSON.stringify(config.data, null, 2)}</pre>
        </Card>
        <Card title="安全要点" size="small">
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 2 }}>
            <li>write_action 路由仅输出 external_write_candidate 上限，永不直接执行外部写入</li>
            <li>不可变数据集（FROZEN）+ 制品 manifest 哈希校验，训练可复现可审计</li>
            <li>阈值保存 / 模型激活受安全约束（false_write_rate ≤ 0.005，write_precision ≥ 0.95）</li>
            <li>默认只绑定 127.0.0.1；不加载 trust_remote_code；不接受用户 pickle；CSV 导出防公式注入</li>
          </ul>
        </Card>
      </Space>
    </div>
  )
}
