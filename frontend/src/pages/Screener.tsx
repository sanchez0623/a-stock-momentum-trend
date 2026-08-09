import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type { IndustryItem, ScreenerTask, UniverseStats } from '../api/client'
import { Button, Card, EmptyState, ErrorBox, Loading, Tag, toast } from '../components/ui'
import { cn } from '../components/ui'

// 理由标签配色: 遵循 A 股惯例, 利多=红 / 偏空=绿 / 需注意=橙 / 中性=灰
const TAG_COLOR: Record<string, string> = {
  good: '#dc2626',
  warn: '#ea580c',
  bad: '#16a34a',
  info: '#64748b',
}

// 趋势阶段(方案B): 启动/加速=红(利多) / 过热=橙(需注意) / 衰竭=绿(偏空)
const STAGE_LABEL: Record<string, string> = {
  launch: '启动期',
  accelerate: '加速期',
  overheat: '过热期',
  exhaust: '衰竭期',
}
const STAGE_COLOR: Record<string, string> = {
  launch: '#dc2626',
  accelerate: '#dc2626',
  overheat: '#ea580c',
  exhaust: '#16a34a',
}

const FACTORS = ['趋势', '动量', '量能'] as const

const BOARDS = [
  { value: 'main', label: '主板' },
  { value: 'chinext', label: '创业板' },
  { value: 'star', label: '科创板' },
  { value: 'bj', label: '北交所' },
] as const

/** 组合选股池: 需其包含的指数都有数据才可用 */
const UNIVERSE_COMBO = { value: 'hs300+zz500', label: '沪深300+中证500(≈中证800)', needs: ['hs300', 'zz500'] }

const CAP_OPTIONS = [
  { value: 0, label: '跟随全局' },
  { value: 3, label: '3只/行业' },
  { value: 5, label: '5只/行业' },
  { value: 10, label: '10只/行业' },
]

const LEVEL_OPTIONS = [
  { value: 'sw_l1', label: '申万一级' },
  { value: 'sw_l2', label: '申万二级' },
  { value: 'sw_l3', label: '申万三级' },
]

