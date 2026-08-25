/** SSE 运行事件日志流：EventSource + Last-Event-ID 自动续传。 */
import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Tag, Typography } from 'antd'
import { sseUrl } from '../api/client'

export interface RunEventItem {
  sequence: number
  stage: string | null
  level: string
  message: string
  created_at: string
}

const LEVEL_COLOR: Record<string, string> = {
  info: 'default',
  warning: 'warning',
  error: 'error',
  success: 'success',
}

export function LogStream({ runId, active }: { runId: string; active: boolean }) {
  const [events, setEvents] = useState<RunEventItem[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lastSeqRef = useRef<number>(0)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!active) return
    const source = new EventSource(sseUrl(`/runs/${runId}/events`))
    source.onopen = () => {
      setConnected(true)
      setError(null)
    }
    source.onerror = () => {
      setConnected(false)
      // EventSource 会自动重连并携带 Last-Event-ID
    }
    // 后端 SSE 事件带 event: 名称（log/progress/metric/terminal），需按类型监听
    const handle = (raw: MessageEvent) => {
      try {
        const data = JSON.parse(raw.data) as { ts?: string; type?: string; payload?: Partial<RunEventItem> }
        // 事件体为 {ts, type, payload} 包装；统一展开为展示结构
        const p = (data.payload ?? {}) as RunEventItem & { level?: string; message?: string; stage?: string; percent?: number }
        const item: RunEventItem = {
          sequence: Number(raw.lastEventId) || 0,
          stage: p.stage ?? null,
          level: p.level ?? (data.type === 'metric' ? 'success' : 'info'),
          message:
            p.message ??
            (data.type === 'terminal'
              ? `终态：${(p as unknown as { status?: string }).status ?? ''}`
              : data.type === 'metric'
                ? `指标：${JSON.stringify(p).slice(0, 160)}`
                : JSON.stringify(p).slice(0, 160)),
          created_at: data.ts ?? new Date().toISOString(),
        }
        if (item.sequence <= lastSeqRef.current) return
        lastSeqRef.current = item.sequence
        setEvents((prev) => [...prev, item])
      } catch {
        setError('事件解析失败')
      }
    }
    for (const t of ['log', 'progress', 'metric', 'terminal']) {
      source.addEventListener(t, handle as EventListener)
    }
    source.onmessage = handle as unknown as (ev: MessageEvent) => void
    return () => {
      source.close()
      setConnected(false)
    }
  }, [runId, active])

  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight })
  }, [events.length])

  return (
    <div>
      <div style={{ marginBottom: 8, display: 'flex', justifyContent: 'space-between' }}>
        <span>
          {active ? (
            connected ? (
              <Tag color="processing">SSE 已连接</Tag>
            ) : (
              <Tag color="warning">连接中断，自动重连中…</Tag>
            )
          ) : (
            <Tag>已结束</Tag>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            共 {events.length} 条事件（断线自动按 Last-Event-ID 续传）
          </Typography.Text>
        </span>
        <Button size="small" onClick={() => setEvents([])}>
          清空
        </Button>
      </div>
      {error && <Alert type="error" message={error} style={{ marginBottom: 8 }} />}
      <div
        ref={boxRef}
        style={{
          background: '#0d1117',
          color: '#c9d1d9',
          padding: 12,
          borderRadius: 6,
          maxHeight: 420,
          overflow: 'auto',
          fontFamily: 'Menlo, Consolas, monospace',
          fontSize: 12,
          lineHeight: 1.7,
        }}
      >
        {events.length === 0 && <span style={{ color: '#8b949e' }}>等待事件…</span>}
        {events.map((e) => (
          <div key={e.sequence}>
            <span style={{ color: '#8b949e' }}>
              [{new Date(e.created_at).toLocaleTimeString()}] #{e.sequence}
            </span>{' '}
            {e.stage && <span style={{ color: '#79c0ff' }}>{e.stage}</span>}{' '}
            <span style={{ color: e.level === 'error' ? '#ff7b72' : e.level === 'warning' ? '#e3b341' : '#c9d1d9' }}>
              {e.message}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
