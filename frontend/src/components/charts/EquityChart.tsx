import type { EquityPoint } from '../../api/client'

// 盈亏曲线 SVG 图: 手写轻量实现(无图表库依赖)
export function EquityChart({ curve }: { curve: EquityPoint[] }) {
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
