import type { PredictResult, QueryUnderstanding, RewriteMode } from '../types'

export type PlaygroundTab = 'single' | 'batch' | 'ab' | 'rewrite'

export interface PlaygroundSnapshot {
  version: 2
  expiresAt: number
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

const PREFIX = 'irs.playground.v2.'
const LEGACY_PREFIX = 'irs.playground.v1.'
const ENABLED_KEY = 'irs.playground.cacheEnabled'
const TTL_MS = 24 * 60 * 60 * 1000

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

export function isPlaygroundCacheEnabled(): boolean {
  try {
    return storage()?.getItem(ENABLED_KEY) !== 'false'
  } catch {
    return true
  }
}

export function setPlaygroundCacheEnabled(enabled: boolean): void {
  try {
    storage()?.setItem(ENABLED_KEY, String(enabled))
  } catch {
    // localStorage 不可用时保持页面主流程。
  }
}

export function readPlaygroundCache(projectId: string | null): PlaygroundSnapshot | null {
  if (!projectId || !isPlaygroundCacheEnabled()) return null
  try {
    const raw = storage()?.getItem(playgroundCacheKey(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as PlaygroundSnapshot
    if (parsed?.version !== 2 || !Number.isFinite(parsed.expiresAt) || parsed.expiresAt <= Date.now()) {
      storage()?.removeItem(playgroundCacheKey(projectId))
      return null
    }
    return parsed
  } catch {
    return null
  }
}

export function patchPlaygroundCache(projectId: string | null, patch: Partial<PlaygroundSnapshot>): void {
  if (!projectId || !isPlaygroundCacheEnabled()) return
  try {
    const current = readPlaygroundCache(projectId) ?? { version: 2 as const, expiresAt: Date.now() + TTL_MS }
    storage()?.setItem(playgroundCacheKey(projectId), JSON.stringify({
      ...current,
      ...patch,
      version: 2,
      expiresAt: Date.now() + TTL_MS,
    }))
  } catch {
    // localStorage 可能被禁用或容量不足；缓存失败不能影响 Playground 主流程。
  }
}

export function clearPlaygroundCache(projectId: string | null): void {
  if (!projectId) return
  try {
    storage()?.removeItem(playgroundCacheKey(projectId))
    storage()?.removeItem(`${LEGACY_PREFIX}${projectId}`)
  } catch {
    // localStorage 不可用时无需阻断项目删除。
  }
}

export function clearAllPlaygroundCaches(): void {
  try {
    const target = storage()
    if (!target) return
    const keys: string[] = []
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index)
      if (key?.startsWith(PREFIX) || key?.startsWith(LEGACY_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => target.removeItem(key))
  } catch {
    // localStorage 不可用时无需阻断 Playground。
  }
}

export function patchRewriteCache(
  projectId: string,
  patch: NonNullable<PlaygroundSnapshot['rewrite']>,
): void {
  const current = readPlaygroundCache(projectId)
  patchPlaygroundCache(projectId, { rewrite: { ...(current?.rewrite ?? {}), ...patch } })
}
