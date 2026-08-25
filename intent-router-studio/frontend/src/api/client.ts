/// <reference types="vite/client" />
/** API 客户端：统一错误结构解析。 */
import type { ApiErrorEnvelope } from '../types'

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

export class ApiError extends Error {
  code: string
  details: Record<string, unknown>
  requestId: string
  status: number

  constructor(envelope: ApiErrorEnvelope, status: number) {
    super(envelope.error?.message ?? '请求失败')
    this.code = envelope.error?.code ?? 'UNKNOWN'
    this.details = (envelope.error?.details ?? {}) as Record<string, unknown>
    this.requestId = envelope.error?.request_id ?? '-'
    this.status = status
  }
}

export async function api<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!response.ok) {
    throw new ApiError((data as ApiErrorEnvelope) ?? { error: { code: 'HTTP_ERROR', message: text, request_id: '-' } }, response.status)
  }
  return data as T
}

export function uploadFile<T = unknown>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  return api<T>(path, { method: 'POST', body: form })
}

export function sseUrl(path: string): string {
  return `${BASE}${path}`
}
