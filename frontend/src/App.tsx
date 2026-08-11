import { lazy, Suspense, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AnimatePresence } from 'framer-motion'
import { api } from './api/client'
import { PageSkeleton, ToastHost } from './components/ui'
import { PageTransition } from './components/PageTransition'

// 页面按需加载(方案: React.lazy + Suspense)
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Screener = lazy(() => import('./pages/Screener'))
const Watchlist = lazy(() => import('./pages/Watchlist'))
const Signals = lazy(() => import('./pages/Signals'))
const Plans = lazy(() => import('./pages/Plans'))
const Trades = lazy(() => import('./pages/Trades'))
const Review = lazy(() => import('./pages/Review'))
const AiReview = lazy(() => import('./pages/AiReview'))
const Backtest = lazy(() => import('./pages/Backtest'))
const Settings = lazy(() => import('./pages/Settings'))
const Guide = lazy(() => import('./pages/Guide'))

// react-query 全局客户端(默认 15s 轮询由各页面 query 配置)
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
})

const NAV = [
  { to: '/dashboard', label: '仪表盘' },
  { to: '/screener', label: '选股' },
  { to: '/watchlist', label: '自选与持仓' },
  { to: '/signals', label: '信号中心' },
  { to: '/plans', label: '交易计划' },
  { to: '/trades', label: '交易日志' },
  { to: '/review', label: '历史回顾' },
  { to: '/ai-review', label: 'AI复盘' },
  { to: '/backtest', label: '回测中心' },
  { to: '/guide', label: '交易说明书' },
  { to: '/settings', label: '设置' },
]

function Sidebar() {
  return (
    <aside className="shrink-0 border-b border-line px-2 py-2 md:w-[180px] md:border-b-0 md:border-r md:px-2 md:py-4">
      <div className="hidden px-2 pb-4 text-[15px] font-bold text-ink md:block">Momentum Trader</div>
      <nav className="flex gap-0.5 overflow-x-auto md:flex-col">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              twNav(isActive)
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}

// 导航项样式: 激活态蓝底白字; 移动端横向滚动, 桌面纵向
function twNav(isActive: boolean) {
  return `block whitespace-nowrap rounded px-3 py-2 text-[14px] no-underline transition-colors ${
    isActive ? 'bg-link text-white' : 'text-ink hover:bg-divider'
  }`
}

function AppShell() {
  const [error, setError] = useState('')
  const location = useLocation()

  useEffect(() => {
    api.health().catch((e) => setError(String(e.message || e)))
  }, [])

  return (
    <div className="flex min-h-screen flex-col font-sans text-ink md:flex-row">
      <Sidebar />
      {/* 主内容区: 统一最大宽度并在宽屏居中, 各页视觉对齐 */}
      <main className="mx-auto min-w-0 w-full max-w-[1440px] flex-1 p-4 md:p-6">
        <Suspense fallback={<PageSkeleton />}>
          <AnimatePresence mode="wait">
            <PageTransition key={location.pathname}>
              <Routes location={location}>
                <Route path="/" element={<Navigate to="/dashboard" replace />} />
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/screener" element={<Screener />} />
                <Route path="/watchlist" element={<Watchlist />} />
                <Route path="/signals" element={<Signals />} />
                <Route path="/plans" element={<Plans />} />
                <Route path="/trades" element={<Trades />} />
                <Route path="/review" element={<Review />} />
                <Route path="/ai-review" element={<AiReview />} />
                <Route path="/backtest" element={<Backtest />} />
                <Route path="/guide" element={<Guide />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="*" element={<Navigate to="/dashboard" replace />} />
              </Routes>
            </PageTransition>
          </AnimatePresence>
        </Suspense>
        {error && <div className="mt-4 text-[13px] text-rise">后端未连接: {error}</div>}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppShell />
      <ToastHost />
    </QueryClientProvider>
  )
}
