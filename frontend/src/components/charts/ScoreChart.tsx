import type { ScorePoint } from '../../api/client'

// 得分追踪双轴图: 得分(左轴) + 价格(右轴) 手写 SVG, 无依赖
// 阶段色块背景 + 信号标记(卖出=绿点/买入=红点)
export function ScoreChart({ points }: { points: ScorePoint[] }) {
  if (points.length < 2) {
    return <div className="py-6 text-center text-xs text-ink-faint">采样点不足, 暂无法绘图(至少 2 个采样点)</div>
  }
  const W = 640, H = 240, PAD = 34, RIGHT = 52
  const scores = points.map((p) => p.score)
  const prices = points.map((p) => p.price)
  const sMin = Math.min(...scores), sMax = Math.max(...scores)
  const pMin = Math.min(...prices), pMax = Math.max(...prices)
  const sSpan = sMax - sMin || 1
  const pSpan = pMax - pMin || 1
  const x = (i: number) => PAD + (i / (points.length - 1)) * (W - PAD - RIGHT - PAD)
  const yS = (v: number) => H - PAD - ((v - sMin) / sSpan) * (H - PAD * 2)
  const yP = (v: number) => H - PAD - ((v - pMin) / pSpan) * (H - PAD * 2)
  const pathS = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${yS(p.score).toFixed(1)}`).join(' ')
  const pathP = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${yP(p.price).toFixed(1)}`).join(' ')

  // 阶段色块(启动/加速=红, 过热=橙, 衰竭=绿)
  const STAGE_BG: Record<string, string> = { launch: '#fef2f2', accelerate: '#fee2e2', overheat: '#fff7ed', exhaust: '#f0fdf4' }
  const stageRects: { from: number; to: number; color: string; label: string }[] = []
  let cur: { color: string; label: string; from: number } | null = null
  points.forEach((p, i) => {
    const color = STAGE_BG[p.stage] ?? 'transparent'
    const label = p.stage || ''
    if (!cur || cur.color !== color) {
      if (cur && cur.color !== 'transparent') stageRects.push({ ...cur, to: x(i - 1) })
      cur = { color, label, from: x(i) }
    } else if (cur.color !== 'transparent') {
      cur.label = label
    }
    if (i === points.length - 1 && cur && cur.color !== 'transparent') stageRects.push({ ...cur, to: x(i) })
  })

  const last = points[points.length - 1]
  const first = points[0]
  const chg = first.price > 0 ? ((last.price - first.price) / first.price) * 100 : 0
  const color = chg >= 0 ? '#dc2626' : '#16a34a'

  return (
    <div>
      <div className="mb-1 flex items-center gap-3 text-[11px] text-ink-muted">
        <span>追踪起: {first.time.slice(0, 16)}</span>
        <span>最新: {last.time.slice(0, 16)}</span>
        <span style={{ color }}>累计 {chg >= 0 ? '+' : ''}{chg.toFixed(1)}%</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }}>
        {/* 阶段背景色块 */}
        {stageRects.map((r, i) => (
          <rect key={i} x={r.from} y={PAD * 0.6} width={r.to - r.from} height={H - PAD * 1.6} fill={r.color} opacity={0.5} />
        ))}
        {/* 得分折线(左轴) */}
        <path d={pathS} fill="none" stroke="#2563eb" strokeWidth={2} />
        <text x={PAD} y={PAD * 0.5} fontSize={11} fill="#2563eb">得分 {sMax.toFixed(0)}</text>
        <text x={PAD} y={H - PAD * 0.5} fontSize={11} fill="#93a3b8">{sMin.toFixed(0)}</text>
        {/* 价格折线(右轴) */}
        <path d={pathP} fill="none" stroke={color} strokeWidth={2} strokeDasharray="5 3" />
        <text x={W - RIGHT} y={PAD * 0.5} textAnchor="end" fontSize={11} fill="#999">价 {pMax.toFixed(2)}</text>
        <text x={W - RIGHT} y={H - PAD * 0.5} textAnchor="end" fontSize={11} fill="#bbb">{pMin.toFixed(2)}</text>
        {/* 信号标记: 卖出绿/买入红 */}
        {points.map((p, i) => {
          if (!p.signal_type) return null
          const sell = p.signal_type.startsWith('SELL') || p.signal_type.startsWith('T_SELL')
          return (
            <g key={i}>
              <circle cx={x(i)} cy={yP(p.price)} r={4} fill={sell ? '#16a34a' : '#dc2626'} stroke="#fff" strokeWidth={1.5} />
              <text x={x(i)} y={yP(p.price) - 7} textAnchor="middle" fontSize={9} fill={sell ? '#16a34a' : '#dc2626'}>
                {p.signal_type.replace('_', ' ')}
              </text>
            </g>
          )
        })}
        {/* 时间轴 */}
        <text x={PAD} y={H - 6} fontSize={10} fill="#bbb">{points[0].time.slice(5, 16)}</text>
        <text x={W - RIGHT} y={H - 6} textAnchor="end" fontSize={10} fill="#bbb">{points[points.length - 1].time.slice(5, 16)}</text>
      </svg>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-ink-faint">
        <span><span className="text-[#2563eb]">—</span> 得分(左轴)</span>
        <span><span style={{ color }}>----</span> 价格(右轴)</span>
        <span><span className="text-rise">●</span> 买入信号</span>
        <span><span className="text-fall">●</span> 卖出信号</span>
        <span>背景色 = 阶段(启动/加速/过热/衰竭)</span>
      </div>
    </div>
  )
}
