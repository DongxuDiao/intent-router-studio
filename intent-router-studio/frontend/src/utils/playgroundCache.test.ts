import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearAllPlaygroundCaches,
  clearPlaygroundCache,
  patchPlaygroundCache,
  patchRewriteCache,
  playgroundCacheKey,
  readPlaygroundCache,
  setPlaygroundCacheEnabled,
} from './playgroundCache'

describe('Playground local cache', () => {
  beforeEach(() => {
    localStorage.clear()
    setPlaygroundCacheEnabled(true)
  })

  it('isolates snapshots by project and merges patches', () => {
    patchPlaygroundCache('p1', { text: 'query-1', tab: 'single' })
    patchPlaygroundCache('p1', { context: 'context-1' })
    patchPlaygroundCache('p2', { text: 'query-2' })
    expect(readPlaygroundCache('p1')).toMatchObject({ text: 'query-1', context: 'context-1' })
    expect(readPlaygroundCache('p2')).toMatchObject({ text: 'query-2' })
  })

  it('merges rewrite input and result independently', () => {
    patchRewriteCache('p1', { text: '这个怎么停？', mode: 'shadow' })
    patchRewriteCache('p1', { context: '实验 123' })
    expect(readPlaygroundCache('p1')?.rewrite).toMatchObject({
      text: '这个怎么停？', context: '实验 123', mode: 'shadow',
    })
  })

  it('ignores malformed and incompatible cache', () => {
    localStorage.setItem(playgroundCacheKey('bad'), '{')
    localStorage.setItem(playgroundCacheKey('old'), JSON.stringify({ version: 1, text: 'old' }))
    expect(readPlaygroundCache('bad')).toBeNull()
    expect(readPlaygroundCache('old')).toBeNull()
  })

  it('expires snapshots after 24 hours and supports an explicit opt-out', () => {
    localStorage.setItem(playgroundCacheKey('expired'), JSON.stringify({
      version: 2, expiresAt: Date.now() - 1, text: 'sensitive',
    }))
    expect(readPlaygroundCache('expired')).toBeNull()
    expect(localStorage.getItem(playgroundCacheKey('expired'))).toBeNull()

    patchPlaygroundCache('p2', { text: 'clear-on-opt-out' })
    setPlaygroundCacheEnabled(false)
    clearAllPlaygroundCaches()
    expect(localStorage.getItem(playgroundCacheKey('p2'))).toBeNull()
    patchPlaygroundCache('p1', { text: 'do-not-store' })
    expect(readPlaygroundCache('p1')).toBeNull()
    expect(localStorage.getItem(playgroundCacheKey('p1'))).toBeNull()
  })

  it('clears only the deleted project snapshot', () => {
    patchPlaygroundCache('p1', { text: 'remove-me' })
    patchPlaygroundCache('p2', { text: 'keep-me' })
    clearPlaygroundCache('p1')
    expect(readPlaygroundCache('p1')).toBeNull()
    expect(readPlaygroundCache('p2')?.text).toBe('keep-me')
  })
})
