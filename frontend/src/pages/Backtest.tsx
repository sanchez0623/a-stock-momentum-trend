import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type BacktestFactorReport, type BacktestHoldStats } from '../api/client'
import { Button, Card, ErrorBox, Loading } from '../components/ui'
import { EquityChart } from '../components/charts/EquityChart'

// 阶段展示顺序(按风险递增)与配色: 红=利多/橙=需注意/绿=偏空
const STAGE_META: Record<string, { label: string; color: string }> = {
  launch: { label: '启动期', color: '#dc2626' },
  accelerate: { label: '加速期', color: '#dc2626' },
  overheat: { label: '过热期', color: '#ea580c' },
  exhaust: { label: '衰竭期', color: '#16a34a' },
  none: { label: '无趋势', color: '#64748b' },
}
const HOLD_LABELS: Record<string, string> = { hold_5: '5日', hold_10: '10日', hold_20: '20日' }

const ACTION_LABEL: Record<string, string> = {
  buy_first: '首仓', buy_add: '加仓', sell_reduce: '止盈减仓', sell_stop: '止损', t_sell: '做T高抛', t_buy: '做T低吸',
}

function fmt(v: number, suffix = '') {
  const s = v > 0 ? '+' : ''
  return `${s}${v.toFixed(2)}${suffix}`
}

function HoldCell({ s }: { s: BacktestHoldStats }) {
  const color = s.expectancy > 0 ? '#dc2626' : s.expectancy < 0 ? '#16a34a' : '#334155'
  return (
    <td className="px-3 py-2 align-top">
      <div className="text-[15px] font-bold" style={{ color }}>
        胜率 {s.win_rate.toFixed(1)}%
      </div>
      <div className="mt-0.5 text-[11px] leading-snug text-ink-muted">
        均值 {fmt(s.avg, '%')} · 中位 {fmt(s.median, '%')}
      </div>
      <div className="text-[11px] text-ink-faint">样本 {s.n}</div>
    </td>
  )
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="rounded-lg border border-line bg-white px-3 py-2">
      <div className="text-[11px] text-ink-muted">{label}</div>
      <div className="text-[16px] font-bold" style={color ? { color } : undefined}>{value}</div>
      {sub && <div className="text-[11px] text-ink-faint">{sub}</div>}
    </div>
  )
}

