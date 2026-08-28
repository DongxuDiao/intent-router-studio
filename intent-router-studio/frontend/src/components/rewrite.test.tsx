/** 改写组件测试（§16.3）：三 Query 对照、安全门展示、降级与拦截提示。 */
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { ProviderTraceCard, QueryRewritePanel, RewriteDiff, RewriteSafetyChecks } from './rewrite'
import type { QueryUnderstanding } from '../types'

function renderWithProviders(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function fixture(overrides: Partial<QueryUnderstanding> = {}): QueryUnderstanding {
  return {
    mode: 'shadow',
    rewrite: {
      original_query: '这个怎么停？',
      normalized_query: '这个怎么停？',
      standalone_query: '怎么停？ 实验 123',
      rewrite_type: 'context_resolution',
      changed: true,
      should_use: true,
      confidence: 0.9,
      preserved_intent: true,
      mentioned_action: null,
      objects: [{ type: 'entity', value: '实验 123', source: 'context', confidence: 0.9 }],
      constraints: {},
      missing_slots: [],
      assumptions: [],
      used_context_refs: ['实验 123'],
      reason_codes: ['RESOLVED_PRONOUN'],
      model: { provider: 'stub', model_id: 'stub-rewriter', prompt_version: 'rewrite-prompt-v1' },
      latency_ms: 12,
      term_replacements: [{ rule_id: 'abc123', source_term: 'libra exp', target_term: 'Libra 实验', source_span: [0, 10] }],
    },
    original_route: {
      route: 'information', decision: 'accept', confidence: 0.71, margin: 0.42,
      top_k: [], reason_codes: [], effect_ceiling: 'none', required_next_gate: 'answer_or_kb',
      latency_ms: 0.1, model_version: 'test',
    },
    rewrite_route: {
      route: 'information', decision: 'accept', confidence: 0.73, margin: 0.45,
      top_k: [], reason_codes: [], effect_ceiling: 'none', required_next_gate: 'answer_or_kb',
      latency_ms: 0.1, model_version: 'test',
    },
    route_consistent: true,
    downstream_query: '这个怎么停？',
    downstream_query_source: 'original',
    safety_decision: 'allow_rewrite_shadow',
    safety: {
      allow: true,
      safety_decision: 'allow_rewrite',
      reason_codes: [],
      checks: [
        { name: 'negation_preserved', passed: true, detail: '原文否定 0 处 / 改写 0 处' },
        { name: 'confidence_threshold', passed: true, detail: '0.90 >= 0.80' },
        { name: 'route_consistency', passed: true, detail: '路由一致' },
      ],
      route_conflict: false,
      escalation: false,
      downgrade: false,
      route_policy: {
        downstream_rewrite_allowed: true, formal_route: 'information', conflict: false,
        escalation: false, downgrade: false, note: '路由一致',
      },
    },
    fallback_reason: null,
    final_route: 'information',
    cache_hit: false,
    ...overrides,
  }
}

describe('RewriteDiff', () => {
  it('展示三 Query 并高亮变化项与术语替换', () => {
    render(<RewriteDiff u={fixture()} />)
    // 原始与规范化文本相同（本例规范化未改变内容）→ 出现两次
    expect(screen.getAllByText('这个怎么停？').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('怎么停？ 实验 123')).toBeTruthy()
    expect(screen.getByText(/libra exp → Libra 实验/)).toBeTruthy()
  })
})

describe('RewriteSafetyChecks', () => {
  it('逐项渲染检查结果', () => {
    render(<RewriteSafetyChecks u={fixture()} />)
    expect(screen.getByText('否定一致')).toBeTruthy()
    expect(screen.getByText('置信度阈值')).toBeTruthy()
    expect(screen.getByText(/0.90 >= 0.80/)).toBeTruthy()
  })

  it('降级时展示原因而非检查列表', () => {
    render(<RewriteSafetyChecks u={fixture({ safety: null, fallback_reason: 'PROVIDER_UNAVAILABLE' })} />)
    expect(screen.getByText(/已降级：改写服务不可用/)).toBeTruthy()
  })
})

describe('QueryRewritePanel', () => {
  it('渲染输入区与模式选择（当前选中项目默认）', () => {
    renderWithProviders(<QueryRewritePanel projectId="prj_test" />)
    expect(screen.getByPlaceholderText(/用户 query/)).toBeTruthy()
    expect(screen.getByText('跟随项目配置')).toBeTruthy() // Select 当前值直接渲染在 DOM
    expect(screen.getByRole('button', { name: '理解 Query' })).toBeTruthy()
  })
})


describe('ProviderTraceCard（外部模型 V1 §9.4）', () => {
  it('展示连接 / 模型 / 延迟 / token / 请求 ID 与缓存命中', () => {
    renderWithProviders(
      <ProviderTraceCard
        u={fixture({
          cache_hit: true,
          provider_trace: {
            connection_id: 'rpc_glm0000000000000000',
            connection_revision: 3,
            provider: 'glm',
            model_id: 'glm-5.2',
            provider_request_id: 'glmr-abc',
            provider_latency_ms: 812.34,
            usage: { prompt_tokens: 210, completion_tokens: 24, total_tokens: 234 },
          },
        })}
      />,
    )
    expect(screen.getByText('rpc_glm0000000000000000')).toBeTruthy()
    expect(screen.getByText('rev 3')).toBeTruthy()
    expect(screen.getByText('glm / glm-5.2')).toBeTruthy()
    expect(screen.getByText('provider 812ms')).toBeTruthy()
    expect(screen.getByText('tokens 234（生成 24）')).toBeTruthy()
    expect(screen.getByText('glmr-abc')).toBeTruthy()
    expect(screen.getByText(/缓存命中/)).toBeTruthy()
  })

  it('降级时展示 fallback 原因；内置连接显示本地 Qwen', () => {
    renderWithProviders(
      <ProviderTraceCard
        u={fixture({
          fallback_reason: 'PROVIDER_AUTH_FAILED',
          provider_trace: {
            connection_id: 'builtin:local_qwen',
            connection_revision: null,
            provider: 'local_qwen',
            model_id: null,
            provider_request_id: null,
            provider_latency_ms: 41.2,
            usage: null,
          },
        })}
      />,
    )
    expect(screen.getByText('本地 Qwen')).toBeTruthy()
    expect(screen.getByText(/降级 PROVIDER_AUTH_FAILED/)).toBeTruthy()
  })
})
