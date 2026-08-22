import type { EquityPoint } from '../../api/client'

export interface ChartSeries {
  key: string
  label: string
  color: string
  points: EquityPoint[]  // time + value(收益率%, 调用方已归一)
}

// 多序列收益率对比图(持仓回测三线 + 基准): 手写轻量 SVG, 无图表库依赖
export function MultiLineChart({ series, height = 240 }: { series: ChartSeries[]; height?: number }) {
  const W = 640
  const H = height
  const PAD = 34
  const usable = series.filter((s) => s.points.length >= 2)
  if (usable.length === 0) {
    return <div className="py-10 text-center text-[12px] text-ink-faint">暂无曲线数据</div>
  }
  // 统一时间轴(取最长序列), 缺失值沿用上一值(对齐展示); 过滤非法 time 防白屏
  const times = [...new Set(usable.flatMap((s) => s.points.map((p) => p.time).filter((t): t is string => typeof t === 'string' && t.length > 0)))].sort()
  if (times.length < 2) {
    return <div className="py-10 text-center text-[12px] text-ink-faint">暂无有效曲线数据</div>
  }
  const valAt = (s: ChartSeries, t: string) => {
    const hit = s.points.find((p) => p.time === t)
    return hit ? hit.equity : NaN
  }
  const allVals = usable.flatMap((s) => s.points.map((p) => p.equity))
  const min = Math.min(...allVals, 0)
  const max = Math.max(...allVals, 0)
  const span = max - min || 1
  const x = (i: number) => PAD + (i / (times.length - 1)) * (W - PAD * 2)
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2)
  const zeroY = y(0)

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }}>
        <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="#e5e6eb" strokeDasharray="4 4" />
        <text x={W - PAD} y={zeroY - 4} textAnchor="end" fontSize={11} fill="#999">0%</text>
        <text x={PAD} y={PAD + 8} fontSize={11} fill="#999">+{max.toFixed(1)}%</text>
        <text x={PAD} y={H - PAD - 6} fontSize={11} fill="#999">{min.toFixed(1)}%</text>
        <text x={PAD} y={H - 8} fontSize={11} fill="#bbb">{times[0].slice(0, 10)}</text>
        <text x={W - PAD} y={H - 8} textAnchor="end" fontSize={11} fill="#bbb">{times[times.length - 1].slice(0, 10)}</text>
        {usable.map((s) => {
          let prev = 0
          const path = times.map((t, i) => {
            const v = valAt(s, t)
            const vv = Number.isNaN(v) ? prev : v
            prev = vv
            return `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(vv).toFixed(1)}`
          }).join(' ')
          return <path key={s.key} d={path} fill="none" stroke={s.color} strokeWidth={s.key === 'signal' ? 2.4 : 1.6}
            strokeDasharray={s.key === 'benchmark' ? '5 4' : undefined} opacity={s.key === 'hold' ? 0.75 : 1} />
        })}
      </svg>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
        {usable.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <span className="inline-block h-[3px] w-4 rounded" style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  )
}