// ---------------------------------------------------------------- 策略回测标签
function StrategyTab() {
  const [taskId, setTaskId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [showTrades, setShowTrades] = useState(false)

  // 任务轮询: 触发后每 2s 拉取进度, 终态(done/error)自动停止
  const { data: task } = useQuery({
    queryKey: ['backtest-task', taskId ?? 'none'],
    queryFn: () => api.backtestTask(taskId!),
    enabled: taskId !== null,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false),
  })
  const running = taskId !== null && task?.status !== 'done' && task?.status !== 'error'
  const progress = task?.progress ?? 0
  const report = task?.status === 'done' ? task.result : null
  const err = error || (task?.status === 'error' ? task.error || '回测失败' : '')

  const run = async () => {
    setError('')
    setTaskId(null)
    try {
      const { task_id } = await api.backtestStrategy({ initial_capital: 1_000_000 })
      setTaskId(task_id)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const meta = report?.meta
  const stats = report?.stats
  const curve = report?.equity_curve?.map((p) => ({ time: p.date, equity: p.equity, pnl: 0 }))
  const winColor = (stats?.win_rate ?? 0) >= 50 ? '#dc2626' : '#16a34a'

  return (
    <div>
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={run} disabled={running}>
            {running ? `回测运行中 ${progress}%…` : '运行策略回测'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            股票池：自选 + 持仓 · 初始资金 100 万 · 全流程模拟（建仓/加仓/止盈/止损/做T + 风控三道闸门）
          </span>
        </div>
        {running && progress > 0 && (
          <div className="mt-3 h-1.5 w-full overflow-hidden rounded bg-divider">
            <div className="h-full bg-link transition-all" style={{ width: `${progress}%` }} />
          </div>
        )}
        {err && <div className="mt-3"><ErrorBox message={err} /></div>}
      </Card>

      {running && !report && (
        <Card className="p-6">
          <Loading text="正在逐日回放信号循环与组合撮合…" />
        </Card>
      )}
      {report && meta && stats && (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
            <StatCard label="总收益率" value={fmt(meta.total_return_pct, '%')}
              color={meta.total_return_pct >= 0 ? '#dc2626' : '#16a34a'} sub={`年化 ${fmt(meta.annual_return_pct, '%')}`} />
            <StatCard label="最大回撤" value={fmt(-meta.max_drawdown_pct, '%')} sub={`夏普 ${meta.sharpe.toFixed(2)}`} />
            <StatCard label="胜率(平仓)" value={`${stats.win_rate.toFixed(1)}%`} color={winColor}
              sub={`盈亏比 ${stats.profit_factor.toFixed(2)} · 期望 ${fmt(stats.expectancy)} 元`} />
            <StatCard label="做T 贡献" value={`${fmt(stats.t_contribution)} 元`}
              sub={`${stats.t_sell_count} 次高抛 · 总成交 ${stats.trades} 笔`} />
          </div>

          <Card className="mb-4 p-4">
            <div className="mb-2 flex items-baseline justify-between">
              <span className="text-[13px] font-semibold text-ink">净值曲线</span>
              <span className="text-[11px] text-ink-faint">
                {meta.pool} 只参与{meta.skipped > 0 ? ` · ${meta.skipped} 只无数据跳过` : ''} · {meta.days} 个交易日 ·
                {meta.final_equity.toLocaleString()} → {meta.final_equity >= meta.initial_capital ? '盈利' : '亏损'}
              </span>
            </div>
            <EquityChart curve={curve ?? []} />
            <div className="mt-2 text-[11px] leading-relaxed text-ink-faint">{meta.notes}</div>
          </Card>

          <Card className="overflow-x-auto p-2">
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-[13px] font-semibold text-ink">交易明细（{report.trades.length} 笔）</span>
              <button className="text-[12px] text-link hover:underline" onClick={() => setShowTrades(!showTrades)}>
                {showTrades ? '收起' : '展开'}
              </button>
            </div>
            {showTrades && (
              <table className="w-full border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-ink-muted">
                    <th className="px-3 py-1.5">日期</th>
                    <th className="px-3 py-1.5">代码</th>
                    <th className="px-3 py-1.5">动作</th>
                    <th className="px-3 py-1.5 text-right">价格</th>
                    <th className="px-3 py-1.5 text-right">数量</th>
                    <th className="px-3 py-1.5 text-right">盈亏</th>
                    <th className="px-3 py-1.5">信号理由</th>
                  </tr>
                </thead>
                <tbody>
                  {report.trades.slice(-60).reverse().map((t, i) => (
                    <tr key={i} className="border-t border-divider">
                      <td className="px-3 py-1.5 whitespace-nowrap">{t.date}</td>
                      <td className="px-3 py-1.5 font-semibold">{t.symbol}</td>
                      <td className="px-3 py-1.5">{ACTION_LABEL[t.action] ?? t.action}</td>
                      <td className="px-3 py-1.5 text-right">{t.price.toFixed(2)}</td>
                      <td className="px-3 py-1.5 text-right">{t.qty}</td>
                      <td className="px-3 py-1.5 text-right" style={{ color: t.pnl > 0 ? '#dc2626' : t.pnl < 0 ? '#16a34a' : undefined }}>
                        {t.pnl === 0 ? '-' : fmt(t.pnl)}
                      </td>
                      <td className="px-3 py-1.5 text-ink-faint">{t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          点击上方按钮，用自选+持仓的历史行情跑一次真实交易循环回测（建仓/加仓/止盈/止损/做T）
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 阶段分桶标签
function FactorTab() {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [report, setReport] = useState<BacktestFactorReport | null>(null)

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const r = await api.backtestFactor({ hold_days: [5, 10, 20], cost: true })
      setReport(r)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setRunning(false)
    }
  }

  const stages = report ? Object.entries(report.by_stage) : []
  const distTotal = report ? Object.values(report.stage_distribution).reduce((a, b) => a + b, 0) : 0

  return (
    <div>
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={run} disabled={running}>
            {running ? '回测运行中…' : '运行阶段分桶回测'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            数据源：本地 K 线缓存（盘后预热落库），全市场约 {report?.meta.symbols_total ?? '3600+'} 只 · 耗时约 1 分钟
          </span>
        </div>
        {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      </Card>

      {running && (
        <Card className="p-6">
          <Loading text="正在逐日回放阶段判定与收益统计…" />
        </Card>
      )}

      {report && !running && (
        <>
          <Card className="mb-4 p-3 text-[12px] leading-relaxed text-ink-muted">
            参与股票 <b className="text-ink">{report.meta.symbols_used}</b> / {report.meta.symbols_total} 只
            {report.meta.date_from && report.meta.date_to && (
              <> · 信号日期 {report.meta.date_from} ~ {report.meta.date_to}</>
            )}
            {' '}· {report.meta.cost_included ? '已扣双边手续费' : '未扣费'}
          </Card>

          <Card className="mb-4 p-4">
            <div className="mb-2 text-[13px] font-semibold text-ink">信号阶段分布（全部信号日）</div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(report.stage_distribution)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => {
                  const m = STAGE_META[k] ?? { label: k, color: '#64748b' }
                  const pct = distTotal ? (v / distTotal) * 100 : 0
                  return (
                    <div key={k} className="rounded-lg border border-line bg-white px-3 py-1.5 text-[12px]">
                      <span className="font-semibold" style={{ color: m.color }}>{m.label}</span>
                      <span className="ml-1.5 text-ink-muted">{v} 次 · {pct.toFixed(1)}%</span>
                    </div>
                  )
                })}
            </div>
          </Card>

          <Card className="overflow-x-auto p-2">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-ink-muted">
                  <th className="px-3 py-2">阶段</th>
                  {report.meta.hold_days.map((h) => (
                    <th key={h} className="px-3 py-2 text-right">{HOLD_LABELS[`hold_${h}`] ?? `${h}日`}持有</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stages.map(([k, info]) => {
                  const m = STAGE_META[k] ?? { label: k, color: '#64748b' }
                  return (
                    <tr key={k} className="border-t border-divider">
                      <td className="px-3 py-2">
                        <div className="text-[14px] font-semibold" style={{ color: m.color }}>{info.label}</div>
                      </td>
                      {report.meta.hold_days.map((h) => {
                        const s = info.holds[`hold_${h}`]
                        return s ? <HoldCell key={h} s={s} /> : <td key={h} className="px-3 py-2 text-ink-faint">-</td>
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Card>

          <Card className="mt-4 p-3 text-[12px] leading-relaxed text-ink-muted">
            <b className="text-ink">怎么看：</b>
            胜率为正收益比例，均值/中位数为净收益率（%）。优先看 <b className="text-ink">20 日期望</b>：
            正期望越大越适合当前阶段买入持有；无趋势/衰竭期期望为负时应回避。
            样本区间覆盖较短（多数股票仅近几个月），结论仅供参考，建议持续回测积累。
          </Card>
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          点击上方按钮，用本地缓存历史数据运行阶段分桶回测
        </Card>
      )}
    </div>
  )
}

export default function Backtest() {
  const [tab, setTab] = useState<'factor' | 'strategy'>('strategy')

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4">
        <h1 className="text-[20px] font-semibold">回测中心</h1>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
          两种回测口径：<b className="text-ink">策略回测</b>（真实交易循环：建仓/加仓/止盈/止损/做T + 风控，推荐）与
          <b className="text-ink">阶段分桶</b>（各阶段买入持有 N 日的胜率/期望统计）。
        </p>
      </div>

      <div className="mb-3 flex gap-1.5">
        {([
          ['strategy', '策略回测（真实循环）'],
          ['factor', '阶段分桶（胜率统计）'],
        ] as const).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`rounded px-3 py-1.5 text-[13px] ${
              tab === k ? 'bg-link text-white' : 'text-ink hover:bg-divider'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'strategy' ? <StrategyTab /> : <FactorTab />}
    </div>
  )
}
