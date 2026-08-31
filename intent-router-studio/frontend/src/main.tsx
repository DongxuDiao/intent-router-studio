import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
// 注意：走 es 原生 ESM 路径。Vite 8（rolldown）对 antd/locale/*（CJS）的
// default 互操作在浏览器端不解包，ConfigProvider 拿到 {default:locale} 导致
// 中文文案静默失效（vitest 的转换链不受影响，易漏测）。
import zhCN from 'antd/es/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ProjectProvider } from './store/project'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#2f54eb' } }}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <ProjectProvider>
            <App />
          </ProjectProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>,
)
