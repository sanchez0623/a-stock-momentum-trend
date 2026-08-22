import { useEffect, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BacktestPreset,
  type BacktestFactorReport,
  type BacktestHoldStats,
  type BacktestDataStatus,
  type CompareVariantResult,
  type PortfolioBacktestReport,
  type SignalAuditReport,
} from '../api/client'
import { Button, Card, ConfirmDialog, ErrorBox, Loading, PageHeader, Table, Td, Th, cn } from '../components/ui'
import { MultiLineChart, type ChartSeries } from '../components/charts/MultiLineChart'

// 阶段展示顺序(按风险递增)与配色: 红=利多/橙=需注意/绿=偏空
const STAGE_META: Record<string, { label: string; color: string }> = {
  launch: { label: '启动期', color: '#dc2626' },
  accelerate: { label: '加速期', color: '#dc2626' },
  overheat: { label: '过热期', color: '#ea580c' },
  exhaust: { label: '衰竭期', color: '#16a34a' },
  none: { label: '无趋势', color: '#64748b' },
}
const HOLD_LABELS: Record<string, string> = { hold_5: '5日', hold_10: '10日', hold_20: '20日' }

// 总分分桶配色: 低分灰 -> 高分深红(分数越高颜色越重)
const SCORE_META: Record<string, { label: string; color: string }> = {
  '0-40': { label: '0-40分', color: '#64748b' },
  '40-50': { label: '40-50分', color: '#0369a1' },
  '50-60': { label: '50-60分', color: '#d97706' },
  '60-70': { label: '60-70分', color: '#dc2626' },
  '70+': { label: '70分以上', color: '#b91c1c' },
}

const ACTION_LABEL: Record<string, string> = {
  buy_entry: '建仓腿', buy_first: '首仓', buy_add: '加仓', sell_reduce: '止盈减仓', sell_stop: '止损', t_sell: '做T高抛', t_buy: '做T低吸',
}

const ATTR_LABEL: Record<string, string> = {
  sell_stop: '止损', sell_reduce: '止盈减仓', t_sell: '做T高抛', t_buy: '做T低吸', buy_add: '加仓',
}

// ---------------------------------------------------------------- 手动建仓腿快捷输入
interface ManualLegRow {
  symbol: string
  entry_date: string
  cost: string
  qty: string
}

const RECENT_KEY = 'bt-recent-legs'
const RECENT_MAX = 5

interface RecentItem {
  time: string
  legs: { symbol: string; entry_date?: string; cost?: number; qty?: number }[]
}

function loadRecent(): RecentItem[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    const arr = raw ? (JSON.parse(raw) as RecentItem[]) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function saveRecent(legs: { symbol: string; entry_date?: string; cost?: number; qty?: number }[]) {
  const key = JSON.stringify(legs)
  const next = [{ time: new Date().toISOString().slice(0, 16).replace('T', ' '), legs }, ...loadRecent().filter((r) => JSON.stringify(r.legs) !== key)].slice(0, RECENT_MAX)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // localStorage 满/不可用: 忽略, 不影响回测
  }
}

// 批量粘贴解析: 支持 CSV / Tab / 分号分隔, 带表头自动跳过, 可含名称列
function parsePasteLegs(text: string): ManualLegRow[] {
  const HEADER = /代码|symbol|名称|name|日期|date|成本|cost|数量|qty/i
  const rows: ManualLegRow[] = []
  for (const line of text.split(/\r?\n/)) {
    const line_ = line.trim()
    if (!line_ || HEADER.test(line_)) continue
    const cols = line_.split(/[\t,，;；]+/).map((c) => c.trim()).filter(Boolean)
    if (cols.length < 3) continue
    // 兼容两种列序: [代码, 建仓日, 成本, 数量] 或 [代码, 名称, 建仓日, 成本, 数量]
    const isDate = (s?: string) => !!s && /^\d{4}-\d{2}-\d{2}/.test(s)
    const [c0, c1, c2, c3, c4] = cols
    const symbol = c0
    if (!symbol) continue
    if (isDate(c1)) {
      rows.push({ symbol, entry_date: c1, cost: c2 ?? '', qty: c3 ?? '' })
    } else {
      rows.push({ symbol, entry_date: c2 ?? '', cost: c3 ?? '', qty: c4 ?? '' })
    }
  }
  return rows
}

function fmt(v: number, suffix = '') {
  const s = v > 0 ? '+' : ''
  return `${s}${v.toFixed(2)}${suffix}`
}

