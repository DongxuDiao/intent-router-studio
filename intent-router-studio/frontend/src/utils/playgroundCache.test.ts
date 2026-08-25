import { beforeEach, describe, expect, it } from 'vitest'
import { patchPlaygroundCache, patchRewriteCache, playgroundCacheKey, readPlaygroundCache } from './playgroundCache'

describe('Playground local cache', () => {
  beforeEach(() => localStorage.clear())

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
    localStorage.setItem(playgroundCacheKey('old'), JSON.stringify({ version: 0, text: 'old' }))
    expect(readPlaygroundCache('bad')).toBeNull()
    expect(readPlaygroundCache('old')).toBeNull()
  })
})
