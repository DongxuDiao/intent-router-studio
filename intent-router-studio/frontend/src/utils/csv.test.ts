import { describe, expect, it } from 'vitest'
import { escapeCsvCell, toCsv } from './format'

describe('escapeCsvCell（CSV 公式注入防护）', () => {
  it('对 = + - @ 开头的单元格加单引号前缀', () => {
    expect(escapeCsvCell('=SUM(A1:A2)')).toBe("'=SUM(A1:A2)")
    expect(escapeCell('+cmd')).toBe("'+cmd")
    expect(escapeCsvCell('-1+1')).toBe("'-1+1")
    expect(escapeCsvCell('@import')).toBe("'@import")
  })

  it('普通文本不变', () => {
    expect(escapeCsvCell('查看我的任务')).toBe('查看我的任务')
    expect(escapeCsvCell('123')).toBe('123')
    expect(escapeCsvCell('')).toBe('')
    expect(escapeCsvCell(null)).toBe('')
  })

  it('负数数字字符串也会被转义（保守策略）', () => {
    expect(escapeCsvCell('-0.5')).toBe("'-0.5")
  })
})

function escapeCell(s: string): string {
  return escapeCsvCell(s)
}

describe('toCsv', () => {
  it('生成带引号的 CSV 且应用转义', () => {
    const csv = toCsv(
      [
        { text: '删除任务', label: 'write_action', margin: 0.2 },
        { text: '=HYPERLINK("x")', label: 'oos', margin: -1 },
      ],
      ['text', 'label', 'margin'],
    )
    const lines = csv.split('\n')
    expect(lines[0]).toBe('text,label,margin')
    expect(lines[1]).toBe('"删除任务","write_action","0.2"')
    expect(lines[2]).toBe('"\'=HYPERLINK(""x"")","oos","\'-1"')
  })

  it('引号被双写转义', () => {
    const csv = toCsv([{ a: 'he said "hi"' }])
    expect(csv).toBe('a\n"he said ""hi"""')
  })
})
