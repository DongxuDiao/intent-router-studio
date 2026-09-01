/** 标签 Schema 获取 hooks（自定义意图标签 §7.2/§7.3）：
 * 展示用的标签集合（key/名称/效果）一律来自 Schema 接口或模型响应，
 * 不再使用前端全局固定五分类常量。 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { LabelDefinitionInput, LabelSchemaDetail } from '../types'

export interface SchemaLabel extends LabelDefinitionInput {
  /** Schema 内顺序（分类头顺序） */
  index: number
}

/** Schema detail 的 document.labels → 启用标签列表（含顺序） */
export function activeSchemaLabels(detail: LabelSchemaDetail | undefined | null): SchemaLabel[] {
  const labels = detail?.document?.labels ?? []
  return labels
    .filter((d) => (d.status ?? 'active') === 'active')
    .map((d, index) => ({ ...d, index }))
}

/** 按 schema_id 取详情（历史数据集按其绑定的版本展示，不跟随项目最新） */
export function useSchemaDetail(projectId: string | null | undefined, schemaId: string | null | undefined) {
  return useQuery({
    queryKey: ['label-schema', projectId, schemaId],
    enabled: !!projectId && !!schemaId,
    queryFn: () => api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/${schemaId}`),
  })
}

/** 取项目当前生效 Schema（导入向导的标签映射目标集合） */
export function useActiveSchema(projectId: string | null | undefined) {
  return useQuery({
    queryKey: ['label-schema-active', projectId],
    enabled: !!projectId,
    queryFn: () => api<LabelSchemaDetail>(`/projects/${projectId}/label-schemas/active`),
  })
}
