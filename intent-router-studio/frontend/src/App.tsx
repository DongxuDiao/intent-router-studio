/** 应用骨架：Layout + Sider 菜单 + 路由。 */
import { Layout, Menu, Spin, Typography } from 'antd'
import {
  ApiOutlined,
  AppstoreOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  ProjectOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { useProject } from './store/project'
import Projects from './pages/Projects'

// 按页面拆包（修改方案 V2 §5.2）：除落地页项目列表外全部懒加载，
// ECharts 等重组件只进图表页的异步 chunk。
const Overview = lazy(() => import('./pages/Overview'))
const Datasets = lazy(() => import('./pages/Datasets'))
const DatasetDetail = lazy(() => import('./pages/DatasetDetail'))
const UploadWizard = lazy(() => import('./pages/UploadWizard'))
const LabelWorkbench = lazy(() => import('./pages/LabelWorkbench'))
const NewRun = lazy(() => import('./pages/NewRun'))
const Runs = lazy(() => import('./pages/Runs'))
const RunDetail = lazy(() => import('./pages/RunDetail'))
const Models = lazy(() => import('./pages/Models'))
const Playground = lazy(() => import('./pages/Playground'))
const RewriteSettings = lazy(() => import('./pages/RewriteSettings'))
const SystemInfo = lazy(() => import('./pages/SystemInfo'))

function PageLoading() {
  return (
    <div style={{ padding: 48, textAlign: 'center' }}>
      <Spin tip="页面加载中…" />
    </div>
  )
}

const { Sider, Header, Content } = Layout

const MENU = [
  { key: '/projects', icon: <ProjectOutlined />, label: '项目' },
  { key: '/overview', icon: <DashboardOutlined />, label: '总览' },
  { key: '/datasets', icon: <DatabaseOutlined />, label: '数据集' },
  { key: '/datasets/upload', icon: <AppstoreOutlined />, label: '导入数据' },
  { key: '/training/new', icon: <ThunderboltOutlined />, label: '发起训练' },
  { key: '/runs', icon: <PlayCircleOutlined />, label: '训练运行' },
  { key: '/models', icon: <CloudServerOutlined />, label: '模型注册表' },
  { key: '/playground', icon: <ExperimentOutlined />, label: 'Playground' },
  { key: '/rewrite', icon: <FileSearchOutlined />, label: '改写设置' },
  { key: '/system', icon: <ApiOutlined />, label: '系统信息' },
]

function fullPath(pathname: string): string {
  if (pathname.startsWith('/datasets/') && pathname !== '/datasets/upload') return '/datasets'
  if (pathname.startsWith('/runs/')) return '/runs'
  return pathname
}

export default function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const { projectId } = useProject()

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={200} style={{ borderRight: '1px solid #f0f0f0' }}>
        <div style={{ padding: '18px 16px 8px' }}>
          <Typography.Title level={5} style={{ margin: 0 }}>
            Intent Router Studio
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            意图路由 · 安全优先
          </Typography.Text>
        </div>
        <Menu
          mode="inline"
          style={{ borderInlineEnd: 'none' }}
          selectedKeys={[fullPath(location.pathname)]}
          items={MENU}
          onClick={({ key }) => navigate(key)}
        />
        <div style={{ position: 'absolute', bottom: 12, left: 16, right: 16 }}>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {projectId ? `项目 ${projectId}` : '未选择项目'}
          </Typography.Text>
        </div>
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', padding: '0 24px', borderBottom: '1px solid #f0f0f0', height: 48, lineHeight: '48px' }}>
          <Typography.Text type="secondary">
            write_action 仅授予 external_write_candidate 上限 —— 系统永不直接执行外部写入
          </Typography.Text>
        </Header>
        <Content style={{ padding: 24 }}>
          <Suspense fallback={<PageLoading />}>
            <Routes>
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/datasets" element={<Datasets />} />
            <Route path="/datasets/upload" element={<UploadWizard />} />
            <Route path="/datasets/:id" element={<DatasetDetail />} />
            <Route path="/datasets/:id/label" element={<LabelWorkbench />} />
            <Route path="/training/new" element={<NewRun />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/models" element={<Models />} />
            <Route path="/playground" element={<Playground />} />
            <Route path="/rewrite" element={<RewriteSettings />} />
            <Route path="/system" element={<SystemInfo />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  )
}
