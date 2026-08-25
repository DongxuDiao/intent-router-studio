/** 展示格式化工具。 */
import dayjs from 'dayjs'

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  return dayjs(iso).format('MM-DD HH:mm:ss')
}

export function fmtPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '-'
  return `${(value * 100).toFixed(digits)}%`
}

export function fmtNumber(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined) return '-'
  return Number(value).toFixed(digits)
}

export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-'
  return `${Number(value).toFixed(1)}ms`
}

export function fmtBytes(bytes: number | undefined): string {
  if (!bytes) return '-'
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 ** 2).toFixed(1)}MB`
}

/** CSV 单元格防公式注入：以 = + - @ 开头的单元格加 ' 前缀。 */
export function escapeCsvCell(value: unknown): string {
  const s = value === null || value === undefined ? '' : String(value)
  if (/^[=+\-@]/.test(s)) {
    return `'${s}`
  }
  return s
}

export function toCsv(rows: Record<string, unknown>[], columns?: string[]): string {
  if (rows.length === 0) return ''
  const cols = columns ?? Object.keys(rows[0])
  const head = cols.join(',')
  const body = rows
    .map((row) => cols.map((c) => `"${escapeCsvCell(row[c]).replace(/"/g, '""')}"`).join(','))
    .join('\n')
  return `${head}\n${body}`
}

export function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([`﻿${content}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
