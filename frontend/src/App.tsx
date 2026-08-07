import { lazy, Suspense, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api/client'

// 页面按需加载(方案: React.lazy + Suspense)
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Screener = lazy(() => import('./pages/Screener'))
const Watchlist = lazy(() => import('./pages/Watchlist'))
const Signals = lazy(() => import('./pages/Signals'))
const Plans = lazy(() => import('./pages/Plans'))
const Trades = lazy(() => import('./pages/Trades'))
const Review = lazy(() => import('./pages/Review'))
const AiReview = lazy(() => import('./pages/AiReview'))
const Settings = lazy(() => import('./pages/Settings'))

const NAV = [
  { to: '/dashboard', label: '仪表盘' },
  { to: '/screener', label: '选股' },
  { to: '/watchlist', label: '自选与持仓' },
  { to: '/signals', label: '信号中心' },
  { to: '/plans', label: '交易计划' },
  { to: '/trades', label: '交易日志' },
  { to: '/review', label: '历史回顾' },
  { to: '/ai-review', label: 'AI复盘' },
  { to: '/settings', label: '设置' },
]

function Placeholder({ title }: { title: string }) {
  return (
    <div style={{ padding: 48, textAlign: 'center', color: '#666' }}>
      <h2>{title}</h2>
      <p>该模块将在后续阶段实现(二期/三期/四期)。</p>
    </div>
  )
}

// 保留占位组件导出, 供后续阶段页面复用
export { Placeholder }

export default function App() {
  const [error, setError] = useState('')

  useEffect(() => {
    api.health().catch((e) => setError(String(e.message || e)))
  }, [])

  return (
    <div style={{ display: 'flex', minHeight: '100vh', fontFamily: 'system-ui, "Microsoft YaHei", sans-serif' }}>
      {/* 左侧导航 */}
      <nav style={{ width: 180, borderRight: '1px solid #e5e6eb', padding: '16px 8px', flexShrink: 0 }}>
        <div style={{ fontWeight: 700, padding: '0 8px 16px', fontSize: 15 }}>Momentum Trader</div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            style={({ isActive }) => ({
              display: 'block',
              padding: '8px 12px',
              marginBottom: 2,
              borderRadius: 6,
              textDecoration: 'none',
              fontSize: 14,
              color: isActive ? '#fff' : '#333',
              background: isActive ? '#2563eb' : 'transparent',
            })}
          >
            {n.label}
          </NavLink>
        ))}
      </nav>

      {/* 主内容区 */}
      <main style={{ flex: 1, padding: 24 }}>
        <Suspense fallback={<div style={{ padding: 48, color: '#888' }}>加载中...</div>}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/screener" element={<Screener />} />
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/plans" element={<Plans />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/review" element={<Review />} />
            <Route path="/ai-review" element={<AiReview />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </Suspense>
        {error && <div style={{ color: '#c00', marginTop: 16, fontSize: 13 }}>后端未连接: {error}</div>}
      </main>
    </div>
  )
}
