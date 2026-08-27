import { LABELS } from '../types'

/** 与标签映射下拉框保持一致：合法的五分类原始值默认映射到自身。 */
export function resolveLabelMapping(rawLabel: string, mapping: Record<string, string>): string {
  return mapping[rawLabel] ?? (LABELS.includes(rawLabel as (typeof LABELS)[number]) ? rawLabel : '')
}

/** 所有预览标签都有合法目标或被显式跳过时，允许执行导入。 */
export function isLabelMappingComplete(
  rawLabels: string[],
  mapping: Record<string, string>,
): boolean {
  return rawLabels.every((rawLabel) => {
    const target = resolveLabelMapping(rawLabel, mapping)
    return target === '__skip__' || LABELS.includes(target as (typeof LABELS)[number])
  })
}
