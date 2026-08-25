import type { PredictResult, QueryUnderstanding, RewriteMode } from '../types'

export type PlaygroundTab = 'single' | 'batch' | 'ab' | 'rewrite'

export interface PlaygroundSnapshot {
  version: 1
  tab?: PlaygroundTab
  text?: string
  context?: string
  expected?: string | null
  saveText?: boolean
  debug?: boolean
  batchText?: string
  modelB?: string | null
  singleResult?: PredictResult | null
  batchResult?: { count: number; results: PredictResult[] } | null
  compareResult?: { a: PredictResult; b: PredictResult } | null
  rewrite?: {
    text?: string
    context?: string
    mode?: RewriteMode | 'project_default'
    understanding?: QueryUnderstanding | null
    edited?: string
    rejectReasons?: string[]
    saveText?: boolean
  }
}

const PREFIX = 'irs.playground.v1.'

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

export function playgroundCacheKey(projectId: string): string {
  return `${PREFIX}${projectId}`
}

export function readPlaygroundCache(projectId: string | null): PlaygroundSnapshot | null {
  if (!projectId) return null
  try {
    const raw = storage()?.getItem(playgroundCacheKey(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as PlaygroundSnapshot
    return parsed?.version === 1 ? parsed : null
  } catch {
    return null
  }
}

export function patchPlaygroundCache(projectId: string | null, patch: Partial<PlaygroundSnapshot>): void {
  if (!projectId) return
  try {
    const current = readPlaygroundCache(projectId) ?? { version: 1 as const }
    storage()?.setItem(playgroundCacheKey(projectId), JSON.stringify({ ...current, ...patch, version: 1 }))
  } catch {
    // localStorage 可能被禁用或容量不足；缓存失败不能影响 Playground 主流程。
  }
}

export function patchRewriteCache(
  projectId: string,
  patch: NonNullable<PlaygroundSnapshot['rewrite']>,
): void {
  const current = readPlaygroundCache(projectId)
  patchPlaygroundCache(projectId, { rewrite: { ...(current?.rewrite ?? {}), ...patch } })
}
