/** 模型激活/回滚分派与项目进入修复（修改方案 V2 §4.5）：
 * - ARCHIVED 模型"回滚激活"必须调用 /rollback（后端 /activate 拒绝归档模型）；
 * - 普通候选才调用 /activate；弹窗按目标状态展示"确认激活"或"确认回滚"；
 * - 项目列表"进入"必须先 setProjectId 再导航。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { ProjectProvider } from '../store/project'
import type { ModelVersion, Project } from '../types'
import Models from './Models'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: vi.fn() }
})

const mockedApi = vi.mocked(api)

afterEach(cleanup) // vitest 未开 globals，RTL 不会自动卸载组件

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

function model(id: string, name: string, status: ModelVersion['status'], runId: string): ModelVersion {
  return {
    id,
    project_id: 'proj-1',
    run_id: runId,
    threshold_id: null,
    name,
    status,
    manifest: null,
    metrics_summary: { macro_f1: 0.8, false_write_rate: 0.02, safe_coverage: 0.7 },
    created_at: '2026-01-01T00:00:00',
    activated_at: status === 'ACTIVE' ? '2026-01-02T00:00:00' : null,
  }
}

const FIXTURES: ModelVersion[] = [
  model('mdl_active', 'v3-活跃', 'ACTIVE', 'run_a'),
  model('mdl_arch', 'v2-已归档', 'ARCHIVED', 'run_b'),
  model('mdl_cand', 'v4-候选', 'CANDIDATE', 'run_c'),
]

function renderModels() {
  mockedApi.mockImplementation(async (path: string) => {
    if (path === '/projects/proj-1/models') return { items: FIXTURES }
    return {}
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.setItem('irs.projectId', 'proj-1')
  return render(
    <QueryClientProvider client={client}>
      <ProjectProvider>
        <MemoryRouter>
          <Models />
        </MemoryRouter>
      </ProjectProvider>
    </QueryClientProvider>,
  )
}

async function openModalAndConfirm(rowButton: RegExp, expectTitle: string, okText: RegExp) {
  await waitFor(() => screen.getByText('v4-候选'))
  fireEvent.click(screen.getByRole('button', { name: rowButton }))
  // antd Modal 在 jsdom 中带进场动画，只断言存在不断言可见性
  expect(await screen.findByText(expectTitle)).toBeInTheDocument()
  const footer = document.querySelector('.ant-modal-footer')
  expect(footer).not.toBeNull()
  fireEvent.click(within(footer as HTMLElement).getByRole('button', { name: okText }))
}

describe('模型激活/回滚分派（V2 §4.5）', () => {
  it('ARCHIVED 模型走 /rollback，弹窗展示"确认回滚"', async () => {
    renderModels()
    await openModalAndConfirm(/回滚激活/, '确认回滚', /^回\s*滚$/)
    await waitFor(() =>
      expect(mockedApi).toHaveBeenCalledWith('/models/mdl_arch/rollback', { method: 'POST' }),
    )
    expect(mockedApi).not.toHaveBeenCalledWith('/models/mdl_arch/activate', { method: 'POST' })
  })

  it('普通候选走 /activate，弹窗展示"确认激活"', async () => {
    renderModels()
    await openModalAndConfirm(/^激\s*活$/, '确认激活', /^激\s*活$/)
    await waitFor(() =>
      expect(mockedApi).toHaveBeenCalledWith('/models/mdl_cand/activate', { method: 'POST' }),
    )
  })

  it('ACTIVE 行不出现激活/回滚按钮', async () => {
    renderModels()
    // v3-活跃 同时出现在顶部摘要卡与表格行，改等候选行渲染完成
    await waitFor(() => screen.getByText('v4-候选'))
    // 只有候选行的"激活"按钮，无第二处
    expect(screen.getAllByRole('button', { name: /^激\s*活$/ })).toHaveLength(1)
    expect(screen.queryByRole('button', { name: '重新激活' })).toBeNull()
  })
})

/** 项目"进入"先切换当前项目再导航。 */
describe('项目进入修复（V2 §4.5）', () => {
  it('点击"进入"写入 projectId 并跳转 /overview', async () => {
    const Projects = (await import('./Projects')).default
    const projects: Project[] = [
      { id: 'proj-1', name: '甲项目', description: '', active_model_id: null, active_model_name: null, dataset_count: 0, run_count: 0, created_at: '2026-01-01T00:00:00' },
      { id: 'proj-2', name: '乙项目', description: '', active_model_id: null, active_model_name: null, dataset_count: 1, run_count: 0, created_at: '2026-01-01T00:00:00' },
    ]
    mockedApi.mockImplementation(async (path: string) => {
      if (path === '/projects') return { items: projects }
      return {}
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    localStorage.setItem('irs.projectId', 'proj-1')
    render(
      <QueryClientProvider client={client}>
        <ProjectProvider>
          <MemoryRouter initialEntries={['/projects']}>
            <Routes>
              <Route path="/projects" element={<Projects />} />
              <Route path="/overview" element={<div>OVERVIEW_MARKER</div>} />
            </Routes>
          </MemoryRouter>
        </ProjectProvider>
      </QueryClientProvider>,
    )
    const row = (await screen.findByText('乙项目')).closest('.ant-list-item')
    expect(row).not.toBeNull()
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /^进\s*入$/ }))
    // 先 setProjectId（localStorage 即刻更新）再导航
    expect(localStorage.getItem('irs.projectId')).toBe('proj-2')
    expect(await screen.findByText('OVERVIEW_MARKER')).toBeVisible()
  })

  it('空项目经确认后删除，并清空当前项目与 Playground 缓存', async () => {
    const Projects = (await import('./Projects')).default
    const emptyProject: Project = {
      id: 'proj-empty', name: '空项目', description: '', active_model_id: null,
      active_model_name: null, dataset_count: 0, run_count: 0, created_at: '2026-01-01T00:00:00',
    }
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/projects' && !init) return { items: [emptyProject] }
      if (path === '/projects/proj-empty/deletion-impact') {
        return {
          project_id: 'proj-empty', project_name: '空项目', is_empty: true, can_delete: true,
          counts: {}, active_runs: [],
        }
      }
      if (path === '/projects/proj-empty' && init?.method === 'DELETE') {
        return { deleted: true, project_id: 'proj-empty' }
      }
      return {}
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    localStorage.setItem('irs.projectId', 'proj-empty')
    localStorage.setItem('irs.playground.v1.proj-empty', JSON.stringify({ version: 1, text: 'cached' }))
    render(
      <QueryClientProvider client={client}>
        <ProjectProvider>
          <MemoryRouter><Projects /></MemoryRouter>
        </ProjectProvider>
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: /^删\s*除$/ }))
    expect(await screen.findByText('确认删除空项目')).toBeInTheDocument()
    const footer = document.querySelector('.ant-modal-footer')
    expect(footer).not.toBeNull()
    fireEvent.click(within(footer as HTMLElement).getByRole('button', { name: '确认删除' }))

    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith('/projects/proj-empty', { method: 'DELETE' }))
    expect(localStorage.getItem('irs.projectId')).toBeNull()
    expect(localStorage.getItem('irs.playground.v1.proj-empty')).toBeNull()
  })

  it('非空项目必须输入完整项目名后才可级联删除', async () => {
    const Projects = (await import('./Projects')).default
    const project: Project = {
      id: 'proj-data', name: '有数据项目', description: '', active_model_id: null,
      active_model_name: null, dataset_count: 1, run_count: 2, created_at: '2026-01-01T00:00:00',
    }
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/projects' && !init) return { items: [project] }
      if (path === '/projects/proj-data/deletion-impact') {
        return {
          project_id: 'proj-data', project_name: '有数据项目', is_empty: false, can_delete: true,
          counts: { datasets: 1, runs: 2, uploads: 1 }, active_runs: [],
        }
      }
      if (path === '/projects/proj-data' && init?.method === 'DELETE') {
        return { deleted: true, project_id: 'proj-data', counts: { datasets: 1, runs: 2, uploads: 1 } }
      }
      return {}
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <ProjectProvider><MemoryRouter><Projects /></MemoryRouter></ProjectProvider>
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: /^删\s*除$/ }))
    expect(await screen.findByText('永久删除项目及全部数据')).toBeInTheDocument()
    const confirmButton = screen.getByRole('button', { name: '确认删除' })
    expect(confirmButton).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText('有数据项目'), { target: { value: '有数据项目' } })
    expect(confirmButton).not.toBeDisabled()
    fireEvent.click(confirmButton)

    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith(
      '/projects/proj-data',
      { method: 'DELETE', body: JSON.stringify({ confirm_name: '有数据项目' }) },
    ))
  })
})
