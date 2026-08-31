/** 与后端契约对应的类型定义。 */

export const LABELS = ['information', 'read_only', 'write_action', 'unclear', 'oos'] as const
export type Label = (typeof LABELS)[number]

export const LABEL_NAMES: Record<string, string> = {
  information: '了解信息',
  read_only: '查询状态',
  write_action: '修改状态',
  unclear: '表达不清',
  oos: '超出范围',
}

export const LABEL_COLORS: Record<string, string> = {
  information: 'blue',
  read_only: 'cyan',
  write_action: 'orange',
  unclear: 'purple',
  oos: 'default',
}

export const EFFECT_CEILING_NAMES: Record<string, string> = {
  none: '无副作用',
  read_only: '只读',
  external_write_candidate: '写候选（不得自动执行）',
}

export const GATE_NAMES: Record<string, string> = {
  answer_or_kb: '回答或知识库',
  readonly_skill_match: '只读 Skill 召回',
  skill_match_and_confirmation: 'Skill 匹配 + 用户确认',
  clarification: '澄清或安全回答',
  capability_boundary: '能力边界提示',
  skill_reselection: 'Skill 重选',
}

export interface ApiErrorEnvelope {
  error: { code: string; message: string; details?: Record<string, unknown>; request_id: string }
}

export interface Project {
  id: string
  name: string
  description: string
  active_model_id: string | null
  active_model_name: string | null
  dataset_count: number
  run_count: number
  created_at: string
}

export interface LabelSchema {
  schema_version: string
  labels: { key: string; name: string; definition: string; positive_example: string; negative_example: string }[]
  reserved_routes: string[]
}

export interface Upload {
  id: string
  original_name: string
  sha256: string
  size_bytes: number
  status: string
  created_at: string
}

export interface PreviewData {
  upload_id: string
  original_name: string
  columns: string[]
  row_count: number
  used_encoding: string
  rows: Record<string, string | null>[]
  suggested_columns: { text: string | null; label: string | null }
}

export interface QualityIssue {
  code: string
  message: string
  details?: Record<string, unknown>
}

export interface QualityReport {
  errors: QualityIssue[]
  warnings: QualityIssue[]
  stats: {
    rows: number
    labeled: number
    unlabeled: number
    label_distribution: Record<string, number>
    unique_hashes: number
    has_group_id: number
    hard_negative: number
    non_write_support: number
  }
  generated_at: string
}

export interface DatasetVersion {
  id: string
  project_id: string
  parent_id: string | null
  version: number
  name: string
  origin: string
  status: 'DRAFT' | 'FROZEN'
  sample_count: number
  labeled_count: number
  unlabeled_count: number
  label_distribution: Record<string, number>
  change_summary: string
  manifest: Record<string, unknown> | null
  quality_report: QualityReport | null
  latest_split_id: string | null
  created_at: string
}

export interface Sample {
  sample_id: string
  text: string
  label: string | null
  group_id: string | null
  context: string | null
  source: string | null
  is_hard_negative: boolean
  risk_slice: string | null
  normalized_hash: string
}

export interface SamplesPage {
  dataset_id: string
  total: number
  page: number
  page_size: number
  samples: Sample[]
}

export interface SplitInfo {
  id: string
  dataset_id: string
  seed: number
  algorithm: string
  ratios: Record<string, number>
  stats: {
    rows: Record<string, number>
    train_label_distribution?: Record<string, number>
    validation_label_distribution?: Record<string, number>
    test_label_distribution?: Record<string, number>
    risk_test_rows?: number
    warnings?: QualityIssue[]
  }
  created_at: string
}

export type RunStatus =
  | 'DRAFT' | 'QUEUED' | 'PREPARING' | 'TRAINING_EMBEDDING' | 'TRAINING_HEAD'
  | 'CALIBRATING' | 'SEARCHING_THRESHOLDS' | 'EVALUATING' | 'PACKAGING'
  | 'SUCCEEDED' | 'CANCELLING' | 'CANCELLED' | 'FAILED' | 'INTERRUPTED'

export const RUN_STAGES: RunStatus[] = [
  'PREPARING', 'TRAINING_EMBEDDING', 'TRAINING_HEAD', 'CALIBRATING',
  'SEARCHING_THRESHOLDS', 'EVALUATING', 'PACKAGING', 'SUCCEEDED',
]

export const STAGE_NAMES: Record<string, string> = {
  PREPARING: '准备数据',
  TRAINING_EMBEDDING: '嵌入微调',
  TRAINING_HEAD: '分类头',
  CALIBRATING: '温度校准',
  SEARCHING_THRESHOLDS: '阈值搜索',
  EVALUATING: '评估',
  PACKAGING: '制品打包',
  SUCCEEDED: '完成',
}

