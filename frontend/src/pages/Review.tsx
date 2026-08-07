import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { EquityPoint, MonthStat, SignalDistItem, StatsSummary, TradeScore } from '../api/client'
import { colorByPct } from '../const/colors'
import { Card, ErrorBox, Loading, SIGNAL_META } from '../components/ui'

export default function Review() {
  const [summary, setSummary] = useState<StatsSummary | null>(null)
  const [curve, setCurve] = useState<EquityPoint[]>([])
  const [months, setMonths] = useState<MonthStat[]>([])
  const [signals, setSignals] = useState<SignalDistItem[]>([])
  const [scores, setScores] = useState<{ items: TradeScore[]; health: number } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.statsSummary().then(setSummary).catch((e) => setError(String(e.message || e)))
    api.statsEquityCurve().then((d) => setCurve(d.curve)).catch(() => {})
    api.statsMonthly().then((d) => setMonths(d.months)).catch(() => {})
    api.statsSignals().then((d) => setSignals(d.items)).catch(() => {})
    api.statsScores().then(setScores).catch(() => {})
  }, [])

  if (error && !summary) return <ErrorBox message={'加载失败: ' + error} />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>历史回顾</h1>
      <div style={{ color: '#888', fontSize: 12, marginBottom: 16 }}>基于已平仓成交(卖出)记录统计</div>

      {/* 健康度 */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 16, alignItems: 'center' }}>
        <Card title="交易健康度" style={{ width: 220 }}>
          <div style={{ fontSize: 30, fontWeight: 700, color: healthColor(scores?.health ?? 50) }}>
            {scores ? scores.health : '-'}
          </div>
          <div style={{ fontSize: 12, color: '#888', marginTop: 4 }}>{healthComment(scores?.health ?? 50)}</div>
        </Card>
        <Card title="关键指标">
          {summary ? (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(100px, 1fr))', gap: 12, fontSize: 13 }}>
              <Metric label="总交易" value={String(summary.trades)} />
              <Metric label="胜率" value={summary.win_rate + '%'} color={colorByPct(summary.win_rate - 50)} />
              <Metric label="总盈亏" value={(summary.total_pnl >= 0 ? '+' : '') + summary.total_pnl.toFixed(0)} color={colorByPct(summary.total_pnl)} />
              <Metric label="盈亏比" value={String(summary.profit_factor)} />
              <Metric label="单笔期望" value={summary.expectancy.toFixed(1)} color={colorByPct(summary.expectancy)} />
              <Metric label="最大连亏" value={String(summary.max_consecutive_losses)} color={summary.max_consecutive_losses >= 3 ? '#ea580c' : '#333'} />
              <Metric label="平均盈利" value={summary.avg_win.toFixed(0)} color="#dc2626" />
              <Metric label="平均亏损" value={summary.avg_loss.toFixed(0)} color="#16a34a" />
            </div>
          ) : <Loading text="暂无平仓记录" />}
        </Card>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: 16, marginBottom: 16 }}>
        <Card title="盈亏曲线(已实现累计)">
          {curve.length > 1 ? <EquityChart curve={curve} /> : <Loading text="暂无平仓记录, 卖出后出现曲线" />}
        </Card>
        <Card title="月度热力图">
          {months.length === 0 ? (
            <Loading text="暂无月度数据" />
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 8 }}>
              {months.map((m) => (
                <div key={m.month} style={{ border: '1px solid #e5e6eb', borderRadius: 8, padding: 10, background: monthBg(m.pnl) }}>
                  <div style={{ fontSize: 12, color: '#666' }}>{m.month}</div>
                  <div style={{ fontSize: 16, fontWeight: 700, color: colorByPct(m.pnl) }}>
                    {m.pnl >= 0 ? '+' : ''}{m.pnl.toFixed(0)}
                  </div>
                  <div style={{ fontSize: 11, color: '#999' }}>{m.trades} 笔 · 胜率 {m.win_rate}%</div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 3fr', gap: 16 }}>
        <Card title="信号分布">
          {signals.every((s) => s.count === 0) ? (
            <Loading text="暂无信号记录" />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {signals.filter((s) => s.count > 0).map((s) => {
                const meta = SIGNAL_META[s.type] || { label: s.type, color: '#333' }
                const max = Math.max(1, ...signals.map((x) => x.count))
                return (
                  <div key={s.type} style={{ fontSize: 13 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
                      <span style={{ color: meta.color, fontWeight: 500 }}>{meta.label}</span>
                      <span style={{ color: '#666' }}>{s.count}</span>
                    </div>
                    <div style={{ background: '#f0f1f3', borderRadius: 4, height: 8 }}>
                      <div style={{ width: `${(s.count / max) * 100}%`, background: meta.color, height: 8, borderRadius: 4 }} />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </Card>
        <Card title="单笔评分">
          {!scores || scores.items.length === 0 ? (
            <Loading text="平仓后自动评分" />
          ) : (
            <div style={{ maxHeight: 320, overflowY: 'auto' }}>
              {scores.items.slice().reverse().map((t) => (
                <div key={t.id} style={{ display: 'flex', justifyContent: 'space-between', padding: '7px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                  <span>{t.time.slice(5, 16)} · {t.symbol} <span style={{ color: '#aaa' }}>{t.name}</span></span>
                  <span style={{ color: colorByPct(t.pnl), fontWeight: 600 }}>{t.pnl >= 0 ? '+' : ''}{t.pnl.toFixed(0)}</span>
                  <span style={{ fontWeight: 700, color: scoreColor(t.score) }}>{t.score}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 子组件
function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: '#999', marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color: color || '#333' }}>{value}</div>
    </div>
  )
}

function EquityChart({ curve }: { curve: EquityPoint[] }) {
  const W = 560, H = 220, PAD = 30
  const values = curve.map((p) => p.equity)
  const min = Math.min(...values, 0), max = Math.max(...values, 0)
  const span = max - min || 1
  const x = (i: number) => PAD + (i / (curve.length - 1)) * (W - PAD * 2)
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2)
  const path = curve.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ')
  const zeroY = y(0)
  const color = curve[curve.length - 1].equity >= 0 ? '#dc2626' : '#16a34a'
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }}>
      <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="#e5e6eb" strokeDasharray="4 4" />
      <path d={path} fill="none" stroke={color} strokeWidth={2} />
      <text x={W - PAD} y={zeroY - 4} textAnchor="end" fontSize={11} fill="#999">0</text>
      <text x={PAD} y={PAD + 8} fontSize={11} fill="#999">+{max.toFixed(0)}</text>
      <text x={PAD} y={H - PAD - 6} fontSize={11} fill="#999">{min.toFixed(0)}</text>
      <text x={PAD} y={H - 8} fontSize={11} fill="#bbb">{curve[0].time.slice(0, 10)}</text>
      <text x={W - PAD} y={H - 8} textAnchor="end" fontSize={11} fill="#bbb">{curve[curve.length - 1].time.slice(0, 10)}</text>
    </svg>
  )
}

function healthColor(v: number) {
  return v >= 70 ? '#dc2626' : v >= 50 ? '#ea580c' : '#16a34a'
}
function healthComment(v: number) {
  return v >= 70 ? '状态优秀, 保持节奏' : v >= 50 ? '及格线, 注意纪律' : '需要复盘调整策略'
}
function scoreColor(v: number) {
  return v >= 70 ? '#16a34a' : v >= 50 ? '#ea580c' : '#dc2626'
}
function monthBg(pnl: number) {
  return pnl > 0 ? '#fef2f2' : pnl < 0 ? '#f0fdf4' : '#fafafa'
}
