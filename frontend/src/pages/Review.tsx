import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { colorByPct } from '../const/colors'
import { Card, ChartContainer, ErrorBox, Loading, PageHeader, SIGNAL_META, StatCard } from '../components/ui'
import { EquityChart } from '../components/charts/EquityChart'

export default function Review() {
  const { data: summary, error: summaryQueryError } = useQuery({
    queryKey: ['stats', 'summary'],
    queryFn: api.statsSummary,
  })
  const { data: curve = [] } = useQuery({
    queryKey: ['stats', 'curve'],
    queryFn: api.statsEquityCurve,
    select: (d) => d.curve,
  })
  const { data: months = [] } = useQuery({
    queryKey: ['stats', 'months'],
    queryFn: api.statsMonthly,
    select: (d) => d.months,
  })
  const { data: signals = [] } = useQuery({
    queryKey: ['stats', 'signals'],
    queryFn: api.statsSignals,
    select: (d) => d.items,
  })
  const { data: scores } = useQuery({
    queryKey: ['stats', 'scores'],
    queryFn: api.statsScores,
  })
  // 原逻辑: 仅 summary 失败且无数据时整页报错(其余图表失败静默降级)
  if (summaryQueryError && !summary) {
    return <ErrorBox message={'加载失败: ' + String((summaryQueryError as Error).message || summaryQueryError)} />
  }

  return (
    <div>
      <PageHeader title="历史回顾" subtitle="基于已平仓成交(卖出)记录统计" />

      {/* 健康度 + 关键指标 */}
      <div className="mb-4 flex flex-col gap-4 lg:flex-row lg:items-center">
        <StatCard
          title="交易健康度"
          className="lg:w-[220px]"
          value={scores && scores.health > 0 ? scores.health : '-'}
          valueClassName={`text-[30px] ${healthColor(scores?.health ?? 0)}`}
          sub={scores && scores.health > 0 ? healthComment(scores.health) : '暂无平仓交易, 完成一笔卖出后自动评估'}
        />
        <Card title="关键指标">
          {summary ? (
            <div className="grid grid-cols-[repeat(4,minmax(100px,1fr))] gap-3 text-[13px]">
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

      <div className="mb-4 grid grid-cols-1 gap-4 lg:grid-cols-[3fr_2fr]">
        <ChartContainer title="盈亏曲线(已实现累计)">
          {curve.length > 1 ? <EquityChart curve={curve} /> : <Loading text="暂无平仓记录, 卖出后出现曲线" />}
        </ChartContainer>
        <ChartContainer title="月度热力图">
          {months.length === 0 ? (
            <Loading text="暂无月度数据" />
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-2">
              {months.map((m) => (
                <div key={m.month} className="rounded-lg border border-line p-2.5" style={{ background: monthBg(m.pnl) }}>
                  <div className="text-xs text-ink-secondary">{m.month}</div>
                  <div className={`text-[16px] font-bold ${colorByPct(m.pnl)}`}>
                    {m.pnl >= 0 ? '+' : ''}{m.pnl.toFixed(0)}
                  </div>
                  <div className="text-[11px] text-ink-faint">{m.trades} 笔 · 胜率 {m.win_rate}%</div>
                </div>
              ))}
            </div>
          )}
        </ChartContainer>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_3fr]">
        <ChartContainer title="信号分布">
          {signals.every((s) => s.count === 0) ? (
            <Loading text="暂无信号记录" />
          ) : (
            <div className="flex flex-col gap-2">
              {signals.filter((s) => s.count > 0).map((s) => {
                const meta = SIGNAL_META[s.type] || { label: s.type, color: '#333' }
                const max = Math.max(1, ...signals.map((x) => x.count))
                return (
                  <div key={s.type} className="text-[13px]">
                    <div className="mb-0.5 flex justify-between">
                      <span style={{ color: meta.color }} className="font-medium">{meta.label}</span>
                      <span className="text-ink-secondary">{s.count}</span>
                    </div>
                    <div className="h-2 rounded bg-divider">
                      <div className="h-2 rounded" style={{ width: `${(s.count / max) * 100}%`, background: meta.color }} />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </ChartContainer>
        <ChartContainer title="单笔评分">
          {!scores || scores.items.length === 0 ? (
            <Loading text="平仓后自动评分" />
          ) : (
            <div className="max-h-80 overflow-y-auto">
              {scores.items.slice().reverse().map((t) => (
                <div key={t.id} className="flex justify-between border-b border-divider py-[7px] text-[13px] last:border-b-0">
                  <span>{t.time.slice(5, 16)} · {t.symbol} <span className="text-ink-faint">{t.name}</span></span>
                  <span className={`font-semibold ${colorByPct(t.pnl)}`}>{t.pnl >= 0 ? '+' : ''}{t.pnl.toFixed(0)}</span>
                  <span className={`font-bold ${scoreColor(t.score)}`}>{t.score}</span>
                </div>
              ))}
            </div>
          )}
        </ChartContainer>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 子组件
function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="mb-0.5 text-[11px] text-ink-faint">{label}</div>
      <div className="text-[16px] font-bold" style={{ color: color || '#333' }}>{value}</div>
    </div>
  )
}

function healthColor(v: number) {
  return v >= 70 ? 'text-rise' : v >= 50 ? 'text-orange-500' : 'text-fall'
}
function healthComment(v: number) {
  return v >= 70 ? '状态优秀, 保持节奏' : v >= 50 ? '及格线, 注意纪律' : '需要复盘调整策略'
}
function scoreColor(v: number) {
  return v >= 70 ? 'text-fall' : v >= 50 ? 'text-orange-500' : 'text-rise'
}
function monthBg(pnl: number) {
  return pnl > 0 ? '#fef2f2' : pnl < 0 ? '#f0fdf4' : '#fafafa'
}
