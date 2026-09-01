import { LABELS } from '../types'

/** 与标签映射下拉框保持一致：目标是当前 Schema 激活标签的原始值默认映射到自身
 * （自定义意图标签 §7.2：合法集合由数据集/项目 Schema 决定，不再是全局五分类）。 */
export function resolveLabelMapping(rawLabel: string, mapping: Record<string, string>, validLabels: readonly string[] = LABELS): string {
  return mapping[rawLabel] ?? (validLabels.includes(rawLabel) ? rawLabel : '')
}

/** 所有预览标签都有合法目标或被显式跳过时，允许执行导入。 */
export function isLabelMappingComplete(
  rawLabels: string[],
  mapping: Record<string, string>,
  validLabels: readonly string[] = LABELS,
): boolean {
  return rawLabels.every((rawLabel) => {
    const target = resolveLabelMapping(rawLabel, mapping, validLabels)
    return target === '__skip__' || validLabels.includes(target)
  })
}
