import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { HealthData, Portfolio, RiskStatus, SignalRecord } from '../api/client'
import { fmtPct } from '../const/colors'
import { Card, ErrorBox, ListRow, StatCard, Tag } from '../components/ui'

const POLL_MS = 15_000

export default function Dashboard() {
  const { data: health, error: healthError } = useQuery<HealthData>({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: POLL_MS,
  })
  const { data: portfolio } = useQuery<Portfolio>({
    queryKey: ['positions'],
    queryFn: api.positions,
    refetchInterval: POLL_MS,
  })
  const { data: risk } = useQuery<RiskStatus>({
    queryKey: ['risk-status'],
    queryFn: api.riskStatus,
    refetchInterval: POLL_MS,
  })
  const { data: signals = [] } = useQuery<SignalRecord[]>({
    queryKey: ['signals', 'recent'],
    queryFn: () => api.signals(undefined, 5),
    refetchInterval: POLL_MS,
  })

  // 仅 health 失败且无数据时整页报错(保持原逻辑)
  if (healthError && !health) return <ErrorBox message={'后端连接失败: ' + String((healthError as Error).message || healthError)} />

  const sourcesOk = health ? health.data_sources.filter((s) => !s.circuit_open).length : 0
  const sourcesTotal = health ? health.data_sources.length : 0

  return (
    <div>
      <h1 className="mb-1 text-[20px] font-semibold">仪表盘</h1>
      <div className="mb-4 text-xs text-ink-muted">
        {health ? `${health.date} · 时区 ${health.tz} · ${health.status === 'up' ? '运行中' : health.status}` : '加载中...'}
      </div>

      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        <StatCard
          title="系统状态"
          value={health ? (health.status === 'up' ? '运行中' : health.status) : '-'}
          valueClassName={health?.status === 'up' ? 'text-fall' : 'text-rise'}
          sub={`数据源 ${health ? `${sourcesOk}/${sourcesTotal} 个可用` : '-'}`}
        />
        <StatCard
          title="持仓盈亏"
          value={portfolio ? `¥${portfolio.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` : '-'}
          valueClassName={colorCls(portfolio?.unrealized_pnl)}
          sub={`市值 ${portfolio ? '¥' + portfolio.market_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '-'}`}
        />
        <StatCard
          title="风控状态"
          value={
            risk ? (
              <>
                <Tag color={risk.day_loss_tripped ? '#dc2626' : '#16a34a'}>
                  {risk.day_loss_tripped ? '日亏损熔断' : '风控正常'}
                </Tag>{' '}
                {risk.defense_mode && <Tag color="#ea580c">防守模式</Tag>}
              </>
            ) : '-'
          }
          valueClassName="text-[15px] font-semibold"
          sub={`连亏 ${risk?.consecutive_losses ?? '-'} 笔`}
        />
        <StatCard
          title="今日信号"
          value={signals.length > 0 ? signals.length : 0}
          sub={`最近 ${signals.length ? signals[0].time : '-'}`}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card title="最近信号">
          {signals.length === 0 ? (
            <div className="text-[13px] text-ink-faint">暂无信号。在「信号中心」输入代码评估,或等待定时扫描。</div>
          ) : (
            signals.map((s) => (
              <ListRow key={s.id}>
                <span>
                  {s.symbol} <span className="text-ink-muted">{s.name}</span>
                </span>
                <span className="flex items-center gap-2">
                  <Tag color={s.type.includes('SELL') ? '#16a34a' : '#dc2626'}>{s.type.replace('_', ' ')}</Tag>
                  <span className="text-ink-secondary">强度 {s.strength.toFixed(0)}</span>
                </span>
              </ListRow>
            ))
          )}
        </Card>
        <Card title="持仓明细">
          {!portfolio || portfolio.positions.length === 0 ? (
            <div className="text-[13px] text-ink-faint">暂无持仓。到「自选与持仓」页录入。</div>
          ) : (
            portfolio.positions.map((p) => (
              <ListRow key={p.symbol}>
                <span>
                  {p.symbol} <span className="text-ink-muted">{p.name}</span>
                </span>
                <span className="flex items-center gap-2">
                  <span className={colorCls(p.unrealized_pct)}>{fmtPct(p.unrealized_pct)}</span>
                  <span className="text-ink-secondary">{p.qty} 股</span>
                </span>
              </ListRow>
            ))
          )}
        </Card>
      </div>
    </div>
  )
}

// 涨红跌绿(平灰)
function colorCls(pct: number | undefined | null): string {
  if (pct === undefined || pct === null || pct === 0) return 'text-ink-faint'
  return pct > 0 ? 'text-rise' : 'text-fall'
}
