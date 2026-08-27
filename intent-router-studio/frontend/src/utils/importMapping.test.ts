import { describe, expect, it } from 'vitest'
import { isLabelMappingComplete, resolveLabelMapping } from './importMapping'

describe('标签导入映射', () => {
  const validLabels = ['information', 'read_only', 'write_action', 'unclear', 'oos']

  it('合法原始标签无需手动操作下拉框即可视为已映射', () => {
    expect(isLabelMappingComplete(validLabels, {})).toBe(true)
    expect(resolveLabelMapping('information', {})).toBe('information')
  })

  it('自定义原始标签仍需显式映射或跳过', () => {
    expect(isLabelMappingComplete(['知识问答'], {})).toBe(false)
    expect(isLabelMappingComplete(['知识问答'], { 知识问答: 'information' })).toBe(true)
    expect(isLabelMappingComplete(['知识问答'], { 知识问答: '__skip__' })).toBe(true)
  })

  it('非法目标标签不能启用导入', () => {
    expect(isLabelMappingComplete(['知识问答'], { 知识问答: 'not-a-label' })).toBe(false)
  })
})
