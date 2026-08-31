/** 改写模型连接前端测试（外部模型 V1 §14.3）：
 * - 编辑时 API Key 不回填；保存请求含 Key，但界面/缓存不含返回值中的敏感信息
 * - GLM 预设锁定官方 Base URL；openai_compatible 可编辑
 * - 测试失败的远程连接不可选为项目改写模型
 * - 切换远程模型时显示外部数据传输提示
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { ProjectProvider } from '../store/project'
import type { ProviderConnection, ProviderConnectionListResponse, RewriteConfigResponse } from '../types'
import RewriteSettings from './RewriteSettings'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: vi.fn() }
})

const mockedApi = vi.mocked(api)
const SECRET = 'zhipu-flutter-secret-9988'

afterEach(cleanup)

beforeEach(() => {
  localStorage.clear()
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }))
  window.scrollTo = vi.fn()
  mockedApi.mockReset()
})

const CONNECTIONS: ProviderConnection[] = [
  {
    id: 'builtin:local_qwen', name: '本地 Qwen3-0.6B', provider_type: 'local_qwen',
    model_id: null, builtin: true, enabled: true, available: true,
  },
  {
    id: 'rpc_ok_test0000000000000', name: '生产 GLM', provider_type: 'glm',
    base_url: 'https://open.bigmodel.cn/api/paas/v4', model_id: 'glm-5.2',
    api_key_hint: '****9988', has_api_key: true, revision: 1, enabled: true,
    egress_acknowledged: true, last_test_status: 'SUCCESS', last_test_latency_ms: 812.3,
    in_use_by_projects: 0, builtin: false,
  },
  {
    id: 'rpc_bad_test0000000000000', name: '坏 Key GLM', provider_type: 'glm',
    base_url: 'https://open.bigmodel.cn/api/paas/v4', model_id: 'glm-5.2',
    api_key_hint: '****dead', has_api_key: true, revision: 1, enabled: true,
    egress_acknowledged: true, last_test_status: 'FAILED', last_test_error_code: 'PROVIDER_AUTH_FAILED',
    in_use_by_projects: 0, builtin: false,
  },
]

function renderPage() {
  const baseConfig = {
    mode: 'shadow', timeout_ms: 90000, min_rewrite_confidence: 0.8,
    require_route_consistency: true, fallback: 'original', store_raw_text: false,
  } as const
  mockedApi.mockImplementation(async (path: string) => {
    if (path === '/projects/proj-1/rewrite-config')
      return {
        active: { id: 'rwcfg_1', config: baseConfig },
        defaults: baseConfig,
        selected_provider: { id: 'builtin:local_qwen', name: '本地 Qwen3-0.6B', provider_type: 'local_qwen', model_id: null, revision: null, builtin: true, enabled: true, available: true, last_test_status: null },
        versions: [],
      } satisfies RewriteConfigResponse
    if (path === '/rewrite/provider-connections') return { items: CONNECTIONS } satisfies ProviderConnectionListResponse
    if (path === '/projects/proj-1/terminology') return { active: { id: 'none', terms: [] } }
    if (path === '/inference/rewrite/health')
      return {
        base_url: 'http://x', connections: { 'builtin:local_qwen': { state: 'closed', consecutive_failures: 0, total_calls: 0, total_failures: 0, last_error: null, unhealthy_code: null, rate_limited: false } },
        rewriter: { ok: true }, metrics: { requests_total: 0, success_total: 0, fallback_total: {}, route_conflict_total: {}, safety_reject_total: {}, cache_hit_total: 0, rewrite_latency_ms: { p50: null, p95: null, n: 0 }, cache_size: 0 },
      }
    return {}
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.setItem('irs.projectId', 'proj-1')
  return render(
    <QueryClientProvider client={client}>
      <ProjectProvider>
        <RewriteSettings />
      </ProjectProvider>
    </QueryClientProvider>,
  )
}

async function openDrawer(editName?: string) {
  await waitFor(() => screen.getByText('生产 GLM'))
  if (editName) {
    const row = screen.getByText(editName).closest('tr')
    expect(row).not.toBeNull()
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /编\s*辑/ }))
  } else {
    fireEvent.click(screen.getByRole('button', { name: /新\s*建\s*连\s*接/ }))
  }
  await waitFor(() => screen.getByText(/新建模型连接|编辑连接/))
}

describe('改写模型连接（V1 §9）', () => {
  it('编辑抽屉 API Key 不回填，留空表示保留', async () => {
    renderPage()
    await openDrawer('生产 GLM')
    const keyInput = screen.getByPlaceholderText('留空保持不变')
    expect((keyInput as HTMLInputElement).value).toBe('')
    expect(within(document.body).queryByDisplayValue(SECRET)).toBeNull()
  })

  it('保存请求包含 Key；界面与本地存储不出现密钥', async () => {
    renderPage()
    await openDrawer()
    fireEvent.change(screen.getByPlaceholderText('如：生产 GLM'), { target: { value: '新连接' } })
    fireEvent.change(screen.getByPlaceholderText('粘贴开放平台 API Key'), { target: { value: SECRET } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /保\s*存\s*并\s*测\s*试/ }))
    await waitFor(() => {
      const call = mockedApi.mock.calls.find((c) => String(c[0]).includes('provider-connections') && c[1]?.method === 'POST')
      expect(call).toBeTruthy()
      expect(String(call![1]?.body)).toContain(SECRET) // 写入请求携带
    })
    // 响应（连接对象）不含密钥；界面与 localStorage 同样不落
    expect(document.body.textContent ?? '').not.toContain(SECRET)
    expect(JSON.stringify(localStorage)).not.toContain(SECRET)
  })

  it('GLM 用端点档位选择（无自由 URL 输入）；coding 档显示条款警示', async () => {
    renderPage()
    await openDrawer()
    // GLM：不出现自由 URL 输入框，展示端点选择器与通用端点地址
    expect(screen.queryByPlaceholderText('https://api.example.com/v1')).toBeNull()
    expect(screen.getByText(/端点档位/)).toBeInTheDocument()
    expect(screen.getByText(/端点：https:\/\/open\.bigmodel\.cn\/api\/paas\/v4/)).toBeInTheDocument()
    // 切到 coding：显示官方条款警示
    fireEvent.mouseDown(screen.getByText('通用开放平台（按量计费，消耗 API 余额）').closest('.ant-select-selector')!)
    const coding = await waitFor(() => screen.getByText('Coding Plan 专用端点（消耗订阅额度）'))
    fireEvent.click(coding)
    expect(await screen.findByText('Coding Plan 官方条款仅限指定编码工具使用')).toBeInTheDocument()
  })

  it('openai_compatible 才有可编辑 Base URL', async () => {
    renderPage()
    await openDrawer()
    fireEvent.mouseDown(screen.getByText('智谱 GLM（官方端点）').closest('.ant-select-selector')!)
    const option = await waitFor(() => screen.getByText('OpenAI 兼容 API（自定义 Base URL）'))
    fireEvent.click(option)
    await waitFor(() => {
      const input = screen.getByPlaceholderText('https://api.example.com/v1') as HTMLInputElement
      expect(input.disabled).toBe(false)
    })
  })

  it('测试失败的连接在项目模型选择中禁用', async () => {
    renderPage()
    await waitFor(() => screen.getByText('坏 Key GLM'))
    // 打开「改写模型」下拉
    const modelSelect = screen.getByText('改写模型').parentElement!.parentElement!.querySelector('.ant-select')!
    fireEvent.mouseDown(modelSelect.querySelector('.ant-select-selector')!)
    await waitFor(() => screen.getAllByText(/坏 Key GLM/).length >= 2)
    const option = screen.getAllByText(/坏 Key GLM/).find((el) => el.closest('.ant-select-item-option'))
    expect(option?.closest('.ant-select-item-option-disabled')).not.toBeNull()
  })

  it('切换到远程模型显示外部数据传输提示', async () => {
    renderPage()
    await waitFor(() => screen.getByText('生产 GLM'))
    const modelSelect = screen.getByText('改写模型').parentElement!.parentElement!.querySelector('.ant-select')!
    fireEvent.mouseDown(modelSelect.querySelector('.ant-select-selector')!)
    const option = await waitFor(() => {
      const found = screen.getAllByText(/生产 GLM/).find((el) => el.closest('.ant-select-item-option'))
      expect(found).toBeTruthy()
      return found!
    })
    fireEvent.click(option)
    expect(await screen.findByText('外部数据传输提示')).toBeVisible()
    expect(document.body.textContent).toContain('将发送到该外部模型服务')
  })
})