export default function Screener() {
  const [task, setTask] = useState<ScreenerTask | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // ① 股票池
  const [universe, setUniverse] = useState('')
  const [universeStats, setUniverseStats] = useState<UniverseStats | null>(null)
  const [universeNote, setUniverseNote] = useState('')

  // ② 过滤条件
  const [boards, setBoards] = useState<string[]>([])
  const [industries, setIndustries] = useState<string[]>([])
  const [industryList, setIndustryList] = useState<IndustryItem[]>([])
  const [industryNote, setIndustryNote] = useState('')
  const [industryKeyword, setIndustryKeyword] = useState('')

  // ③ 扫描参数
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [topN, setTopN] = useState(30)
  const [perIndustry, setPerIndustry] = useState(0)
  const [industryLevel, setIndustryLevel] = useState('sw_l1')
  const [applyGate, setApplyGate] = useState(true)
  const [applyFactors, setApplyFactors] = useState(true)

  const [running, setRunning] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPoll = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  useEffect(() => {
    api.screenerLatest().then((t) => { if (t && (t.status === 'done' || t.status === 'failed')) setTask(t) }).catch(() => {})
      .finally(() => setLoading(false))
    api.universeStats().then((s) => {
      setUniverseStats(s)
      if (Object.keys(s).length === 0) setUniverseNote('成分股缓存为空, 请先在后端刷新选股池(Swagger: POST /screener/universe/refresh)')
    }).catch(() => setUniverseNote('选股池状态读取失败'))
    api.screenerIndustries().then((r) => {
      setIndustryList(r.items ?? [])
      if (!r.items?.length) setIndustryNote('行业数据为空, 需东财股票列表成功拉取一次后自动填充')
    }).catch(() => setIndustryNote('行业列表读取失败'))
    return stopPoll
  }, [])

  const toggleBoard = (b: string) =>
    setBoards((prev) => (prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b]))

  const toggleIndustry = (name: string) =>
    setIndustries((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]))

  const filteredIndustries = useMemo(() => {
    const kw = industryKeyword.trim().toLowerCase()
    const list = kw ? industryList.filter((i) => i.name.toLowerCase().includes(kw)) : industryList
    return list.slice(0, 30)
  }, [industryList, industryKeyword])

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const { task_id } = await api.screenerRun(
        'all', topN,
        boards.join(',') || undefined,
        industries.join(',') || undefined,
        universe || undefined,
        { perIndustry, industryLevel, applyGate, applyFactors },
      )
      toast.info('扫描已启动, 完成后自动刷新结果')
      stopPoll()
      pollRef.current = setInterval(async () => {
        try {
          const t = await api.screenerResult(task_id)
          setTask(t)
          if (t.status === 'done' || t.status === 'failed') {
            stopPoll()
            setRunning(false)
            if (t.status === 'failed') toast.error(t.error || '扫描失败')
          }
        } catch {
          stopPoll()
          setRunning(false)
        }
      }, 3000)
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
      setRunning(false)
    }
  }

  if (loading) return <Loading />

  const scanning = running || (task?.status === 'running' || task?.status === 'pending')

  const selectedChips = [
    ...(universe ? [{ label: `股票池 ${universeStats?.[universe]?.label ?? (universe === UNIVERSE_COMBO.value ? UNIVERSE_COMBO.label : universe)}` }] : []),
    ...boards.map((b) => ({ label: BOARDS.find((x) => x.value === b)?.label ?? b })),
    ...industries.map((n) => ({ label: n })),
  ]

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">选股</h1>
      {error && <ErrorBox message={error} />}

      {/* ---------- ① 股票池 ---------- */}
      <Card title="① 股票池 · 从哪批股票里选">
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setUniverse('')}
            className={cn(
              'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
              universe === '' ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
            )}
          >
            全部A股
          </button>
          {universeStats && Object.entries(universeStats).map(([key, v]) => (
            <button
              key={key}
              type="button"
              onClick={() => setUniverse(key)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
                universe === key ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
              )}
            >
              {v.label}成分 · {v.count}只
            </button>
          ))}
          {universeStats && UNIVERSE_COMBO.needs.every((k) => universeStats[k]?.count > 0) && (
            <button
              type="button"
              onClick={() => setUniverse(UNIVERSE_COMBO.value)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
                universe === UNIVERSE_COMBO.value ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
              )}
            >
              {UNIVERSE_COMBO.label}
            </button>
          )}
        </div>
        {universeNote && <p className="mt-2 text-[11px] text-[#ea580c]">{universeNote}</p>}
      </Card>

      {/* ---------- ② 过滤条件 ---------- */}
      <Card title="② 过滤条件 · 池内缩小范围(可组合)" className="mt-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-ink-secondary">上市板块</span>
          {BOARDS.map((b) => (
            <button
              key={b.value}
              type="button"
              onClick={() => toggleBoard(b.value)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
                boards.includes(b.value) ? 'border-[#185fa5] bg-[#185fa5] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
              )}
            >
              {b.label}
            </button>
          ))}
        </div>

        <div className="mt-3">
          <div className="flex items-center gap-2">
            <span className="text-[13px] text-ink-secondary">申万行业</span>
            <input
              value={industryKeyword}
              onChange={(e) => setIndustryKeyword(e.target.value)}
              placeholder="搜索行业..."
              className="w-[160px] rounded border border-[#d0d3d9] px-2.5 py-[6px] text-[13px]"
            />
            {industries.length > 0 && (
              <span className="text-[11px] text-ink-faint">已选 {industries.length} 个</span>
            )}
          </div>
          <div className="mt-2 flex max-h-[104px] flex-wrap gap-1.5 overflow-y-auto">
            {filteredIndustries.map((i) => (
              <button
                key={i.name}
                type="button"
                onClick={() => toggleIndustry(i.name)}
                title={`覆盖 ${i.count} 只`}
                className={cn(
                  'rounded-full border px-2.5 py-1 text-[12px] transition-colors',
                  industries.includes(i.name) ? 'border-[#185fa5] bg-[#185fa5] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
                )}
              >
                {i.name} · {i.count}
              </button>
            ))}
            {filteredIndustries.length === 0 && (
              <span className="text-[12px] text-ink-faint">{industryNote || '无匹配行业'}</span>
            )}
          </div>
          {industries.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {industries.map((n) => (
                <span key={n} className="flex items-center gap-1 rounded-full bg-[#eef2f7] px-2.5 py-0.5 text-[12px] text-[#185fa5]">
                  {n}
                  <button type="button" onClick={() => toggleIndustry(n)} className="text-[#64748b] hover:text-[#185fa5]">×</button>
                </span>
              ))}
            </div>
          )}
        </div>
      </Card>

      {/* ---------- ③ 扫描参数 ---------- */}
      <Card className="mt-3">
        <button
          type="button"
          onClick={() => setShowAdvanced((v) => !v)}
          className="flex w-full items-center justify-between text-[13px] font-medium"
        >
          <span>③ 扫描参数</span>
          <span className="text-ink-faint">{showAdvanced ? '收起 ▲' : '展开 ▼'}</span>
        </button>
        {showAdvanced && (
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            <label className="flex items-center gap-2 text-[13px]">
              结果数量 TopN
              <input
                type="number" min={5} max={200}
                value={topN}
                onChange={(e) => setTopN(Number(e.target.value) || 30)}
                className="w-[70px] rounded border border-[#d0d3d9] px-2 py-1.5 text-[13px]"
              />
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              行业限配
              <select
                value={perIndustry}
                onChange={(e) => setPerIndustry(Number(e.target.value))}
                className="rounded border border-[#d0d3d9] px-2 py-1.5 text-[13px]"
              >
                {CAP_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              行业分级
              <select
                value={industryLevel}
                onChange={(e) => setIndustryLevel(e.target.value)}
                className="rounded border border-[#d0d3d9] px-2 py-1.5 text-[13px]"
              >
                {LEVEL_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              择时闸门
              <button
                type="button"
                onClick={() => setApplyGate((v) => !v)}
                className={cn('rounded-full border px-3 py-1 text-[12px]', applyGate ? 'border-[#185fa5] bg-[#185fa5] text-white' : 'border-[#d0d3d9]')}
              >
                {applyGate ? '开' : '关'}
              </button>
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              基本面+事件因子
              <button
                type="button"
                onClick={() => setApplyFactors((v) => !v)}
                className={cn('rounded-full border px-3 py-1 text-[12px]', applyFactors ? 'border-[#185fa5] bg-[#185fa5] text-white' : 'border-[#d0d3d9]')}
              >
                {applyFactors ? '开' : '关'}
              </button>
            </label>
          </div>
        )}
      </Card>

      {/* ---------- 扫描按钮 + 已选摘要 ---------- */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button onClick={run} disabled={scanning}>
          {scanning ? '扫描中...' : '开始扫描'}
        </Button>
        {task && <span className="text-xs text-ink-muted">最近任务: {task.status} · {task.done}/{task.total} · {task.progress}%</span>}
        {selectedChips.length > 0 && (
          <span className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-faint">
            已选:
            {selectedChips.map((c, i) => (
              <span key={`${c.label}-${i}`} className="rounded bg-[#f1f3f5] px-2 py-0.5 text-[#185fa5]">{c.label}</span>
            ))}
          </span>
        )}
      </div>
      {task?.error && <div className="mt-2 text-xs text-orange-500">任务异常: {task.error}</div>}

      {/* ---------- 结果 ---------- */}
      {task && task.status === 'done' && (
        <Card title={`排名 Top ${task.result.length}(日成交额 ≥ 5000万)`} className="mt-3">
          {task.result.length === 0 ? (
            <EmptyState>
              无结果。当前股票列表可能降级为自选池(东财列表接口风控期), 或均未达评分/流动性门槛。
            </EmptyState>
          ) : (
            <>
              <div className="mb-2 text-[11px] text-ink-faint">
                点击任意一行可展开三因子拆解与风险提示。标签配色: <span className="text-rise">红=利多</span> ·{' '}
                <span className="text-[#ea580c]">橙=需注意</span> · <span className="text-fall">绿=偏空</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="text-left text-ink-muted">
                      <th className="px-1.5 py-2">#</th>
                      <th className="px-1.5 py-2">代码</th>
                      <th className="px-1.5 py-2">名称</th>
                      <th className="px-1.5 py-2 text-right">总分</th>
                      <th className="px-1.5 py-2">阶段</th>
                      <th className="px-1.5 py-2 text-right">趋势</th>
                      <th className="px-1.5 py-2 text-right">动量</th>
                      <th className="px-1.5 py-2 text-right">量能</th>
                      <th className="px-1.5 py-2 text-right">现价</th>
                      <th className="px-1.5 py-2 text-right">ADX</th>
                      <th className="px-1.5 py-2 text-right">RSI</th>
                      <th className="px-1.5 py-2 text-right">量比</th>
                      <th className="px-1.5 py-2 text-right">额(亿)</th>
                      <th className="px-1.5 py-2">关注度</th>
                      <th className="px-1.5 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {task.result.map((r, i) => {
                      const open = expanded === r.symbol
                      const tags = r.tags ?? []
                      const risks = r.risk ? r.risk.split('；').filter(Boolean) : []
                      return (
                        <Fragment key={r.symbol}>
                          <tr
                            className="cursor-pointer border-t border-divider hover:bg-[#fafbfc]"
                            onClick={() => setExpanded(open ? null : r.symbol)}
                          >
                            <td className="px-1.5 py-2 text-ink-faint">{i + 1}</td>
                            <td className="px-1.5 py-2 font-semibold">{r.symbol}</td>
                            <td className="px-1.5 py-2">{r.name || '-'}</td>
                            <td className={cn('px-1.5 py-2 text-right font-bold', r.total >= 60 ? 'text-rise' : 'text-ink')}>{r.total.toFixed(1)}</td>
                            <td className="px-1.5 py-2">
                              {r.stage && r.stage !== 'none' ? (
                                <span className={cn('text-[12px] font-medium', STAGE_COLOR[r.stage] ?? 'text-ink-muted')}>
                                  {STAGE_LABEL[r.stage] ?? r.stage}
                                </span>
                              ) : (
                                <span className="text-ink-faint">-</span>
                              )}
                            </td>
                            <td className="px-1.5 py-2 text-right">{r.trend_score.toFixed(1)}</td>
                            <td className="px-1.5 py-2 text-right">{r.momentum_score.toFixed(1)}</td>
                            <td className="px-1.5 py-2 text-right">{r.volume_score.toFixed(1)}</td>
                            <td className="px-1.5 py-2 text-right">{r.close.toFixed(2)}</td>
                            <td className="px-1.5 py-2 text-right">{r.adx.toFixed(1)}</td>
                            <td className={cn('px-1.5 py-2 text-right', r.rsi > 80 ? 'text-[#ea580c]' : r.rsi < 40 ? 'text-fall' : '')}>
                              {r.rsi?.toFixed(0) ?? '-'}
                            </td>
                            <td className="px-1.5 py-2 text-right">{r.volume_ratio.toFixed(2)}</td>
                            <td className="px-1.5 py-2 text-right text-ink-muted">{r.amount_avg?.toFixed(1) ?? '-'}</td>
                            <td className="px-1.5 py-2">
                              <Tag color={r.attention === '强烈关注' ? '#dc2626' : r.attention === '重点观察' ? '#ea580c' : '#64748b'}>{r.attention}</Tag>
                            </td>
                            <td className="px-1.5 py-2 text-center text-[10px] text-ink-faint">{open ? '▲' : '▼'}</td>
                          </tr>

                          {/* 理由行: 标签常驻, 展开后追加三因子拆解与风险 */}
                          <tr className={cn(open ? 'bg-[#fafbfc]' : '')}>
                            <td colSpan={14} className="px-1.5 pb-2 align-top">
                              {tags.length === 0 && !r.reason ? (
                                <span className="text-[11px] text-ink-faint">本条结果由旧版本生成，重新扫描即可显示选股理由</span>
                              ) : (
                                <div className="flex flex-wrap items-center gap-1">
                                  {tags.map((t, k) => (
                                    <Tag key={`${t.text}-${k}`} color={TAG_COLOR[t.kind] ?? TAG_COLOR.info}>{t.text}</Tag>
                                  ))}
                                  {!open && risks.length > 0 && (
                                    <span className="text-[11px] text-[#c2410c]">
                                      ⚠ {risks[0]}{risks.length > 1 ? ` 等${risks.length}项` : ''}
                                    </span>
                                  )}
                                </div>
                              )}

                              {open && (
                                <div className="mt-2 rounded border border-line bg-white p-2.5">
                                  <div className="grid gap-2 md:grid-cols-3">
                                    {FACTORS.map((k) => (
                                      <div key={k}>
                                        <div className="mb-0.5 text-[11px] font-semibold text-ink-secondary">{k}</div>
                                        <div className="text-[12px] leading-[1.7] text-ink-muted">{r.detail?.[k] ?? '-'}</div>
                                      </div>
                                    ))}
                                  </div>
                                  {risks.length > 0 && (
                                    <div className="mt-2 rounded bg-[#fff7ed] px-2 py-1.5 text-[12px] leading-[1.7] text-[#c2410c]">
                                      <span className="font-semibold">风险提示：</span>
                                      {risks.join('；')}
                                    </div>
                                  )}
                                  <div className="mt-2 text-[11px] text-ink-faint">
                                    乖离率(现价相对短期均线) {r.bias?.toFixed(1) ?? '-'}% · 近20日均成交额 {r.amount_avg?.toFixed(2) ?? '-'} 亿 ·
                                    评分口径 趋势40 + 动量40 + 量能20
                                    {(r.stage_bonus ?? 0) > 0 || (r.stage_penalty ?? 0) > 0 ? (
                                      <span>
                                        {' '}· 阶段调整 {(r.stage_bonus ?? 0) > 0 ? `+${r.stage_bonus}` : ''}{(r.stage_penalty ?? 0) > 0 ? `-${r.stage_penalty}` : ''}
                                      </span>
                                    ) : null}
                                  </div>
                                </div>
                              )}
                            </td>
                          </tr>
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Card>
      )}
    </div>
  )
}