function HoldCell({ s }: { s: BacktestHoldStats }) {
  const color = s.expectancy > 0 ? '#dc2626' : s.expectancy < 0 ? '#16a34a' : '#334155'
  return (
    <Td>
      <div className="text-[15px] font-bold" style={{ color }}>
        胜率 {s.win_rate.toFixed(1)}%
      </div>
      <div className="mt-0.5 text-[11px] leading-snug text-ink-muted">
        均值 {fmt(s.avg, '%')} · 中位 {fmt(s.median, '%')}
      </div>
      <div className="text-[11px] text-ink-faint">样本 {s.n}</div>
    </Td>
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

// ---------------------------------------------------------------- K线数据管理
function DataTab() {
  const [taskId, setTaskId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [warmupRunning, setWarmupRunning] = useState(false)
  const [warmupMsg, setWarmupMsg] = useState('')

  // 日线缓存统计(实盘+回测共用)
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ['kline-stats'],
    queryFn: () => api.klineStats(),
  })
  // 回测冻结快照状态
  const { data: snapshot, refetch: refetchSnapshot } = useQuery({
    queryKey: ['backtest-data-status'],
    queryFn: () => api.backtestDataStatus(),
  })
  // 补拉任务轮询(运行中每 3s; 结束后刷新统计)
  const { data: task } = useQuery({
    queryKey: ['backfill-task', taskId ?? 'latest'],
    queryFn: () => api.backfillStatus(taskId ?? undefined),
    refetchInterval: (query) => {
      const st = query.state.data?.status
      return st === 'running' || st === 'pending' ? 3000 : false
    },
  })
  const running = task?.status === 'running' || task?.status === 'pending'
  useEffect(() => {
    if (task?.status === 'done') refetchStats()
  }, [task?.status])  // eslint-disable-line react-hooks/exhaustive-deps

  const startBackfill = async () => {
    setError('')
    setWarmupMsg('')
    try {
      const { task_id } = await api.backfillStart()  // 增量: 只补 陈旧/不足/未缓存
      setTaskId(task_id)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const runWarmup = async (force = false) => {
    setWarmupRunning(true)
    setWarmupMsg('')
    setError('')
    try {
      const r = await api.backtestWarmup({ force })
      const meta = r.meta as { symbols?: number; rows?: number; source_breakdown?: Record<string, number> }
      setWarmupMsg(`预热完成: ${meta.symbols ?? 0} 只${meta.rows ? ` · 新增 ${meta.rows} 行` : ''}${meta.source_breakdown ? ` · 来源 ${JSON.stringify(meta.source_breakdown)}` : ''}`)
      refetchSnapshot()
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setWarmupRunning(false)
    }
  }

  const st = stats
  const staleTotal = (st?.stale ?? 0) + (st?.missing ?? 0)
  const freshColor = (st?.days_behind ?? 99) <= 3 ? '#16a34a' : (st?.days_behind ?? 99) <= 10 ? '#f59e0b' : '#dc2626'
  const result = task?.result

  return (
    <div>
      {/* 日线缓存(回测/实盘共用) */}
      <Card className="mb-4 p-4">
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-[13px] font-semibold text-ink">日线缓存（回测与实盘共用）</span>
          <span className="text-[11px] text-ink-faint">{st?.note}</span>
        </div>
        {st ? (
          <>
            <div className="my-3 grid grid-cols-2 gap-2 md:grid-cols-4">
              <StatCard label="已缓存" value={`${st.symbols} 只`}
                sub={`达标 ${st.ok} · 待更新 ${st.stale} · 无数据 ${st.missing}`} />
              <StatCard label="数据最新到" value={st.date_to || '—'}
                color={freshColor} sub={st.days_behind != null ? `落后 ${st.days_behind} 天` : ''} />
              <StatCard label="最早数据" value={st.date_from || '—'} sub={`目标 ${st.target} 根/只`} />
              <StatCard label="待补拉" value={`${staleTotal} 只`}
                color={staleTotal > 0 ? '#f59e0b' : '#16a34a'} sub="陈旧或未缓存" />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={startBackfill} disabled={running}>
                {running ? `补拉中 ${task?.progress ?? 0}%（${task?.done ?? 0}/${task?.total ?? '?'}）` : staleTotal > 0 ? `增量补拉（${staleTotal} 只待更新）` : '增量补拉（已是最新）'}
              </Button>
              <span className="text-[11px] text-ink-faint">
                只拉 陈旧/不足/未缓存 的票, 断点续跑幂等; 全市场约 10~25 分钟 · 五源 failover
              </span>
            </div>
            {running && (
              <div className="mt-3 h-1.5 w-full overflow-hidden rounded bg-divider">
                <div className="h-full bg-link transition-all" style={{ width: `${task?.progress ?? 0}%` }} />
              </div>
            )}
            {task?.status === 'done' && result && (
              <div className="mt-2 text-[11px] leading-relaxed text-ink-muted">
                上次补拉 {result.finished_at ?? ''}: 待补 {result.pending ?? 0} 只 → 成功 {result.done ?? 0}
                {result.insufficient?.length ? ` · 历史短已尽力 ${result.insufficient.length}` : ''}
                {result.failed?.length ? ` · 失败 ${result.failed.length}` : ''}
              </div>
            )}
            {task?.status === 'failed' && (
              <div className="mt-2 text-[11px] text-red-600">补拉失败: {task.error}</div>
            )}
          </>
        ) : (
          <Loading text="读取缓存统计…" />
        )}
      </Card>

      {/* 回测冻结快照(baostock 前复权) */}
      <Card className="mb-4 p-4">
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-[13px] font-semibold text-ink">回测冻结快照（前复权 · 3 年）</span>
          <span className="text-[11px] text-ink-faint">{snapshot?.note}</span>
        </div>
        {snapshot ? (
          <>
            <div className="my-3 grid grid-cols-2 gap-2 md:grid-cols-3">
              <StatCard label="快照股票数" value={`${(snapshot as BacktestDataStatus).symbols} 只`}
                sub={`复权口径: ${(snapshot as BacktestDataStatus).adjusts.join('/') || '—'}`} />
              <StatCard label="最近拉取" value={(snapshot as BacktestDataStatus).last_fetched_at?.slice(0, 16) || '未拉取'}
                sub="已拉日期冻结, 不随日常刷新" />
              <StatCard label="默认池" value="自选 + 持仓" sub="对比回测/持仓回测直接读共用日线缓存" />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={() => runWarmup(false)} disabled={warmupRunning}>
                {warmupRunning ? '预热中…' : '预热快照（增量）'}
              </Button>
              <button className="text-[11px] text-link hover:underline" disabled={warmupRunning} onClick={() => runWarmup(true)}>
                强制重拉
              </button>
              <span className="text-[11px] text-ink-faint">baostock 前复权 · 未覆盖日期自动补, 已拉日期不覆盖</span>
            </div>
            {warmupMsg && <div className="mt-2 text-[11px] text-ink-muted">{warmupMsg}</div>}
          </>
        ) : (
          <Loading text="读取快照状态…" />
        )}
      </Card>

      {error && <ErrorBox message={error} />}
    </div>
  )
}

// ---------------------------------------------------------------- 变体对比回测(消融实验)
// 预设变体(与后端 compare.PRESETS 同源): key 仅前端勾选用
const COMPARE_PRESETS: { key: string; label: string; cooldown_days: number; defense: string }[] = [
  { key: 'raw', label: '裸奔基线(无冷却·无防守)', cooldown_days: 0, defense: 'off' },
  { key: 'cool', label: '仅冷却10日', cooldown_days: 10, defense: 'off' },
  { key: 'soft', label: '仅软防守', cooldown_days: 0, defense: 'soft' },
  { key: 'full', label: '软防守+冷却10日(当前默认)', cooldown_days: 10, defense: 'soft' },
]
const DEFENSE_LABEL: Record<string, string> = { soft: '软防守', hard: '硬防守', off: '关闭' }
const COMPARE_COLORS = ['#94a3b8', '#0369a1', '#dc2626', '#f59e0b', '#16a34a', '#7c3aed']
// 选池范围(与选股中心同源): 选股池/板块/行业
const UNIVERSE_OPTIONS = [
  { value: 'all', label: '全A' },
  { value: 'hs300', label: '沪深300' },
  { value: 'zz500', label: '中证500' },
  { value: 'sz50', label: '上证50' },
  { value: 'hs300+zz500', label: '沪深300+中证500(≈中证800)' },
]
const BOARD_OPTIONS = [
  { value: '', label: '全部板块' },
  { value: 'main', label: '主板' },
  { value: 'chinext', label: '创业板' },
  { value: 'star', label: '科创板' },
  { value: 'bj', label: '北交所' },
]

// 回测区间快捷选项
function _fmtDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
const QUICK_RANGES: { label: string; range: () => [string, string] }[] = [
  { label: '最近一季', range: () => { const d = new Date(); d.setMonth(d.getMonth() - 3); return [_fmtDate(d), _fmtDate(new Date())] } },
  { label: '最近半年', range: () => { const d = new Date(); d.setMonth(d.getMonth() - 6); return [_fmtDate(d), _fmtDate(new Date())] } },
  { label: '最近一年', range: () => { const d = new Date(); d.setMonth(d.getMonth() - 12); return [_fmtDate(d), _fmtDate(new Date())] } },
  { label: '最近两年', range: () => { const d = new Date(); d.setMonth(d.getMonth() - 24); return [_fmtDate(d), _fmtDate(new Date())] } },
  { label: '今年', range: () => [`${new Date().getFullYear()}-01-01`, _fmtDate(new Date())] },
  { label: '去年', range: () => { const y = new Date().getFullYear() - 1; return [`${y}-01-01`, `${y}-12-31`] } },
]

function CompareTab({ taskId, setTaskId }: { taskId: string | null; setTaskId: (id: string | null) => void }) {
  const [universe, setUniverse] = useState('all')
  const [board, setBoard] = useState('')
  const [industry, setIndustry] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [poolSize, setPoolSize] = useState(60)
  const [seed, setSeed] = useState(42)
  const [selectedKeys, setSelectedKeys] = useState<string[]>(['raw', 'cool', 'full'])
  const [customVariants, setCustomVariants] = useState<{ label: string; cooldown_days: number; defense: string }[]>([])
  const [customLabel, setCustomLabel] = useState('')
  const [customCooldown, setCustomCooldown] = useState(5)
  const [customDefense, setCustomDefense] = useState('soft')
  const [error, setError] = useState('')
  // 心跳时钟: 每秒重算"距上次活动秒数"(修复数据不变时不重渲染导致的提示冻结)
  const [nowTs, setNowTs] = useState(() => Date.now())
  // 卡死诊断: 两次采样对比栈顶(不变=真卡死)
  const [diagStack, setDiagStack] = useState<{ top: string; idle: number; stack: string; at: string } | null>(null)

  const diagnose = async () => {
    if (!taskId) return
    try {
      const d = await api.backtestTaskStack(taskId)
      const top = d.stack.trim().split('\n').slice(-1)[0] ?? ''
      const at = new Date().toLocaleTimeString()
      if (diagStack && diagStack.top === top) {
        // 两次栈顶相同 → 真卡死, 展示完整栈
        setDiagStack({ top, idle: d.idle_seconds, stack: d.stack, at: `${diagStack.at} → ${at} 栈顶未变(疑似卡死)` })
      } else {
        setDiagStack({ top, idle: d.idle_seconds, stack: d.stack, at: `${at} 第1次采样, 再点一次对比` })
      }
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  // 行业选项(与选股中心同源接口)
  const { data: industries } = useQuery({
    queryKey: ['screener-industries'],
    queryFn: () => api.screenerIndustries(),
    staleTime: 10 * 60 * 1000,
  })

  // 任务轮询: 触发后每 2s 拉取进度, 终态(done/error)自动停止
  const { data: task, error: taskError } = useQuery({
    queryKey: ['backtest-compare-task', taskId ?? 'none'],
    queryFn: () => api.backtestCompareTask(taskId!),
    enabled: taskId !== null,
    retry: false,  // 任务不存在(后端重启)不需要重试, 直接走失效清理
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false),
  })
  const running = taskId !== null && task?.status !== 'done' && task?.status !== 'error'
  const progress = task?.progress ?? 0
  const report = task?.status === 'done' ? task.result : null
  const err = error || (task?.status === 'error' ? task.error || '对比回测失败' : '')

  // 任务已不存在(后端重启后内存任务丢失) -> 自动解除前端运行态,
  // 修复: 刷新恢复 taskId 但任务查询失败时永远卡在"运行中 0%"且按钮被禁用的死锁.
  useEffect(() => {
    if (taskId && taskError && String((taskError as Error).message || '').includes('任务不存在')) {
      setTaskId(null)
      setError('')
    }
  }, [taskId, taskError, setTaskId])

  // 放弃当前任务: 协作取消 + 清前端状态(守卫立即放行新任务)
  const abandon = async () => {
    if (!taskId) return
    try {
      await api.cancelBacktestTask(taskId)
    } catch {
      /* 任务可能已结束/不存在, 忽略 */
    }
    setTaskId(null)
    setError('')
  }

  // 心跳时钟: 运行中每秒重渲染一次, 保证"距上次活动秒数"实时变化
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNowTs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [running])

  const allVariants = [
    ...COMPARE_PRESETS
      .filter((p) => selectedKeys.includes(p.key))
      .map((p) => ({ label: p.label, cooldown_days: p.cooldown_days, defense: p.defense })),
    ...customVariants,
  ]

  const run = async () => {
    setError('')
    setTaskId(null)
    if (allVariants.length === 0) {
      setError('请至少勾选或添加一个变体')
      return
    }
    try {
      const { task_id } = await api.backtestStrategyCompare({
        pool_size: poolSize, seed,
        universe, board, industry,
        start: dateFrom, end: dateTo,
        variants: allVariants,
      })
      setTaskId(task_id)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const addCustom = () => {
    setCustomVariants([...customVariants, {
      label: customLabel.trim() || `自定义·冷却${customCooldown}日·${DEFENSE_LABEL[customDefense]}`,
      cooldown_days: customCooldown,
      defense: customDefense,
    }])
    setCustomLabel('')
  }

  // ---- 结果渲染
  const variants = report?.variants ?? []
  const okVariants = variants.filter((v) => !v.error && v.total_return_pct != null)
  const bestReturn = okVariants.length ? Math.max(...okVariants.map((v) => v.total_return_pct!)) : null
  const series: ChartSeries[] = variants
    .map((v, i) => {
      const curve = v.equity_curve
      if (!curve?.length || !curve[0].equity) return null
      const base = curve[0].equity
      return {
        key: `v${i}`, label: v.label, color: COMPARE_COLORS[i % COMPARE_COLORS.length],
        points: curve.map((p) => ({ time: p.date, equity: (p.equity / base - 1) * 100, pnl: 0 })),
      }
    })
    .filter((s): s is ChartSeries => s !== null)

  const act = (v: CompareVariantResult, key: string) => v.by_action?.[key]
  const metricRows: { label: string; render: (v: CompareVariantResult) => ReactNode }[] = [
    {
      label: '总收益',
      render: (v) => v.total_return_pct == null ? '—' : (
        <b style={{ color: v.total_return_pct >= 0 ? '#dc2626' : '#16a34a' }}>
          {fmt(v.total_return_pct, '%')}{bestReturn != null && v.total_return_pct === bestReturn ? ' ★最优' : ''}
        </b>
      ),
    },
    { label: '年化', render: (v) => v.annual_return_pct == null ? '—' : fmt(v.annual_return_pct, '%') },
    { label: '最大回撤', render: (v) => v.max_drawdown_pct == null ? '—' : fmt(-v.max_drawdown_pct, '%') },
    { label: 'Sharpe', render: (v) => v.sharpe?.toFixed(2) ?? '—' },
    { label: '胜率(平仓)', render: (v) => v.win_rate == null ? '—' : `${v.win_rate.toFixed(1)}%` },
    { label: '盈亏因子', render: (v) => v.profit_factor?.toFixed(2) ?? '—' },
    { label: '单笔期望(元)', render: (v) => v.expectancy == null ? '—' : fmt(v.expectancy) },
    { label: '总笔数', render: (v) => v.trades ?? '—' },
    {
      label: '止损(笔·元)',
      render: (v) => {
        const s = act(v, 'sell_stop')
        return s && s.n ? <span style={{ color: s.pnl >= 0 ? '#dc2626' : '#16a34a' }}>{s.n} · {fmt(s.pnl)}</span> : '—'
      },
    },
    {
      label: '止盈减仓(元)',
      render: (v) => {
        const s = act(v, 'sell_reduce')
        return s && s.n ? <span style={{ color: s.pnl >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(s.pnl)}</span> : '—'
      },
    },
    {
      label: '做T高抛(元)',
      render: (v) => {
        const s = act(v, 't_sell')
        return s && s.n ? <span style={{ color: s.pnl >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(s.pnl)}</span> : '—'
      },
    },
    { label: '冷却拦截', render: (v) => v.cooldown_blocks ? `${v.cooldown_blocks} 次` : '0 次' },
    { label: '期末防守', render: (v) => v.final_defense ? '开启' : '关闭' },
  ]
  const errorVariants = variants.filter((v) => v.error)

  return (
    <div>
      <Card className="mb-4 p-4">
        {/* 选池范围(与选股中心同源) */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[12px] font-semibold text-ink">选池范围</span>
          <select
            value={universe}
            onChange={(e) => setUniverse(e.target.value)}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
            title="指数成分股预筛(与选股中心同源, 需成分股缓存)"
          >
            {UNIVERSE_OPTIONS.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
          </select>
          <select
            value={board}
            onChange={(e) => setBoard(e.target.value)}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
            title="板块过滤(代码前缀)"
          >
            {BOARD_OPTIONS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
          </select>
          <select
            value={industry}
            onChange={(e) => setIndustry(e.target.value)}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
            title="行业过滤(申万/东财行业)"
          >
            <option value="">全部行业</option>
            {(industries?.items ?? []).map((it) => (
              <option key={it.name} value={it.name}>{it.name}（{it.count}）</option>
            ))}
          </select>
          <span className="text-[11px] text-ink-faint">与选股中心同源筛选</span>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="flex flex-wrap items-center gap-1">
            <span className="mr-1 text-[13px] text-ink-muted">回测区间</span>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="rounded border border-line px-2 py-1 text-[13px]"
              title="区间起始日(含); 留空=不限. 起点前的历史自动作为指标预热段"
            />
            ~
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="rounded border border-line px-2 py-1 text-[13px]"
              title="区间结束日(含); 留空=不限"
            />
            <span className="text-[11px] text-ink-faint">（留空=全部本地数据）</span>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            {QUICK_RANGES.map((q) => (
              <button
                key={q.label}
                className="rounded-full border border-line bg-white px-2 py-0.5 text-[11px] text-ink-muted hover:border-link hover:text-link"
                onClick={() => { const [f, t] = q.range(); setDateFrom(f); setDateTo(t) }}
              >
                {q.label}
              </button>
            ))}
            <button
              className="rounded-full border border-line bg-white px-2 py-0.5 text-[11px] text-ink-muted hover:border-link hover:text-link"
              onClick={() => { setDateFrom(''); setDateTo('') }}
              title="清空区间, 回测全部本地数据"
            >
              全部
            </button>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1 text-[13px] text-ink-muted">
            随机抽样
            <input
              type="number" min={0} max={500}
              value={poolSize}
              onChange={(e) => setPoolSize(Math.max(0, Math.min(500, Number(e.target.value))))}
              className="w-16 rounded border border-line px-2 py-1 text-[13px]"
              title="从筛选后的池中随机抽取的股票数; 0 = 不抽样(筛选后全部, 注意耗时)"
            />
            只（0=全部）
          </label>
          <label className="flex items-center gap-1 text-[13px] text-ink-muted">
            抽样种子
            <input
              type="number"
              value={seed}
              onChange={(e) => setSeed(Number(e.target.value))}
              className="w-20 rounded border border-line px-2 py-1 text-[13px]"
              title="固定种子: 相同种子+数量 = 相同股票池, 结果可复现可对比"
            />
          </label>
          <Button onClick={run} disabled={running || allVariants.length === 0}>
            {running ? `对比运行中 ${progress}%…` : `运行对比回测（${allVariants.length} 个变体）`}
          </Button>
          <span className="text-[12px] text-ink-faint">
            同池同种子跑各变体 · 差异只来自风控开关本身 · 初始资金 100 万
          </span>
        </div>

        {/* 预设变体勾选 */}
        <div className="mt-3 flex flex-wrap gap-2">
          {COMPARE_PRESETS.map((p) => (
            <label
              key={p.key}
              className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-white px-3 py-1.5 text-[12px]"
            >
              <input
                type="checkbox"
                checked={selectedKeys.includes(p.key)}
                onChange={(e) =>
                  setSelectedKeys(e.target.checked
                    ? [...selectedKeys, p.key]
                    : selectedKeys.filter((k) => k !== p.key))
                }
              />
              <span className="font-semibold">{p.label}</span>
            </label>
          ))}
        </div>

        {/* 自定义变体 */}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            placeholder="自定义变体名(可空)"
            value={customLabel}
            onChange={(e) => setCustomLabel(e.target.value)}
            className="w-40 rounded border border-line px-2 py-1 text-[12px]"
          />
          <label className="flex items-center gap-1 text-[12px] text-ink-muted">
            冷却
            <input
              type="number" min={0} max={30}
              value={customCooldown}
              onChange={(e) => setCustomCooldown(Math.max(0, Math.min(30, Number(e.target.value))))}
              className="w-14 rounded border border-line px-2 py-1 text-[12px]"
            />
            日
          </label>
          <select
            value={customDefense}
            onChange={(e) => setCustomDefense(e.target.value)}
            className="rounded border border-line bg-white px-2 py-1 text-[12px]"
          >
            <option value="soft">软防守</option>
            <option value="off">防守关闭</option>
          </select>
          <button className="text-[12px] text-link hover:underline" onClick={addCustom}>+ 添加</button>
          {customVariants.map((c, i) => (
            <span key={i} className="flex items-center gap-1.5 rounded-full border border-line bg-white px-2 py-0.5 text-[11px]">
              <button
                title="点击移除"
                className="text-ink hover:text-red-600"
                onClick={() => setCustomVariants(customVariants.filter((_, j) => j !== i))}
              >
                {c.label} ✕
              </button>
            </span>
          ))}
        </div>

        {running && progress > 0 && (
          <div className="mt-3">
            <div className="h-1.5 w-full overflow-hidden rounded bg-divider">
              <div className="h-full bg-link transition-all" style={{ width: `${progress}%` }} />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {task?.last_active ? (
                <span className="text-[11px] text-ink-faint">
                  {nowTs / 1000 - task.last_active < 10
                    ? '✓ 任务进行中（最近数秒内有活动）'
                    : `⚠ 已 ${Math.round(nowTs / 1000 - task.last_active)} 秒无活动 —— 正在慢段运行或任务已停止`}
                </span>
              ) : (
                <span className="text-[11px] text-ink-faint">等待进度上报…</span>
              )}
              <button className="text-[11px] text-link hover:underline" onClick={diagnose}>
                卡住了？点此诊断
              </button>
              <button className="text-[11px] text-red-600 hover:underline" onClick={abandon}>
                放弃本次回测
              </button>
            </div>
            {task?.stall_stack && (
              <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2">
                <div className="text-[11px] font-semibold text-amber-700">
                  ⏱ 看门狗：任务在 {task.stall_progress}% 处停滞超 30 秒，已自动抓取线程栈（每分钟刷新）——
                  请截图此内容反馈
                </div>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-relaxed text-ink-muted">
                  {task.stall_stack}
                </pre>
              </div>
            )}
            {diagStack && (
              <div className="mt-2 rounded-lg border border-line bg-white p-2">
                <div className="text-[11px] text-ink-muted">
                  {diagStack.at} · 无活动 {diagStack.idle}s
                </div>
                <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-relaxed text-ink-faint">
                  {diagStack.stack}
                </pre>
              </div>
            )}
          </div>
        )}
        {err && <div className="mt-3"><ErrorBox message={err} /></div>}
      </Card>

      {running && !report && (
        <Card className="p-6">
          <Loading text="正在同池同种子逐变体回放(顺序执行, 进度为总体进度)…" />
        </Card>
      )}

      {report && variants.length > 0 && (
        <>
          <Card className="mb-4 overflow-x-auto p-2">
            <div className="px-3 py-2 text-[13px] font-semibold text-ink">
              变体对比（{report.pool.symbols} 只 · 种子 {report.pool.seed} · 同池消融）
            </div>
            {(() => {
              const ok = variants.find((v) => !v.error && v.date_from)
              return ok ? (
                <div className="px-3 pb-2 text-[11px] text-ink-faint">
                  实际区间：{ok.date_from} ~ {ok.date_to}（{ok.days} 个交易日）
                  {report.pool.note ? ` · 池构成：${report.pool.note}` : ''}
                </div>
              ) : report.pool.note ? (
                <div className="px-3 pb-2 text-[11px] text-ink-faint">池构成：{report.pool.note}</div>
              ) : null
            })()}
            <Table className="text-[12px]">
              <thead>
                <tr>
                  <Th>指标</Th>
                  {variants.map((v, i) => (
                    <Th key={i} center>
                      <span style={{ color: COMPARE_COLORS[i % COMPARE_COLORS.length] }}>●</span> {v.label}
                    </Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {metricRows.map((row) => (
                  <tr key={row.label} className="border-t border-divider">
                    <Td className="whitespace-nowrap text-ink-muted">{row.label}</Td>
                    {variants.map((v, i) => (
                      <Td key={i} center className="whitespace-nowrap">
                        {v.error ? <span className="text-red-600">失败</span> : row.render(v)}
                      </Td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </Table>
            {errorVariants.length > 0 && (
              <div className="px-3 pb-2 text-[11px] text-red-600">
                {errorVariants.map((v) => `${v.label}: ${v.error}`).join('；')}
              </div>
            )}
          </Card>

          {series.length > 0 && (
            <Card className="mb-4 p-4">
              <div className="mb-2 text-[13px] font-semibold text-ink">净值曲线叠加（收益率 %，各自归一）</div>
              <MultiLineChart series={series} height={240} />
              <div className="mt-2 text-[11px] leading-relaxed text-ink-faint">
                同一股票池、同一种子、同一信号引擎，唯一差异是各变体的风控开关 ——
                曲线间的差就是对应开关"值多少钱"。注意: 做T按当日高低价近似(乐观口径), 真实收益可能低于回测。
              </div>
            </Card>
          )}
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          勾选预设变体（或添加自定义组合）后运行：同池同种子对比各风控开关（止损冷却 / 回撤防守）的边际贡献
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
  const scoreRows = report ? Object.entries(report.by_score) : []
  const distTotal = report ? Object.values(report.stage_distribution).reduce((a, b) => a + b, 0) : 0
  const scoreDistTotal = report ? Object.values(report.score_distribution).reduce((a, b) => a + b, 0) : 0

  return (
    <div>
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={run} disabled={running}>
            {running ? '回测运行中…' : '运行因子回测（阶段+总分分桶）'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            数据源：本地 K 线缓存（盘后预热落库），全市场约 {report?.meta.symbols_total ?? '3600+'} 只 · 耗时约 1~2 分钟
          </span>
        </div>
        {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      </Card>

      {running && (
        <Card className="p-6">
          <Loading text="正在逐日回放阶段/总分判定与收益统计…" />
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
            {' '}· 总分 = 技术面三因子 + 阶段加减分（与选股中心排序分同源，不含基本面因子）
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

          <Card className="mb-4 overflow-x-auto p-2">
            <div className="px-3 py-2 text-[13px] font-semibold text-ink">按趋势阶段分桶</div>
            <Table>
              <thead>
                <tr>
                  <Th>阶段</Th>
                  {report.meta.hold_days.map((h) => (
                    <Th key={h} right>{HOLD_LABELS[`hold_${h}`] ?? `${h}日`}持有</Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stages.map(([k, info]) => {
                  const m = STAGE_META[k] ?? { label: k, color: '#64748b' }
                  return (
                    <tr key={k} className="border-t border-divider">
                      <Td>
                        <div className="text-[14px] font-semibold" style={{ color: m.color }}>{info.label}</div>
                      </Td>
                      {report.meta.hold_days.map((h) => {
                        const s = info.holds[`hold_${h}`]
                        return s ? <HoldCell key={h} s={s} /> : <Td key={h} className="text-ink-faint">-</Td>
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </Table>
          </Card>

          <Card className="overflow-x-auto p-2">
            <div className="px-3 py-2 text-[13px] font-semibold text-ink">按选股总分分桶（验证评分有效性）</div>
            <div className="px-3 pb-2 text-[11px] text-ink-faint">
              {Object.entries(report.score_distribution).map(([k, v]) => {
                const pct = scoreDistTotal ? (v / scoreDistTotal) * 100 : 0
                return (
                  <span key={k} className="mr-3">
                    {SCORE_META[k]?.label ?? k}：{v} 次（{pct.toFixed(1)}%）
                  </span>
                )
              })}
            </div>
            <Table>
              <thead>
                <tr>
                  <Th>总分区间</Th>
                  {report.meta.hold_days.map((h) => (
                    <Th key={h} right>{HOLD_LABELS[`hold_${h}`] ?? `${h}日`}持有</Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {scoreRows.map(([k, info]) => {
                  const m = SCORE_META[k] ?? { label: info.label, color: '#64748b' }
                  return (
                    <tr key={k} className="border-t border-divider">
                      <Td>
                        <div className="text-[14px] font-semibold" style={{ color: m.color }}>{m.label}</div>
                      </Td>
                      {report.meta.hold_days.map((h) => {
                        const s = info.holds[`hold_${h}`]
                        return s ? <HoldCell key={h} s={s} /> : <Td key={h} className="text-ink-faint">-</Td>
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </Table>
          </Card>

          <Card className="mt-4 p-3 text-[12px] leading-relaxed text-ink-muted">
            <b className="text-ink">怎么看：</b>
            胜率为正收益比例，均值/中位数为净收益率（%）。<br />
            · <b className="text-ink">阶段表</b>：优先看 20 日期望——启动/加速期为正、过热/衰竭为负，说明"刚起趋势优于追高"成立；
            无趋势/衰竭期期望为负时应回避。<br />
            · <b className="text-ink">总分表</b>：若期望随分数区间<b className="text-ink">单调递增</b>（50-60 &lt; 60-70 &lt; 70+），
            说明评分体系有效，高分股未来收益更好；若<b className="text-ink">高分桶期望反而更低</b>，
            说明分数与未来收益脱节（典型如动量分在暴涨顶部打满、过热惩罚压不住），评分体系需要重校。
            样本区间覆盖较短（多数股票仅近几个月），结论仅供参考，建议持续回测积累。
          </Card>
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          点击上方按钮，用本地缓存历史数据运行阶段 + 总分双分桶因子回测
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 信号审计标签(方案 v2 §6)
const DEVIATION_LABEL: Record<string, string> = {
  一致: '一致', 违背: '违背', 滞后: '滞后', 提前: '提前',
}
const DEVIATION_COLOR: Record<string, string> = {
  一致: '#16a34a', 违背: '#dc2626', 滞后: '#f59e0b', 提前: '#f59e0b',
}

function AuditTab() {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [report, setReport] = useState<SignalAuditReport | null>(null)
  const [showDetail, setShowDetail] = useState(false)

  const run = async () => {
    setRunning(true)
    setError('')
    setReport(null)
    try {
      const r = await api.backtestAudit({})
      setReport(r)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setRunning(false)
    }
  }

  const st = report?.stats
  const series: ChartSeries[] = report ? [
    {
      key: 'real', label: '真实(实际成交)', color: '#94a3b8',
      points: report.curves.real.map((p) => ({ time: p.date, equity: p.equity, pnl: 0 })),
    },
    {
      key: 'discipline', label: '纪律(严格执行信号)', color: '#dc2626',
      points: report.curves.discipline.map((p) => ({ time: p.date, equity: p.equity, pnl: 0 })),
    },
  ] : []

  return (
    <div>
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={run} disabled={running}>
            {running ? '审计运行中…' : '运行信号审计'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            真实成交 vs 严格执行信号 · 逐笔标注 违背/滞后/提前 · 组合=各票等权
          </span>
        </div>
        {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      </Card>

      {running && (
        <Card className="p-6">
          <Loading text="正在回放真实成交与纪律线逐日判定…" />
        </Card>
      )}

      {report && st && (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
            <StatCard label="偏差总额(真实−纪律)" value={fmt(st.gap_total_pct, '%')}
              color={st.gap_total_pct >= 0 ? '#16a34a' : '#dc2626'}
              sub={`${report.meta.symbols} 只 · ${report.meta.days} 交易日`} />
            <StatCard label="审计笔数" value={`${st.audit_count} 笔`}
              sub={`一致 ${st.agree} · 违背 ${st.violate} · 滞后 ${st.lag} · 提前 ${st.early}`} />
            <StatCard label="违背(该执行没执行)" value={`${st.violate} 笔`} color={st.violate > 0 ? '#dc2626' : '#16a34a'} />
            <StatCard label="滞后(晚于信号)" value={`${st.lag} 笔`} color={st.lag > 0 ? '#f59e0b' : '#16a34a'}
              sub={`提前(抢跑) ${st.early} 笔`} />
          </div>

          <Card className="mb-4 p-4">
            <div className="mb-2 text-[13px] font-semibold text-ink">真实 vs 纪律（收益率 %）</div>
            <MultiLineChart series={series} height={220} />
            <div className="mt-2 text-[11px] leading-relaxed text-ink-faint">{report.meta.notes}</div>
          </Card>

          <Card className="mb-4 overflow-x-auto p-2">
            <div className="px-3 py-2 text-[13px] font-semibold text-ink">每票对比</div>
            <Table className="text-[12px]">
              <thead>
                <tr>
                  <Th>代码</Th>
                  <Th right>真实%</Th>
                  <Th right>纪律%</Th>
                  <Th right>偏差%</Th>
                </tr>
              </thead>
              <tbody>
                {report.by_symbol.map((b) => (
                  <tr key={b.symbol} className="border-t border-divider">
                    <Td className="font-semibold">{b.symbol} <span className="text-ink-faint">{b.name}</span></Td>
                    <Td right style={{ color: b.real_return_pct >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(b.real_return_pct, '%')}</Td>
                    <Td right style={{ color: b.discipline_return_pct >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(b.discipline_return_pct, '%')}</Td>
                    <Td right style={{ color: b.gap_pct >= 0 ? '#16a34a' : '#dc2626' }}>{fmt(b.gap_pct, '%')}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>

          <Card className="overflow-x-auto p-2">
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-[13px] font-semibold text-ink">逐笔审计（{report.audits.length} 条）</span>
              <button className="text-[12px] text-link hover:underline" onClick={() => setShowDetail(!showDetail)}>
                {showDetail ? '收起' : '展开'}
              </button>
            </div>
            {showDetail && (
              <Table className="text-[12px]">
                <thead>
                  <tr>
                    <Th>日期</Th>
                    <Th>代码</Th>
                    <Th>实际动作</Th>
                    <Th>纪律建议</Th>
                    <Th>偏差</Th>
                  </tr>
                </thead>
                <tbody>
                  {report.audits.slice(-100).reverse().map((a, i) => (
                    <tr key={i} className="border-t border-divider">
                      <Td className="whitespace-nowrap">{a.date}</Td>
                      <Td className="font-semibold">{a.symbol}</Td>
                      <Td>{a.real_action}</Td>
                      <Td className="text-ink-faint">{a.advice}</Td>
                      <Td style={{ color: DEVIATION_COLOR[a.deviation] ?? '#334155' }}>
                        <b>{DEVIATION_LABEL[a.deviation] ?? a.deviation}</b>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          点击上方按钮，用真实成交记录量化「计划 vs 执行」偏差（回答：我该执行没执行的信号亏了多少）
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 持仓回测报告
function PortfolioReport({ report }: { report: PortfolioBacktestReport }) {
  const [showTrades, setShowTrades] = useState(false)
  const st = report.stats
  const attrKeys = ['sell_stop', 'sell_reduce', 't_sell', 'buy_add'] as const

  const toSeries = (): ChartSeries[] => {
    const init = report.meta.initial_capital
    const mk = (key: string, label: string, color: string, pts: { date: string; equity: number }[]): ChartSeries => ({
      key, label, color,
      points: pts.map((p) => ({ time: p.date, equity: (p.equity / init - 1) * 100, pnl: 0 })),
    })
    const series: ChartSeries[] = [
      mk('hold', '躺平(买入持有)', '#94a3b8', report.curves.hold),
      mk('stop', '纪律(仅止损)', '#f59e0b', report.curves.stop),
      mk('signal', '系统(信号全开)', '#dc2626', report.curves.signal),
    ]
    if (report.curves.benchmark?.length) {
      series.push({
        key: 'benchmark', label: '沪深300', color: '#3b82f6',
        points: report.curves.benchmark.map((p) => ({ time: p.date, equity: (p.equity - 1) * 100, pnl: 0 })),
      })
    }
    return series
  }

  return (
    <>
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <StatCard label="躺平(买入持有)" value={fmt(st.hold_return_pct, '%')} color={st.hold_return_pct >= 0 ? '#dc2626' : '#16a34a'} />
        <StatCard label="纪律(仅止损)" value={fmt(st.stop_return_pct, '%')} color={st.stop_return_pct >= 0 ? '#dc2626' : '#16a34a'} />
        <StatCard label="系统(信号全开)" value={fmt(st.signal_return_pct, '%')}
          color={st.signal_return_pct >= 0 ? '#dc2626' : '#16a34a'}
          sub={`超额 ${fmt(st.excess_vs_hold_pct, '%')} · 年化 ${fmt(st.signal_annual_pct, '%')}`} />
        <StatCard label="系统风控" value={`回撤 ${fmt(-st.managed_max_drawdown_pct, '%')}`}
          sub={`夏普 ${st.managed_sharpe.toFixed(2)} · 胜率 ${st.win_rate.toFixed(1)}% · 做T ${fmt(st.t_contribution)} 元`} />
      </div>

      <Card className="mb-4 p-4">
        <div className="mb-2 flex items-baseline justify-between">
          <span className="text-[13px] font-semibold text-ink">三线对照 + 沪深300 基准（收益率 %）</span>
          <span className="text-[11px] text-ink-faint">
            {report.meta.legs} 条建仓腿{report.meta.skipped > 0 ? ` · ${report.meta.skipped} 只无数据跳过` : ''} · {report.meta.days} 个交易日
          </span>
        </div>
        <MultiLineChart series={toSeries()} />
        <div className="mt-2 text-[11px] leading-relaxed text-ink-faint">{report.meta.notes}</div>
      </Card>

      <Card className="mb-4 overflow-x-auto p-2">
        <div className="px-3 py-2 text-[13px] font-semibold text-ink">差异归因（每腿：管理线 − 躺平线，超额拆解到操作类型）</div>
        <Table className="text-[12px]">
          <thead>
            <tr>
              <Th>代码</Th>
              <Th>建仓日</Th>
              <Th right>成本/数量</Th>
              <Th right>躺平%</Th>
              <Th right>管理%</Th>
              <Th right>超额%</Th>
              <Th>归因（元）</Th>
            </tr>
          </thead>
          <tbody>
            {report.legs.map((l) => (
              <tr key={`${l.symbol}|${l.entry_date}`} className="border-t border-divider">
                <Td className="font-semibold">{l.symbol}</Td>
                <Td className="whitespace-nowrap">{l.entry_date}</Td>
                <Td right>{l.cost.toFixed(2)} / {l.qty}</Td>
                <Td right style={{ color: l.hold_return_pct >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(l.hold_return_pct, '%')}</Td>
                <Td right style={{ color: l.managed_return_pct >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(l.managed_return_pct, '%')}</Td>
                <Td right style={{ color: l.excess_pct >= 0 ? '#dc2626' : '#16a34a' }}>{fmt(l.excess_pct, '%')}</Td>
                <Td className="text-ink-muted">
                  {attrKeys.filter((k) => l.attribution?.[k]).map((k) => (
                    <span key={k} className="mr-2 whitespace-nowrap">
                      {ATTR_LABEL[k]} {fmt(l.attribution[k])}
                    </span>
                  ))}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      <Card className="overflow-x-auto p-2">
        <div className="flex items-center justify-between px-3 py-2">
          <span className="text-[13px] font-semibold text-ink">交易明细（{report.trades.length} 笔）</span>
          <button className="text-[12px] text-link hover:underline" onClick={() => setShowTrades(!showTrades)}>
            {showTrades ? '收起' : '展开'}
          </button>
        </div>
        {showTrades && (
          <Table className="text-[12px]">
            <thead>
              <tr>
                <Th>日期</Th>
                <Th>代码</Th>
                <Th>动作</Th>
                <Th right>价格</Th>
                <Th right>数量</Th>
                <Th right>盈亏</Th>
                <Th>信号理由</Th>
              </tr>
            </thead>
            <tbody>
              {report.trades.slice(-60).reverse().map((t, i) => (
                <tr key={i} className="border-t border-divider">
                  <Td className="whitespace-nowrap">{t.date}</Td>
                  <Td className="font-semibold">{t.symbol}</Td>
                  <Td>{ACTION_LABEL[t.action] ?? t.action}</Td>
                  <Td right>{t.price.toFixed(2)}</Td>
                  <Td right>{t.qty}</Td>
                  <Td right style={{ color: t.pnl > 0 ? '#dc2626' : t.pnl < 0 ? '#16a34a' : undefined }}>
                    {t.pnl === 0 ? '-' : fmt(t.pnl)}
                  </Td>
                  <Td className="text-ink-faint">{t.reason}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  )
}

// ---------------------------------------------------------------- 持仓回测标签(方案 v2 §5)
function PortfolioTab() {
  const [mode, setMode] = useState<'pos' | 'trades' | 'manual'>('pos')
  const [manage, setManage] = useState('signal')
  const [intradayMinutes, setIntradayMinutes] = useState(10)  // 盘中路径模拟粒度
  const [selectedPos, setSelectedPos] = useState<string[]>([])
  const [selectedTrades, setSelectedTrades] = useState<string[]>([])
  const [manualLegs, setManualLegs] = useState<ManualLegRow[]>([
    { symbol: '', entry_date: '', cost: '', qty: '' },
  ])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [report, setReport] = useState<PortfolioBacktestReport | null>(null)
  // 快捷输入: 模板 / 粘贴 / 最近使用
  const [presetName, setPresetName] = useState('')
  const [showPaste, setShowPaste] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const [recent, setRecent] = useState<RecentItem[]>(loadRecent())
  const [deleteTarget, setDeleteTarget] = useState<BacktestPreset | null>(null)
  const queryClient = useQueryClient()

  const { data: preview } = useQuery({
    queryKey: ['portfolio-preview'],
    queryFn: () => api.portfolioPreview(),
  })
  const { data: presets } = useQuery({
    queryKey: ['backtest-presets'],
    queryFn: () => api.backtestPresets(),
  })

  // 默认全选
  useEffect(() => {
    if (!preview) return
    setSelectedPos((prev) => (prev.length ? prev : preview.positions.map((p) => p.symbol)))
    setSelectedTrades((prev) => (prev.length ? prev : preview.trades.map((t) => `${t.symbol}|${t.entry_date}`)))
  }, [preview])

  const buildLegs = () => {
    if (mode === 'pos') {
      return (preview?.positions ?? [])
        .filter((p) => selectedPos.includes(p.symbol))
        .map((p) => ({ symbol: p.symbol, name: p.name, entry_date: p.entry_date, cost: p.cost, qty: p.qty }))
    }
    if (mode === 'trades') {
      return (preview?.trades ?? [])
        .filter((t) => selectedTrades.includes(`${t.symbol}|${t.entry_date}`))
        .map((t) => ({ symbol: t.symbol, name: t.name, entry_date: t.entry_date, cost: t.cost, qty: t.qty }))
    }
    return manualLegs
      .filter((l) => l.symbol.trim() && Number(l.qty) > 0 && Number(l.cost) > 0)
      .map((l) => ({ symbol: l.symbol.trim(), name: '', entry_date: l.entry_date, cost: Number(l.cost), qty: Number(l.qty) }))
  }

  const run = async () => {
    setRunning(true)
    setError('')
    setReport(null)
    try {
      const legs = buildLegs()
      if (legs.length === 0) {
        setError('请至少选择/输入一条建仓腿')
        setRunning(false)
        return
      }
      const r = await api.backtestPortfolio({ mode: 'import', legs, manage, intraday_minutes: intradayMinutes })
      setReport(r)
      // 记录最近使用(本地, 免登录复用)
      saveRecent(legs)
      setRecent(loadRecent())
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setRunning(false)
    }
  }

  // ---- 模板: 保存 / 加载 / 删除
  const savePreset = async () => {
    const legs = buildLegs()
    if (!presetName.trim() || legs.length === 0) {
      setError('请输入模板名且至少一条有效建仓腿')
      return
    }
    try {
      await api.saveBacktestPreset({ name: presetName.trim(), legs })
      setPresetName('')
      setError('')
      queryClient.invalidateQueries({ queryKey: ['backtest-presets'] })
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const loadPreset = (p: BacktestPreset) => {
    setManualLegs(p.legs.map((l) => ({
      symbol: l.symbol, entry_date: l.entry_date ?? '',
      cost: l.cost != null ? String(l.cost) : '', qty: l.qty != null ? String(l.qty) : '',
    })))
    setMode('manual')
  }

  const confirmDeletePreset = async () => {
    if (!deleteTarget) return
    try {
      await api.deleteBacktestPreset(deleteTarget.id)
      setDeleteTarget(null)
      queryClient.invalidateQueries({ queryKey: ['backtest-presets'] })
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  // ---- 批量粘贴解析 -> 填入手动表格
  const applyPaste = () => {
    const rows = parsePasteLegs(pasteText)
    if (rows.length === 0) {
      setError('未解析到有效行(格式: 代码,建仓日,成本,数量 一行一腿)')
      return
    }
    setManualLegs(rows.length ? rows : manualLegs)
    setPasteText('')
    setShowPaste(false)
    setError('')
  }

  const loadRecentLegs = (item: RecentItem) => {
    setManualLegs(item.legs.map((l) => ({
      symbol: l.symbol, entry_date: l.entry_date ?? '',
      cost: l.cost != null ? String(l.cost) : '', qty: l.qty != null ? String(l.qty) : '',
    })))
    setMode('manual')
  }

  return (
    <div>
      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as 'pos' | 'trades' | 'manual')}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
          >
            <option value="pos">真实持仓（模式 A）</option>
            <option value="trades">从成交导入（模式 B）</option>
            <option value="manual">手动输入建仓腿（模式 B）</option>
          </select>
          <select
            value={manage}
            onChange={(e) => setManage(e.target.value)}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
          >
            <option value="signal">系统（信号全开）</option>
            <option value="stop">纪律（仅止损）</option>
            <option value="hold">躺平（买入持有）</option>
          </select>
          <select
            value={intradayMinutes}
            onChange={(e) => setIntradayMinutes(Number(e.target.value))}
            className="rounded border border-line bg-white px-2 py-1.5 text-[13px]"
            title="盘中路径模拟粒度：用日内路径模拟盘中实时触发"
          >
            <option value={5}>盘中模拟 5 分钟</option>
            <option value={10}>盘中模拟 10 分钟</option>
            <option value={15}>盘中模拟 15 分钟</option>
            <option value={30}>盘中模拟 30 分钟</option>
          </select>
          <Button onClick={run} disabled={running}>
            {running ? '回测运行中…' : '运行持仓回测'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            从各自建仓日起回放 · 三线对照 + 沪深300基准 · 信号盘中路径触发（止损/做T 触及即成交）
          </span>
        </div>
        {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      </Card>

      {mode === 'pos' && (
        <Card className="mb-4 p-3">
          <div className="mb-2 text-[13px] font-semibold text-ink">当前持仓（勾选参与回测）</div>
          {(preview?.positions?.length ?? 0) === 0 && (
            <div className="text-[12px] text-ink-faint">暂无持仓</div>
          )}
          <div className="flex flex-wrap gap-2">
            {preview?.positions.map((p) => (
              <label key={p.symbol} className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-white px-3 py-1.5 text-[12px]">
                <input
                  type="checkbox"
                  checked={selectedPos.includes(p.symbol)}
                  onChange={(e) =>
                    setSelectedPos(e.target.checked
                      ? [...selectedPos, p.symbol]
                      : selectedPos.filter((s) => s !== p.symbol))
                  }
                />
                <span className="font-semibold">{p.symbol}</span>
                <span className="text-ink-muted">{p.name}</span>
                <span className="text-ink-faint">{p.entry_date}建仓 · {p.qty}股 · 成本{p.cost.toFixed(2)}</span>
              </label>
            ))}
          </div>
        </Card>
      )}

      {mode === 'trades' && (
        <Card className="mb-4 p-3">
          <div className="mb-2 text-[13px] font-semibold text-ink">真实买入成交（勾选作为建仓腿，不同时间建仓）</div>
          {(preview?.trades?.length ?? 0) === 0 && (
            <div className="text-[12px] text-ink-faint">暂无成交记录</div>
          )}
          <div className="flex flex-wrap gap-2">
            {preview?.trades.map((t) => {
              const key = `${t.symbol}|${t.entry_date}`
              return (
                <label key={key} className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-white px-3 py-1.5 text-[12px]">
                  <input
                    type="checkbox"
                    checked={selectedTrades.includes(key)}
                    onChange={(e) =>
                      setSelectedTrades(e.target.checked
                        ? [...selectedTrades, key]
                        : selectedTrades.filter((s) => s !== key))
                    }
                  />
                  <span className="font-semibold">{t.symbol}</span>
                  <span className="text-ink-muted">{t.name}</span>
                  <span className="text-ink-faint">{t.entry_date} · {t.qty}股 · {t.cost.toFixed(2)}</span>
                </label>
              )
            })}
          </div>
        </Card>
      )}

      {mode === 'manual' && (
        <Card className="mb-4 p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold text-ink">手动建仓腿</span>
            {/* 模板加载 */}
            <select
              value=""
              onChange={(e) => {
                const p = presets?.find((x) => String(x.id) === e.target.value)
                if (p) loadPreset(p)
              }}
              className="rounded border border-line bg-white px-2 py-1 text-[12px]"
            >
              <option value="">模板：选择加载…</option>
              {(presets ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.name}（{p.legs.length}腿）</option>
              ))}
            </select>
            {/* 保存模板 */}
            <input
              placeholder="模板名"
              value={presetName}
              onChange={(e) => setPresetName(e.target.value)}
              className="w-28 rounded border border-line px-2 py-1 text-[12px]"
            />
            <Button kind="default" className="!px-2 !py-1 !text-[12px]" onClick={savePreset}>存为模板</Button>
            <button
              className="rounded border border-line bg-white px-2 py-1 text-[12px] text-ink hover:border-link hover:text-link"
              onClick={() => setShowPaste(!showPaste)}
            >
              {showPaste ? '收起粘贴' : '批量粘贴导入'}
            </button>
          </div>

          {/* 最近使用(本地自动记录最近 5 次) */}
          {recent.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-[11px] text-ink-faint">最近回测：</span>
              {recent.map((r, i) => (
                <button
                  key={i}
                  title={r.legs.map((l) => `${l.symbol}×${l.qty}`).join(' / ')}
                  className="rounded-full border border-line bg-white px-2 py-0.5 text-[11px] text-ink hover:border-link hover:text-link"
                  onClick={() => loadRecentLegs(r)}
                >
                  {r.time.slice(5)} · {r.legs.length}腿
                </button>
              ))}
            </div>
          )}

          {/* 批量粘贴: CSV / Tab / 分号, 一行一腿 */}
          {showPaste && (
            <div className="mb-2 rounded-lg border border-line bg-white p-2">
              <textarea
                value={pasteText}
                onChange={(e) => setPasteText(e.target.value)}
                rows={5}
                placeholder={'从 Excel/CSV 复制粘贴，一行一腿：\n600000,2025-06-03,7.5,2000\n000001,2025-08-04,11.0,1000\n（支持带表头与名称列）'}
                className="w-full rounded border border-line p-2 font-mono text-[12px]"
              />
              <div className="mt-1 flex items-center gap-2">
                <Button className="!px-2 !py-1 !text-[12px]" onClick={applyPaste}>解析并填充</Button>
                <span className="text-[11px] text-ink-faint">解析成功会替换当前腿列表</span>
              </div>
            </div>
          )}

          <div className="space-y-2">
            {manualLegs.map((l, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <input
                  placeholder="代码"
                  value={l.symbol}
                  onChange={(e) => setManualLegs(manualLegs.map((x, j) => (j === i ? { ...x, symbol: e.target.value } : x)))}
                  className="w-24 rounded border border-line px-2 py-1 text-[13px]"
                />
                <input
                  placeholder="建仓日 YYYY-MM-DD"
                  value={l.entry_date}
                  onChange={(e) => setManualLegs(manualLegs.map((x, j) => (j === i ? { ...x, entry_date: e.target.value } : x)))}
                  className="w-36 rounded border border-line px-2 py-1 text-[13px]"
                />
                <input
                  placeholder="成本"
                  type="number"
                  value={l.cost}
                  onChange={(e) => setManualLegs(manualLegs.map((x, j) => (j === i ? { ...x, cost: e.target.value } : x)))}
                  className="w-20 rounded border border-line px-2 py-1 text-[13px]"
                />
                <input
                  placeholder="数量"
                  type="number"
                  value={l.qty}
                  onChange={(e) => setManualLegs(manualLegs.map((x, j) => (j === i ? { ...x, qty: e.target.value } : x)))}
                  className="w-20 rounded border border-line px-2 py-1 text-[13px]"
                />
                <button
                  className="text-[12px] text-ink-faint hover:text-red-600"
                  onClick={() => setManualLegs(manualLegs.filter((_, j) => j !== i))}
                >
                  删除
                </button>
              </div>
            ))}
            <button className="text-[12px] text-link hover:underline" onClick={() => setManualLegs([...manualLegs, { symbol: '', entry_date: '', cost: '', qty: '' }])}>
              + 添加一条建仓腿
            </button>
          </div>

          {/* 已存模板列表(可删除) */}
          {(presets ?? []).length > 0 && (
            <div className="mt-3 border-t border-divider pt-2">
              <div className="mb-1 text-[11px] text-ink-faint">已存模板：</div>
              <div className="flex flex-wrap gap-2">
                {presets?.map((p) => (
                  <span key={p.id} className="flex items-center gap-1.5 rounded-lg border border-line bg-white px-2 py-1 text-[11px]">
                    <button className="text-ink hover:text-link" onClick={() => loadPreset(p)}>{p.name}（{p.legs.length}腿）</button>
                    <button className="text-ink-faint hover:text-red-600" onClick={() => setDeleteTarget(p)}>✕</button>
                  </span>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="删除模板"
          message={`确定删除模板「${deleteTarget.name}」？删除后不可恢复。`}
          onConfirm={confirmDeletePreset}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {running && (
        <Card className="p-6">
          <Loading text="正在回放三条对照线（躺平/纪律/系统）与组合撮合…" />
        </Card>
      )}

      {report && <PortfolioReport report={report} />}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          选择建仓腿后运行，输出 躺平/纪律/系统 三线对照、沪深300基准与每腿差异归因
        </Card>
      )}
    </div>
  )
}

export default function Backtest() {
  const [tab, setTab] = useState<'portfolio' | 'audit' | 'compare' | 'factor' | 'data'>('portfolio')
  // 对比回测任务 ID 提升到父级: 切 tab 不丢进度(React Query 缓存恢复轮询);
  // sessionStorage 兜底: 整页刷新后也能接上(后端任务在进程内存中, 刷新不中断).
  const [compareTaskId, setCompareTaskId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem('bt-compare-task') || null
    } catch {
      return null
    }
  })
  const setCompareTask = (id: string | null) => {
    setCompareTaskId(id)
    try {
      if (id) sessionStorage.setItem('bt-compare-task', id)
      else sessionStorage.removeItem('bt-compare-task')
    } catch {
      /* sessionStorage 不可用: 忽略 */
    }
  }

  return (
    <div>
      <PageHeader
        title="回测中心"
        subtitle={
          <>
            <b className="text-ink">持仓回测</b>（躺平·纪律·系统三线对照 + 差异归因）、<b className="text-ink">信号审计</b>（真实 vs 纪律，
            量化计划-执行偏差）、<b className="text-ink">对比回测</b>（同池消融，量化风控开关贡献）、<b className="text-ink">阶段分桶</b>（胜率/期望统计）与
            <b className="text-ink">数据管理</b>（K线缓存新鲜度 + 增量补拉）。
          </>
        }
      />

      <div className="mb-3 flex flex-wrap gap-1.5">
        {([
          ['portfolio', '持仓回测（三线对照）'],
          ['audit', '信号审计（真实 vs 纪律）'],
          ['compare', '对比回测（消融实验）'],
          ['factor', '阶段分桶（胜率统计）'],
          ['data', '数据管理（K线更新）'],
        ] as const).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={cn(
              'rounded-full px-3 py-1.5 text-[13px] transition-colors',
              tab === k
                ? 'bg-link text-white'
                : 'border border-line bg-white text-ink hover:border-link hover:text-link',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'portfolio' ? <PortfolioTab />
        : tab === 'audit' ? <AuditTab />
        : tab === 'compare' ? <CompareTab taskId={compareTaskId} setTaskId={setCompareTask} />
        : tab === 'factor' ? <FactorTab />
        : <DataTab />}
    </div>
  )
}
