/** 意图标签管理页（自定义意图标签 §7.1）：
 * - 生效版本卡片 + 版本历史来自 /label-schemas 列表；
 * - 存在草稿时展示编辑器，标签行含效果类型徽标与停用操作；
 * - 发布流程：先 PATCH 保存草稿，再 POST /publish（破坏性变更需勾选确认，
 *   未确认时服务端 422 → 弹窗提示且不关闭）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { ProjectProvider } from '../store/project'
import type { LabelSchemaDetail, LabelSchemaInfo } from '../types'
import LabelSchemas from './LabelSchemas'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: vi.fn() }
})

const mockedApi = vi.mocked(api)

afterEach(cleanup)

beforeEach(() => {
  localStorage.clear()
  const getComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = vi.fn((element: Element) => getComputedStyle(element))
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
  window.scrollTo = vi.fn()
  mockedApi.mockReset()
})

function schemaInfo(id: string, version: number, status: LabelSchemaInfo['status']): LabelSchemaInfo {
  return {
    id,
    project_id: 'proj-1',
    version,
    status,
    parent_id: null,
    change_summary: status === 'DRAFT' ? '新增工单意图' : '初始五分类',
    created_by: 'system',
    hash: `${id}-hash-0123456789abcdef`,
    schema_format: 'intent-schema-v2',
    active_label_count: 5,
    deprecated_label_count: 0,
    label_keys: ['information', 'read_only', 'write_action', 'unclear', 'oos'],
    published_at: status === 'ACTIVE' ? '2026-01-01T00:00:00' : null,
    created_at: '2026-01-01T00:00:00',
    references: { datasets: 2, runs: 1, models: 1 },
  }
}

const ACTIVE = schemaInfo('sch_active', 1, 'ACTIVE')
const DRAFT = schemaInfo('sch_draft', 2, 'DRAFT')

const DRAFT_DETAIL: LabelSchemaDetail = {
  ...DRAFT,
  schema_json: { schema_format: 'intent-schema-v2', labels: [] },
  document: {
    schema_format: 'intent-schema-v2',
    labels: [
      { key: 'faq', name: '常见问题', effect_type: 'information', status: 'active', order: 0 },
      { key: 'create_task', name: '创建任务', effect_type: 'write_action', status: 'active', order: 10 },
    ],
  },
}

function renderPage(items: LabelSchemaInfo[]) {
  mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === '/projects/proj-1/label-schemas') return { items }
    if (path === '/projects/proj-1/label-schemas/sch_draft') return DRAFT_DETAIL
    if (path === '/projects/proj-1/label-schemas/sch_draft/impact' && init?.method === 'POST') {
      return {
        schema_id: 'sch_draft',
        base_schema_id: 'sch_active',
        breaking: true,
        added: ['create_task'],
        removed: [],
        deprecated: [],
        effect_type_changed: [{ key: 'faq', from: 'information', to: 'read_only' }],
        affected_datasets: 2,
        affected_runs: 1,
        affected_models: 1,
        requires_retraining: true,
      }
    }
    return {}
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.setItem('irs.projectId', 'proj-1')
  return render(
    <QueryClientProvider client={client}>
      <ProjectProvider>
        <MemoryRouter>
          <LabelSchemas />
        </MemoryRouter>
      </ProjectProvider>
    </QueryClientProvider>,
  )
}

describe('意图标签管理页（§7.1）', () => {
  it('展示生效版本卡片与版本历史（含引用计数）', async () => {
    renderPage([DRAFT, ACTIVE])
    expect(await screen.findByText('当前生效 v1')).toBeInTheDocument()
    // 生效卡片与版本历史行各出现一次
    expect(screen.getAllByText(/数据集 2 · Run 1 · 模型 1/).length).toBeGreaterThanOrEqual(1)
    // 草稿行 + 生效行都出现在版本历史
    expect(screen.getByText('生效中')).toBeInTheDocument()
    expect(screen.getByText('草稿')).toBeInTheDocument()
  })

  it('存在草稿时展示编辑器：标签行含效果类型与停用操作', async () => {
    renderPage([DRAFT, ACTIVE])
    expect(await screen.findByText('常见问题')).toBeInTheDocument()
    expect(screen.getByText('创建任务')).toBeInTheDocument()
    // write_action 效果徽标带「仅候选资格」提示文案
    expect(screen.getByText('外部写入（仅候选资格）')).toBeInTheDocument()
    // 两个草稿标签行各有一个停用按钮
    expect(screen.getAllByRole('button', { name: /停用/ })).toHaveLength(2)
  })

  it('发布流程：先 PATCH 保存草稿再 POST publish；破坏性变更未勾选时被拒绝并保持弹窗', async () => {
    renderPage([DRAFT, ACTIVE])
    await screen.findByText('创建任务')

    // 点击卡片头的「发布」→ 拉取影响分析并打开弹窗
    fireEvent.click(screen.getAllByRole('button', { name: /发\s*布/ })[0])
    expect(await screen.findByText('破坏性变更')).toBeInTheDocument()
    // 精确锚定报告行，避免匹配勾选框文案里的子串
    expect(screen.getByText(/^效果类型变化/)).toBeInTheDocument()

    // OK 按钮在未勾选确认时禁用
    const footer = document.querySelector('.ant-modal-footer')
    expect(footer).not.toBeNull()
    const okBtn = footer!.querySelector('button.ant-btn-primary') as HTMLButtonElement
    expect(okBtn).toBeDisabled()

    // 勾选确认 → PATCH 草稿 + POST 发布
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(okBtn)
    await waitFor(() =>
      expect(mockedApi).toHaveBeenCalledWith(
        '/projects/proj-1/label-schemas/sch_draft',
        expect.objectContaining({ method: 'PATCH' }),
      ),
    )
    await waitFor(() =>
      expect(mockedApi).toHaveBeenCalledWith(
        '/projects/proj-1/label-schemas/sch_draft/publish',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ expected_hash: `${DRAFT.hash}`, confirm_breaking_changes: true }),
        }),
      ),
    )
  })

  it('无草稿时仅显示新建入口，不渲染编辑器', async () => {
    renderPage([ACTIVE])
    expect(await screen.findByText('当前生效 v1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /新建变更草稿/ })).toBeInTheDocument()
    expect(screen.queryByText('编辑草稿 v2')).toBeNull()
  })
})