export interface RunConfig {
  train: {
    base_model_id: string
    seed: number
    device: string
    max_length: number
    batch_size: number
    num_epochs: number
    body_learning_rate: number
    sampling_strategy: string
    num_iterations: number
    max_embedding_pairs: number
    fine_tune_embeddings: boolean
  }
  threshold_search: Record<string, unknown>
}

export interface TrainingRun {
  id: string
  project_id: string
  dataset_id: string
  split_id: string | null
  name: string
  config: RunConfig
  status: RunStatus
  stage: string | null
  stage_index: number | null
  progress: number
  worker_id: string | null
  cancel_requested: boolean
  parent_run_id: string | null
  error: { code?: string; message?: string } | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface PerClassMetric {
  label: string
  precision: number
  recall: number
  f1: number
  support: number
}

export interface RouteMetrics {
  n: number
  accepted_count: number
  coverage: number | null
  safe_coverage: number | null
  selective_accuracy: number | null
  false_write_rate: number | null
  false_write_count: number
  write_precision: number | null
  write_recall: number | null
  unclear_rate: number | null
  route_counts: Record<string, number>
}

export interface SplitEval {
  classification: {
    accuracy: number | null
    macro_f1: number | null
    micro_f1: number | null
    weighted_f1: number | null
    per_class: PerClassMetric[]
    confusion_matrix: { labels: string[]; matrix: number[][] }
    support: number
  }
  routing: RouteMetrics
  false_write_confidence_interval: {
    false_write_count: number
    non_write_support: number
    rate: number | null
    wilson_95: [number | null, number | null] | null
    note: string
  }
  calibration: { nll: number; ece: number; brier: number }
}

export interface Thresholds {
  default_min_confidence: number
  write_min_confidence: number
  oos_min_confidence: number
  min_margin: number
}

export interface RunMetrics {
  status: string
  available: boolean
  run_id?: string
  label_order?: string[]
  thresholds?: Thresholds
  threshold_search?: {
    best: Thresholds
    feasible: boolean
    n_feasible: number
    n_candidates: number
    n_retained_candidates?: number
    n_tied?: number
    selection?: {
      n_tied: number
      n_unique_route_patterns: number
      criterion: string
      chosen_macro_f1: number
      chosen_conservatism: number
    }
    curves: Record<string, { value: number; coverage: number | null; safe_coverage: number | null; false_write_rate: number | null; unclear_rate: number | null; write_precision: number | null }[]>
    pareto: { thresholds: Thresholds; safe_coverage: number; false_write_rate: number }[]
  }
  calibration?: {
    temperature: number
    before: { nll: number; ece: number; brier: number }
    after: { nll: number; ece: number; brier: number }
    reliability_before?: { bin: number; confidence: number; accuracy: number; count: number }[]
    reliability_after?: { bin: number; confidence: number; accuracy: number; count: number }[]
  }
  validation?: SplitEval
  test?: SplitEval
  risk_test?: { support: number } | null
  slices?: Record<string, { support: number; macro_f1: number; accuracy: number; false_write_rate: number; false_write_count: number; coverage: number | null }>
  distributions?: {
    confidence: { edges: number[]; counts: number[] }
    margin: { edges: number[]; counts: number[] }
  }
  latency?: { p50: number | null; p95: number | null; p99: number | null; mean: number | null; n: number }
  environment?: Record<string, unknown>
}

export interface ErrorSample {
  sample_id: string
  text: string
  context: string | null
  true_label: string
  raw_prediction: string
  final_route: string
  decision: string
  margin: number
  top_k: { label: string; probability: number }[]
  reason_codes: string[]
  risk_slice: string | null
  source: string | null
  group_id: string | null
  split: string
}

export interface ModelVersion {
  id: string
  project_id: string
  run_id: string
  threshold_id: string | null
  name: string
  status: 'CANDIDATE' | 'VALIDATED' | 'ACTIVE' | 'ARCHIVED'
  manifest: Record<string, unknown> | null
  metrics_summary: { macro_f1?: number; false_write_rate?: number; safe_coverage?: number } | null
  created_at: string
  activated_at: string | null
}

export interface PredictResult {
  route: string
  decision: 'accept' | 'unclear'
  confidence: number
  margin: number
  top_k: { label: string; probability: number }[]
  reason_codes: string[]
  effect_ceiling: string
  required_next_gate: string
  latency_ms: number
  model_version: string
  model_version_id?: string
  cache_hit?: boolean
  debug?: {
    thresholds_applied: Thresholds
    temperature: number
    label_order: string[]
    threshold_version_id: string | null
  }
  request_id?: string
}

export interface ThresholdVersionInfo {
  id: string
  run_id: string
  version: number
  config: Thresholds
  metrics: RouteMetrics | null
  source: string
  created_at: string
}

export interface SimulationResult {
  thresholds: Thresholds
  metrics: RouteMetrics
  violations: { code: string; message: string }[]
  n: number
}

export interface SystemInfo {
  python: string
  platform: string
  cpu_count: number
  memory_total_mb: number
  memory_available_mb: number
  artifact_root_free_gb: number
  torch?: string
  cuda_available?: boolean
  mps_available?: boolean
  [key: string]: unknown
}

// ---- Query 改写（修改方案 §6 / §13）----

export const REWRITE_MODES = ['off', 'normalize_only', 'shadow', 'safe_apply'] as const
export type RewriteMode = (typeof REWRITE_MODES)[number]

export const REWRITE_MODE_NAMES: Record<string, string> = {
  off: '关闭（走现有稳定链路）',
  normalize_only: '仅术语归一（L0，无生成模型）',
  shadow: '影子模式（默认：双路评估，不替换下游）',
  safe_apply: '安全应用（安全门全绿才替换下游 Query）',
}

export const REWRITE_REASON_NAMES: Record<string, string> = {
  NO_REWRITE_NEEDED: '无需改写',
  NORMALIZED_TERM: '术语已归一',
  RESOLVED_PRONOUN: '指代已解析',
  COMPLETED_ELLIPSIS: '省略已补全',
  MISSING_CONTEXT: '缺少上下文',
  AMBIGUOUS_REFERENCE: '指代不明',
  UNSUPPORTED_ASSUMPTION: '引入未经支持的假设',
  NEGATION_CHANGED: '否定关系改变',
  MODALITY_CHANGED: '语气改变',
  ACTION_INTENSIFIED: '动作被强化',
  OBJECT_INVENTED: '对象被凭空创造',
  ROUTE_CONFLICT: '路由冲突',
  LOW_CONFIDENCE: '置信度不足',
  TIMEOUT: '生成超时',
  INVALID_JSON: '输出无法解析',
  PROVIDER_UNAVAILABLE: '改写服务不可用',
  REWRITER_BUSY: '生成队列已满，已回退原文',
}

export interface RewriteModelInfo {
  provider: string
  model_id: string
  prompt_version: string
}

export interface TermReplacement {
  rule_id: string
  source_term: string
  target_term: string
  source_span: [number, number]
}

export interface RewriteResult {
  original_query: string
  normalized_query: string
  standalone_query: string
  rewrite_type: string
  changed: boolean
  should_use: boolean
  confidence: number
  preserved_intent: boolean
  mentioned_action: string | null
  objects: { type: string; value: string; source: string; confidence: number }[]
  constraints: Record<string, unknown>
  missing_slots: string[]
  assumptions: string[]
  used_context_refs: string[]
  reason_codes: string[]
  model: RewriteModelInfo
  latency_ms: number
  term_replacements: TermReplacement[]
}

export interface SafetyCheck {
  name: string
  passed: boolean
  detail: string
}

export interface SafetyDecision {
  allow: boolean
  safety_decision: 'allow_rewrite' | 'blocked'
  reason_codes: string[]
  checks: SafetyCheck[]
  route_conflict: boolean
  escalation: boolean
  downgrade: boolean
  route_policy: { downstream_rewrite_allowed: boolean; formal_route: string; conflict: boolean; escalation: boolean; downgrade: boolean; note: string } | null
}

export interface QueryUnderstanding {
  available?: boolean
  mode: RewriteMode
  rewrite: RewriteResult
  original_route: PredictResult
  rewrite_route: PredictResult | null
  route_consistent: boolean
  downstream_query: string
  downstream_query_source: 'original' | 'rewrite'
  safety_decision: string
  safety: SafetyDecision | null
  fallback_reason: string | null
  final_route: string
  cache_hit?: boolean
  provider_trace?: ProviderTrace | null
}

export interface RewriteOptionsInput {
  enabled: boolean
  mode?: 'project_default' | RewriteMode
  include_trace?: boolean
}

export interface RewriteConfigPayload {
  mode: RewriteMode
  timeout_ms: number
  min_rewrite_confidence: number
  require_route_consistency: boolean
  fallback: string
  store_raw_text: boolean
  /** 外部模型 V1 §6.2：项目选择的改写模型连接（版本化，可回滚） */
  provider_connection_id?: string
}

/** V2 §4.3 方案A：生成模型参数由部署环境管理（rewriter REWRITE_* 环境变量），项目级只读 */
export interface RewriteDeploymentInfo {
  available: boolean
  provider?: string | null
  model_id?: string | null
  device?: string | null
  max_new_tokens?: number | null
  prompt_version?: string | null
  note: string
}

/** 外部模型 V1：改写模型连接（密钥只写不读，接口永不回传） */
export interface ProviderConnection {
  id: string
  name: string
  provider_type: 'local_qwen' | 'glm' | 'openai_compatible' | string
  base_url?: string | null
  /** GLM 端点档位：general=通用开放平台（按量计费）／coding=Coding Plan 专用（订阅额度） */
  glm_endpoint?: 'general' | 'coding' | null
  model_id: string | null
  api_key_hint?: string
  has_api_key?: boolean
  generation_config?: {
    temperature?: number
    max_tokens?: number
    thinking?: boolean
    json_mode?: boolean
  } & Record<string, unknown>
  revision?: number
  enabled: boolean
  egress_acknowledged?: boolean
  last_test_status?: 'SUCCESS' | 'FAILED' | null
  last_test_error_code?: string | null
  last_test_latency_ms?: number | null
  last_tested_at?: string | null
  in_use_by_projects?: number
  builtin: boolean
  available?: boolean
  created_at?: string
  updated_at?: string
}

export interface ProviderConnectionListResponse {
  items: ProviderConnection[]
}

export interface ProviderConnectionTestResult {
  status: 'SUCCESS' | 'FAILED'
  latency_ms?: number
  error_code?: string
  message?: string
  provider?: string
  model_id?: string
  provider_request_id?: string
  standalone_query?: string
}

/** 当前项目生效的模型连接（GET /rewrite-config 的 selected_provider） */
export interface SelectedProvider {
  id: string
  name: string
  provider_type: string
  model_id: string | null
  revision: number | null
  builtin: boolean
  enabled: boolean
  available: boolean
  last_test_status: string | null
}

/** Provider 观测元信息（V1 §9.4 Playground Trace；不含密钥/原文） */
export interface ProviderTrace {
  connection_id: string | null
  connection_revision: number | null
  provider: string | null
  model_id: string | null
  provider_request_id: string | null
  provider_latency_ms: number | null
  usage: { prompt_tokens?: number | null; completion_tokens?: number | null; total_tokens?: number | null } | null
}

export interface RewriteConfigResponse {
  active: { id: string; config: RewriteConfigPayload }
  defaults: RewriteConfigPayload
  selected_provider?: SelectedProvider
  deployment?: RewriteDeploymentInfo
  versions: { id: string; version: number; config: RewriteConfigPayload; hash: string; status: string; created_at: string }[]
}

export interface TerminologyTerm {
  canonical: string
  aliases: string[]
  confusable_with?: string[]
  never_replace_when?: string
  enabled?: boolean
}

export interface TerminologyResponse {
  active: { id: string; terms: TerminologyTerm[] }
  versions: { id: string; version: number; count: number; hash: string; created_at: string }[]
}

export interface RewriteFeedbackItem {
  id: string
  input_hash: string
  verdict: 'accept' | 'reject' | 'edit'
  reason_codes: string[]
  original_route: string | null
  rewrite_route: string | null
  has_raw_text: boolean
  created_at: string
}

/** 外部模型 V1 §8.2：熔断按连接隔离 */
export interface ConnectionBreakerSummary {
  state: 'closed' | 'open' | 'half-open' | 'unhealthy' | 'rate_limited'
  consecutive_failures: number
  total_calls: number
  total_failures: number
  last_error: string | null
  unhealthy_code: string | null
  rate_limited: boolean
}

export interface RewriteHealth {
  base_url: string
  /** 兼容旧字段；新代码读取 connections */
  breaker_state?: 'closed' | 'open' | 'half-open'
  connections?: Record<string, ConnectionBreakerSummary>
  consecutive_failures?: number
  total_calls?: number
  total_failures?: number
  last_error?: string | null
  rewriter?: { ok?: boolean; [key: string]: unknown }
  metrics: {
    requests_total: number
    success_total: number
    fallback_total: Record<string, number>
    route_conflict_total: Record<string, number>
    safety_reject_total: Record<string, number>
    cache_hit_total: number
    rewrite_latency_ms: { p50: number | null; p95: number | null; n: number }
    cache_size: number
  }
}
