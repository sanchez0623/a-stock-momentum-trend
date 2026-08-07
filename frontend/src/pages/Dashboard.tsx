import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { HealthData, Portfolio, RiskStatus, SignalRecord } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Card, ErrorBox, Tag } from '../components/ui'

export default function Dashboard() {
  const [health, setHealth] = useState<HealthData | null>(null)
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [signals, setSignals] = useState<SignalRecord[]>([])
  const [error, setError] = useState('')

  const refresh = () => {
    api.health().then(setHealth).catch((e) => setError(String(e.message || e)))
    api.positions().then(setPortfolio).catch(() => {})
    api.riskStatus().then(setRisk).catch(() => {})
    api.signals(undefined, 5).then(setSignals).catch(() => {})
  }

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 15000) // 15s 刷新
    return () => clearInterval(timer)
  }, [])

  if (error && !health) return <ErrorBox message={'后端连接失败: ' + error} />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>仪表盘</h1>
      <div style={{ color: '#888', fontSize: 12, marginBottom: 16 }}>
        {health ? `${health.date} · 时区 ${health.tz} · ${health.status === 'up' ? '运行中' : health.status}` : '加载中...'}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 12, marginBottom: 16 }}>
        <Card title="系统状态">
          <div style={{ fontSize: 22, fontWeight: 700, color: health?.status === 'up' ? '#16a34a' : '#dc2626' }}>
            {health ? (health.status === 'up' ? '运行中' : health.status) : '-'}
          </div>
          <div style={{ fontSize: 12, color: '#888', marginTop: 4 }}>
            数据源 {health ? health.data_sources.filter((s) => !s.circuit_open).length + '/' + health.data_sources.length + ' 个可用' : '-'}
          </div>
        </Card>
        <Card title="持仓盈亏">
          <div style={{ fontSize: 22, fontWeight: 700, color: colorByPct(portfolio?.unrealized_pnl) }}>
            {portfolio ? `¥${portfolio.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` : '-'}
          </div>
          <div style={{ fontSize: 12, color: '#888', marginTop: 4 }}>
            市值 {portfolio ? '¥' + portfolio.market_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '-'}
          </div>
        </Card>
        <Card title="风控状态">
          <div style={{ fontSize: 15, fontWeight: 600 }}>
            {risk ? (
              <>
                <Tag color={risk.day_loss_tripped ? '#dc2626' : '#16a34a'}>
                  {risk.day_loss_tripped ? '日亏损熔断' : '风控正常'}
                </Tag>{' '}
                {risk.defense_mode && <Tag color="#ea580c">防守模式</Tag>}
              </>
            ) : '-'}
          </div>
          <div style={{ fontSize: 12, color: '#888', marginTop: 6 }}>连亏 {risk?.consecutive_losses ?? '-'} 笔</div>
        </Card>
        <Card title="今日信号">
          <div style={{ fontSize: 22, fontWeight: 700 }}>{signals.length > 0 ? signals.length : 0}</div>
          <div style={{ fontSize: 12, color: '#888', marginTop: 4 }}>最近 {signals.length ? signals[0].time : '-'}</div>
        </Card>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <Card title="最近信号">
          {signals.length === 0 ? (
            <div style={{ color: '#999', fontSize: 13 }}>暂无信号。在「信号中心」输入代码评估,或等待定时扫描。</div>
          ) : (
            signals.map((s) => (
              <div key={s.id} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                <span>
                  {s.symbol} <span style={{ color: '#888' }}>{s.name}</span>
                </span>
                <span>
                  <Tag color={s.type.includes('SELL') ? '#16a34a' : '#dc2626'}>{s.type.replace('_', ' ')}</Tag>
                  <span style={{ marginLeft: 8, color: '#666' }}>强度 {s.strength.toFixed(0)}</span>
                </span>
              </div>
            ))
          )}
        </Card>
        <Card title="持仓明细">
          {!portfolio || portfolio.positions.length === 0 ? (
            <div style={{ color: '#999', fontSize: 13 }}>暂无持仓。到「自选与持仓」页录入。</div>
          ) : (
            portfolio.positions.map((p) => (
              <div key={p.symbol} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                <span>
                  {p.symbol} <span style={{ color: '#888' }}>{p.name}</span>
                </span>
                <span>
                  <span style={{ color: colorByPct(p.unrealized_pct) }}>{fmtPct(p.unrealized_pct)}</span>
                  <span style={{ marginLeft: 8, color: '#666' }}>{p.qty} 股</span>
                </span>
              </div>
            ))
          )}
        </Card>
      </div>
    </div>
  )
}
