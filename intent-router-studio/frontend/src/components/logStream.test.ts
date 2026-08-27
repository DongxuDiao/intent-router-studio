import { describe, expect, it } from 'vitest'
import { parseRunSseEvent } from './LogStream'

describe('parseRunSseEvent', () => {
  it('解析后端当前的直接 payload', () => {
    const event = parseRunSseEvent(
      JSON.stringify({ level: 'INFO', message: '开始训练', stage: 'TRAINING_EMBEDDING' }),
      '6',
      'progress',
      '2026-08-27T00:00:00Z',
    )
    expect(event).toMatchObject({ sequence: 6, stage: 'TRAINING_EMBEDDING', message: '开始训练', level: 'INFO' })
  })

  it('兼容旧的包装格式', () => {
    const event = parseRunSseEvent(
      JSON.stringify({ ts: '2026-08-27T00:00:01Z', type: 'terminal', payload: { status: 'FAILED' } }),
      '7',
      'message',
    )
    expect(event).toMatchObject({ sequence: 7, message: '终态：FAILED', created_at: '2026-08-27T00:00:01Z' })
  })
})
