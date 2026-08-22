import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { DailyReportRecord, HealthData, NotificationItem, Portfolio, RiskStatus, SignalRecord } from '../api/client'
import { fmtPct } from '../const/colors'
import { Button, Card, ErrorBox, ListRow, PageHeader, StatCard, Tag } from '../components/ui'

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
  const { data: report, refetch: refetchReport } = useQuery<DailyReportRecord | null>({
    queryKey: ['report-daily'],
    queryFn: () => api.reportDaily(),
    refetchInterval: POLL_MS,
  })
  const [generating, setGenerating] = useState(false)
  const queryClient = useQueryClient()
  const { data: notifications = [] } = useQuery<NotificationItem[]>({
    queryKey: ['notifications'],
    queryFn: () => api.notifications(8),
    refetchInterval: POLL_MS,
  })

  const markRead = async (id: number) => {
    try {
      await api.notificationRead(id)
      queryClient.invalidateQueries({ queryKey: ['notifications'] })
    } catch (e) {
      console.error('标记已读失败:', e)
    }
  }

  const generateReport = async () => {
    setGenerating(true)
    try {
      await api.reportDailyRun()
      refetchReport()
    } catch (e) {
      console.error('日报生成失败:', e)
    } finally {
      setGenerating(false)
    }
  }

  // 仅 health 失败且无数据时整页报错(保持原逻辑)
  if (healthError && !health) return <ErrorBox message={'后端连接失败: ' + String((healthError as Error).message || healthError)} />

  const sourcesOk = health ? health.data_sources.filter((s) => !s.circuit_open).length : 0
  const sourcesTotal = health ? health.data_sources.length : 0

  return (
    <div>
      <PageHeader
        title="仪表盘"
        subtitle={
          health ? `${health.date} · 时区 ${health.tz} · ${health.status === 'up' ? '运行中' : health.status}` : '加载中...'
        }
      />

      <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        <StatCard
          title="系统状态"
          value={health ? (health.status === 'up' ? '运行中' : health.status) : '-'}
          valueClassName={health?.status === 'up' ? 'text-fall' : 'text-rise'}
          sub={`数据源 ${health ? `${sourcesOk}/${sourcesTotal} 个可用` : '-'}`}
        />
        <StatCard
          title="持仓盈亏(含费)"
          value={portfolio ? `¥${portfolio.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` : '-'}
          valueClassName={colorCls(portfolio?.unrealized_pnl)}
          sub={
            portfolio
              ? `市值 ¥${portfolio.market_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} · 已摊入费用 ¥${portfolio.fee_cost.toFixed(2)}`
              : '市值 -'
          }
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

      {/* 今日日报(盘后 AI 日报, 只读) */}
      <Card title="今日日报" className="mt-3">
        {!report ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[13px] text-ink-faint">今日暂无日报。可手动生成验证, 或等每日 16:30 定时任务。</span>
            <Button kind="ghost" onClick={generateReport} disabled={generating} style={{ padding: '4px 10px', fontSize: 12 }} className="h-7">
              {generating ? '生成中...' : '生成日报'}
            </Button>
          </div>
        ) : (
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-medium">{report.date}</span>
              <Tag color={report.status === 'ok' ? '#16a34a' : '#d97706'}>
                {report.status === 'ok' ? `AI 生成${report.model ? ` · ${report.model}` : ''}` : '规则模板(未启用 LLM)'}
              </Tag>
              <Button kind="ghost" onClick={generateReport} disabled={generating} style={{ padding: '2px 8px', fontSize: 11 }} className="h-6">
                {generating ? '生成中...' : '重新生成'}
              </Button>
            </div>
            {report.status === 'ok' ? (
              <div className="space-y-2.5 text-[13px] leading-relaxed">
                <div>
                  <span className="font-medium">市况</span> {report.content.market_summary}
                </div>
                <div>
                  <span className="font-medium">今日操作</span> {report.content.trade_summary}
                </div>
                {report.content.holdings_review.length > 0 && (
                  <ReportList title="持仓点评" items={report.content.holdings_review} />
                )}
                {report.content.signals_today.length > 0 && (
                  <ReportList title="今日信号" items={report.content.signals_today} />
                )}
                {report.content.tomorrow_watch.length > 0 && (
                  <ReportList title="明日关注" items={report.content.tomorrow_watch} highlight />
                )}
                {report.content.risk_notes.length > 0 && (
                  <ReportList title="风险提示" items={report.content.risk_notes} />
                )}
                <div className="flex items-center gap-2">
                  <span className="font-medium">纪律评分</span>
                  <Tag color={report.content.discipline_score >= 60 ? '#16a34a' : '#dc2626'}>
                    {report.content.discipline_score}
                  </Tag>
                </div>
              </div>
            ) : (
              <pre className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink-secondary">
                {report.content.text}
              </pre>
            )}
          </div>
        )}
      </Card>

      <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-2">
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

      {/* 通知中心(AI 助理提醒 / 日报) */}
      <Card title="通知中心" className="mt-3">
        {notifications.length === 0 ? (
          <div className="text-[13px] text-ink-faint">暂无通知。开启「设置 → AI 助理」后,盘前观察/盘中信号提醒/盘后日报会自动推送。</div>
        ) : (
          <div className="space-y-1.5">
            {notifications.map((n) => (
              <div
                key={n.id}
                onClick={() => !n.read && markRead(n.id)}
                className={`flex items-center justify-between gap-2 rounded-md px-2 py-1.5 ${n.read ? '' : 'cursor-pointer hover:bg-ink-muted/5'}`}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {!n.read && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rise" />}
                  <span className="text-[13px]">
                    <span className="font-medium">{n.title}</span>
                    <span className="ml-2 text-ink-secondary">{n.content}</span>
                  </span>
                </span>
                <span className="shrink-0 text-[11px] text-ink-faint">{n.time.slice(5, 16)}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

// 涨红跌绿(平灰)
function colorCls(pct: number | undefined | null): string {
  if (pct === undefined || pct === null || pct === 0) return 'text-ink-faint'
  return pct > 0 ? 'text-rise' : 'text-fall'
}

// 日报小节列表
function ReportList({ title, items, highlight }: { title: string; items: string[]; highlight?: boolean }) {
  return (
    <div>
      <span className="font-medium">{title}</span>
      <ul className="mt-0.5 space-y-0.5">
        {items.map((it, i) => (
          <li key={i} className={highlight ? 'font-medium text-ink' : 'text-ink-secondary'}>
            · {it}
          </li>
        ))}
      </ul>
    </div>
  )
}
