import { Fragment, useEffect, useMemo, useState, type MouseEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { IndustryNode, InterruptedTask, ScreenerHistoryItem, ScreenerPreset, ScreenerTask } from '../api/client'
import { Button, Card, ConfirmDialog, EmptyState, ErrorBox, Loading, PageHeader, Table, Td, Th, Tag, inputStyle, toast } from '../components/ui'
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

// 结果表格的阶段筛选选项(含"无阶段": stage 缺失或为 none)
const STAGE_FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'launch', label: '启动期' },
  { value: 'accelerate', label: '加速期' },
  { value: 'overheat', label: '过热期' },
  { value: 'exhaust', label: '衰竭期' },
  { value: 'none', label: '无阶段' },
] as const

type StageFilter = (typeof STAGE_FILTERS)[number]['value']

/** 结果行的阶段归组键: 缺失/空/none 统一归为 'none' */
const stageKeyOf = (stage?: string) => (stage && stage !== 'none' ? stage : 'none')

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
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [error, setError] = useState('')

  // 股票条件: 指数池(多选, 空=全A) + 板块 + 申万行业; 默认上证50(全A串行扫描小时级, sz50 秒级)
  const [universeSel, setUniverseSel] = useState<string[]>(['sz50'])
  const [universeNote, setUniverseNote] = useState('')
  const [boards, setBoards] = useState<string[]>([])
  const [industries, setIndustries] = useState<string[]>([])
  const [industryNote, setIndustryNote] = useState('')
  const [expandedInds, setExpandedInds] = useState<Set<string>>(new Set()) // 树形展开的节点名

  // 条件组合预设(一键复用)
  const [presetName, setPresetName] = useState('')
  const [presetInputOpen, setPresetInputOpen] = useState(false)
  const [savingPreset, setSavingPreset] = useState(false)

  // ③ 扫描参数
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [topN, setTopN] = useState(30)
  const [perIndustry, setPerIndustry] = useState(0)
  const [industryLevel, setIndustryLevel] = useState('sw_l1')
  const [applyGate, setApplyGate] = useState(true)
  const [applyFactors, setApplyFactors] = useState(true)

  const [expanded, setExpanded] = useState<string | null>(null)
  // 分析记录(持久化落库): 历史列表 + 当前回看中的记录 id
  const [activeHistory, setActiveHistory] = useState<number | null>(null)
  // 结果阶段筛选(全部/启动/加速/过热/衰竭/无阶段)
  const [stageFilter, setStageFilter] = useState<StageFilter>('all')
  // 删除二次确认(项目规则): 待删除的目标
  const [confirmDel, setConfirmDel] = useState<{ type: 'history' | 'preset'; id: number } | null>(null)

  // ---- 静态数据(挂载加载, 操作后 invalidate 刷新) ----
  const { data: latestTask, isLoading } = useQuery({
    queryKey: ['screener', 'latest'],
    queryFn: api.screenerLatest,
  })
  const { data: history = [] } = useQuery({
    queryKey: ['screener', 'history'],
    queryFn: api.screenerHistory,
    select: (d) => d.items,
  })
  const { data: universeStats, error: uniQueryError } = useQuery({
    queryKey: ['screener', 'universe-stats'],
    queryFn: api.universeStats,
  })
  const { data: industryTree = [] } = useQuery({
    queryKey: ['screener', 'industry-tree'],
    queryFn: api.screenerIndustryTree,
    select: (d) => d.items,
  })
  const { data: presets = [] } = useQuery({
    queryKey: ['screener', 'presets'],
    queryFn: api.screenerPresets,
    select: (d) => d.items,
  })
  // 断点续传: 可恢复的中断任务(服务重启后自动标记, 可继续扫描; 完成/丢弃后失效)
  const { data: interruptedTasks = [] } = useQuery({
    queryKey: ['screener', 'interrupted'],
    queryFn: api.screenerInterruptedTasks,
    select: (d) => d.items,
  })
  const interrupted: InterruptedTask | null = interruptedTasks[0] ?? null

  // ---- 实时扫描任务: 触发后每 3s 轮询, 终态自动停止 ----
  const [taskId, setTaskId] = useState<string | null>(null)
  const { data: liveTask } = useQuery({
    queryKey: ['screener-task', taskId ?? 'none'],
    queryFn: () => api.screenerResult(taskId!),
    enabled: taskId !== null,
    refetchInterval: (q) => (q.state.data?.status === 'running' || q.state.data?.status === 'pending' ? 3000 : false),
  })
  // 历史回看: 独立 state 覆盖渲染(不干扰实时任务轮询)
  const [historyTask, setHistoryTask] = useState<ScreenerTask | null>(null)
  const task = historyTask ?? liveTask ??
    (latestTask && (latestTask.status === 'done' || latestTask.status === 'failed') ? latestTask : null)
  const running = taskId !== null && (liveTask === undefined || liveTask.status === 'running' || liveTask.status === 'pending')

  // 任务终态副作用: 刷新历史列表 + 清除中断提示 + 回到实时结果
  useEffect(() => {
    if (liveTask?.status === 'done' || liveTask?.status === 'failed') {
      setActiveHistory(null)
      setHistoryTask(null)
      queryClient.invalidateQueries({ queryKey: ['screener', 'history'] })
      queryClient.invalidateQueries({ queryKey: ['screener', 'interrupted'] })
      if (liveTask.status === 'failed') toast.error(liveTask.error || '扫描失败')
    }
  }, [liveTask?.status])

  // 选股池/行业树状态提示(数据到达后派生)
  useEffect(() => {
    if (universeStats && Object.keys(universeStats).length === 0) {
      setUniverseNote('成分股缓存为空, 请先在后端刷新选股池(Swagger: POST /screener/universe/refresh)')
    }
  }, [universeStats])
  useEffect(() => {
    if (uniQueryError) setUniverseNote('选股池状态读取失败')
  }, [uniQueryError])
  useEffect(() => {
    if (industryTree.length === 0) {
      setIndustryNote('行业数据为空, 需先刷新申万分类映射(设置->数据源或 Swagger: POST /screener/classification/refresh)')
    }
  }, [industryTree])

  const refreshHistory = () => queryClient.invalidateQueries({ queryKey: ['screener', 'history'] })
  const refreshPresets = () => queryClient.invalidateQueries({ queryKey: ['screener', 'presets'] })

  // 指数池多选: 点"全部A股"清空选择(空=全A); 点指数加入/移除
  const toggleUniverse = (key: string) => {
    if (key === 'all') {
      setUniverseSel([])
      return
    }
    setUniverseSel((prev) => (prev.includes(key) ? prev.filter((x) => x !== key) : [...prev, key]))
  }

  // 指数池 chip 展示名(含预置组合 key 兼容)
  const universeLabelOf = (key: string) => {
    const u = universeStats?.[key]
    if (u?.label) return u.label
    if (key === UNIVERSE_COMBO.value) return UNIVERSE_COMBO.label
    return key
  }

  // 保存当前条件为预设
  const savePreset = async () => {
    const name = presetName.trim()
    if (!name) {
      toast.error('请输入预设名称')
      return
    }
    setSavingPreset(true)
    try {
      await api.saveScreenerPreset({
        name,
        universe: universeSel.join(',') || undefined,
        board: boards.join(',') || undefined,
        industry: industries.join(',') || undefined,
      })
      toast.success(`预设「${name}」已保存`)
      setPresetName('')
      setPresetInputOpen(false)
      refreshPresets()
    } catch (e) {
      toast.error(String((e as Error).message))
    } finally {
      setSavingPreset(false)
    }
  }

  // 应用预设: 一键填充条件(同名覆盖)
  const applyPreset = (p: ScreenerPreset) => {
    setUniverseSel((p.universe || '').split(',').filter(Boolean))
    setBoards((p.board || '').split(',').filter(Boolean))
    setIndustries((p.industry || '').split(',').filter(Boolean))
    toast.success(`已应用预设「${p.name}」`)
  }

  // 删除预设(二次确认)
  const removePreset = (e: MouseEvent, id: number) => {
    e.stopPropagation()
    setConfirmDel({ type: 'preset', id })
  }

  // 确认删除(分析记录/条件预设 共用)
  const doDelete = async () => {
    if (!confirmDel) return
    const { type, id } = confirmDel
    try {
      if (type === 'history') {
        await api.deleteScreenerHistory(id)
        toast.success('记录已删除')
        refreshHistory()
        if (activeHistory === id) setActiveHistory(null)
      } else {
        await api.deleteScreenerPreset(id)
        toast.success('预设已删除')
        refreshPresets()
      }
    } catch (err) {
      toast.error(String((err as Error).message))
    } finally {
      setConfirmDel(null)
    }
  }

  const toggleBoard = (b: string) =>
    setBoards((prev) => (prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b]))

  const toggleIndustry = (name: string) =>
    setIndustries((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]))

  const toggleExpand = (name: string) =>
    setExpandedInds((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const run = async (resumeTaskId?: string) => {
    setError('')
    try {
      const { task_id } = await api.screenerRun(
        'all', topN,
        boards.join(',') || undefined,
        industries.join(',') || undefined,
        // 关键: 未选指数池时显式传 'all'(而不是 undefined), 否则后端会回退到配置默认 sz50
        universeSel.join(',') || 'all',
        { perIndustry, industryLevel, applyGate, applyFactors },
        resumeTaskId,
      )
      toast.info(resumeTaskId ? '已从断点继续扫描(跳过已完成票, 结果保留)' : '扫描已启动, 完成后自动刷新结果')
      setHistoryTask(null)
      setTaskId(task_id)
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    }
  }

  // 各阶段命中数(筛选条上显示, 随结果/筛选自动更新)
  const stageCounts = useMemo(() => {
    const counts: Record<string, number> = { all: task?.result.length ?? 0 }
    for (const r of task?.result ?? []) {
      const k = stageKeyOf(r.stage)
      counts[k] = (counts[k] ?? 0) + 1
    }
    return counts
  }, [task])

  // 按阶段筛选后的结果(历史回看与实时任务共用同一渲染)
  const filteredResult = useMemo(() => {
    const list = task?.result ?? []
    if (!stageFilter || stageFilter === 'all') return list
    return list.filter((r) => stageKeyOf(r.stage) === stageFilter)
  }, [task, stageFilter])

  if (isLoading) return <Loading />

  const scanning = running || (task?.status === 'running' || task?.status === 'pending')

  // 点击历史记录 -> 加载该次扫描结果(无需重新分析)
  const loadHistory = async (id: number) => {
    setActiveHistory(id)
    try {
      const h = await api.screenerHistoryDetail(id)
      setHistoryTask({
        id: `h${id}`,
        status: h.status === 'failed' ? 'failed' : 'done',
        market: h.market,
        top_n: h.top_n,
        total: h.total,
        done: h.total,
        progress: 100,
        result: h.result ?? [],
        error: h.error ?? '',
      })
      setExpanded(null)
    } catch (e) {
      toast.error(String((e as Error).message))
    }
  }

  // 删除单条历史记录(二次确认)
  const removeHistory = (e: MouseEvent, id: number) => {
    e.stopPropagation()
    setConfirmDel({ type: 'history', id })
  }

  // 历史记录参数摘要(如: 沪深300+中证500成分 · 主板/科创板 · Top30)
  const historySummary = (h: ScreenerHistoryItem) => {
    const parts: string[] = []
    if (h.universe) {
      parts.push(h.universe.split(',').map((u) => universeLabelOf(u)).join('+'))
    }
    if (h.board) parts.push(h.board.split(',').map((b) => BOARDS.find((x) => x.value === b)?.label ?? b).join('/'))
    if (h.industry) parts.push(`${h.industry.split(',').length} 个行业`)
    parts.push(`Top${h.top_n}`)
    if (!h.apply_gate) parts.push('闸门关')
    if (!h.apply_factors) parts.push('因子关')
    return parts.join(' · ') || '全A'
  }

  // 已应用条件 chips(带类型, 便于单独删除)
  const conditionChips = [
    ...universeSel.map((u) => ({ key: `u-${u}`, type: 'universe' as const, label: universeLabelOf(u) })),
    ...boards.map((b) => ({ key: `b-${b}`, type: 'board' as const, label: BOARDS.find((x) => x.value === b)?.label ?? b })),
    ...industries.map((n) => ({ key: `i-${n}`, type: 'industry' as const, label: n })),
  ]

  const removeCondition = (type: 'universe' | 'board' | 'industry', value: string) => {
    if (type === 'universe') setUniverseSel((prev) => prev.filter((x) => x !== value))
    if (type === 'board') setBoards((prev) => prev.filter((x) => x !== value))
    if (type === 'industry') setIndustries((prev) => prev.filter((x) => x !== value))
  }

  const clearConditions = () => {
    setUniverseSel([])
    setBoards([])
    setIndustries([])
  }

  // 丢弃中断任务
  const discardTask = async () => {
    if (!interrupted) return
    try {
      await api.deleteScreenerTask(interrupted.task_id)
      toast.success('已丢弃中断任务')
      queryClient.invalidateQueries({ queryKey: ['screener', 'interrupted'] })
    } catch (e) {
      toast.error(String((e as Error).message))
    }
  }

  return (
    <div>
      <PageHeader title="选股" />
      {error && <ErrorBox message={error} />}

      {/* ---------- 股票条件(统一过滤器: 指数池+板块+行业) ---------- */}
      <Card className="mt-3">
        {/* 已应用条件 chips(带 × 可单独删除) */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[13px] font-medium">股票条件</span>
          {conditionChips.length === 0 ? (
            <span className="text-[11px] text-ink-faint">未选条件(扫描全A)</span>
          ) : (
            <>
              <span className="text-[11px] text-ink-faint">已应用 {conditionChips.length} 个:</span>
              {conditionChips.map((c) => (
                <span key={c.key} className="flex items-center gap-1 rounded-full bg-[#eef2f7] px-2.5 py-0.5 text-[12px] text-[#185fa5]">
                  {c.label}
                  <button type="button" onClick={() => removeCondition(c.type, c.key.slice(2))} className="text-[#64748b] hover:text-[#185fa5]">×</button>
                </span>
              ))}
              <button type="button" onClick={clearConditions} className="text-[11px] text-ink-faint hover:text-rise">清空全部</button>
            </>
          )}
        </div>

        <div className="my-3 border-t border-divider" />

        {/* 指数池(多选, 空=全A) */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-ink-secondary">指数池(多选)</span>
          <button
            type="button"
            onClick={() => toggleUniverse('all')}
            className={cn(
              'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
              universeSel.length === 0 ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
            )}
          >
            全部A股
          </button>
          {universeStats && Object.entries(universeStats).map(([key, v]) => (
            <button
              key={key}
              type="button"
              onClick={() => toggleUniverse(key)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
                universeSel.includes(key) ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
              )}
            >
              {v.label}成分 · {v.count}只
            </button>
          ))}
          {universeStats && UNIVERSE_COMBO.needs.every((k) => universeStats[k]?.count > 0) && (
            <button
              type="button"
              onClick={() => toggleUniverse(UNIVERSE_COMBO.value)}
              className={cn(
                'rounded-full border px-3 py-1.5 text-[13px] transition-colors',
                universeSel.includes(UNIVERSE_COMBO.value) ? 'border-[#dc2626] bg-[#dc2626] text-white' : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
              )}
            >
              {UNIVERSE_COMBO.label}
            </button>
          )}
        </div>
        {universeNote && <p className="mt-2 text-[11px] text-[#ea580c]">{universeNote}</p>}
        {universeSel.length > 0 ? (
          <div className="mt-1.5 text-[11px] text-[#ea580c]">
            当前限制在指数池内（{universeSel.map((u) => universeLabelOf(u)).join('、')}成分 ∩ 其他条件）。
            默认上证50（扫描最快）；点「全部A股」可清除指数池限制扫全A（耗时较长）。
          </div>
        ) : (
          <div className="mt-1.5 text-[11px] text-ink-faint">
            当前不限指数池（扫描全A，耗时较长；建议选择指数池加速）。
          </div>
        )}

        <div className="my-3 border-t border-divider" />

        {/* 上市板块(多选) */}
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
            <span className="text-[13px] text-ink-secondary">申万行业(三级树)</span>
            {industries.length > 0 && (
              <span className="text-[11px] text-ink-faint">已选 {industries.length} 个</span>
            )}
          </div>
          <div className="mt-2 max-h-[240px] overflow-y-auto rounded border border-divider p-1.5">
            {industryTree.length === 0 ? (
              <span className="text-[12px] text-ink-faint">{industryNote || '无行业数据'}</span>
            ) : (
              industryTree.map((n) => (
                <TreeRow
                  key={n.name}
                  node={n}
                  depth={0}
                  expanded={expandedInds}
                  selected={industries}
                  onToggleSelect={toggleIndustry}
                  onToggleExpand={toggleExpand}
                />
              ))
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

        <div className="mt-2 text-[11px] text-ink-faint">
          条件逻辑: 组内任一命中(OR), 组间全部满足(AND)。例如 指数池[沪深300或中证500] 且 板块[科创板] 且 行业[半导体]。
        </div>

        <div className="my-3 border-t border-divider" />

        {/* 条件预设(一键复用) */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-ink-secondary">条件预设</span>
          {presetInputOpen ? (
            <span className="flex items-center gap-1.5">
              <input
                value={presetName}
                onChange={(e) => setPresetName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && savePreset()}
                placeholder="预设名称"
                style={{ ...inputStyle, width: 130 }}
              />
              <Button onClick={savePreset} disabled={savingPreset} style={{ padding: '4px 10px', fontSize: 12 }} className="h-7">{savingPreset ? '保存中' : '保存'}</Button>
              <button
                type="button"
                onClick={() => { setPresetInputOpen(false); setPresetName('') }}
                className="text-[11px] text-ink-faint hover:text-ink"
              >
                取消
              </button>
            </span>
          ) : (
            <Button kind="ghost" onClick={() => setPresetInputOpen(true)} style={{ padding: '4px 10px', fontSize: 12 }} className="h-7">保存当前条件</Button>
          )}
          {presets.map((p) => (
            <span
              key={p.id}
              className="group flex cursor-pointer items-center gap-1 rounded-full border border-[#d0d3d9] px-2.5 py-0.5 text-[12px] transition-colors hover:border-[#185fa5] hover:text-[#185fa5]"
              onClick={() => applyPreset(p)}
              title={`指数:${p.universe || '全A'} · 板块:${p.board || '不限'} · 行业:${p.industry || '不限'}`}
            >
              {p.name}
              <button
                type="button"
                onClick={(e) => removePreset(e, p.id)}
                className="text-[#64748b] opacity-0 transition-opacity group-hover:opacity-100 hover:text-rise"
              >
                ×
              </button>
            </span>
          ))}
          {presets.length === 0 && !presetInputOpen && (
            <span className="text-[11px] text-ink-faint">保存常用条件组合, 一键复用</span>
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
                style={{ ...inputStyle, width: 70 }}
              />
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              行业限配
              <select
                value={perIndustry}
                onChange={(e) => setPerIndustry(Number(e.target.value))}
                style={{ ...inputStyle, width: 'auto' }}
              >
                {CAP_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2 text-[13px]">
              行业分级
              <select
                value={industryLevel}
                onChange={(e) => setIndustryLevel(e.target.value)}
                style={{ ...inputStyle, width: 'auto' }}
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
        <Button onClick={() => run()} disabled={scanning}>
          {scanning ? '扫描中...' : '开始扫描'}
        </Button>
        {task && <span className="text-xs text-ink-muted">最近任务: {task.status} · {task.done}/{task.total} · {task.progress}%</span>}
        {conditionChips.length > 0 && (
          <span className="text-xs text-ink-faint">已应用 {conditionChips.length} 个条件</span>
        )}
      </div>

      {/* 断点续传: 中断任务提示(可继续/重扫/丢弃) */}
      {interrupted && !scanning && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-[#f59e0b]/40 bg-[#fffbeb] px-3 py-2 text-[12px]">
          <span className="text-[#b45309]">
            上次扫描中断: {interrupted.done}/{interrupted.total}({interrupted.total ? Math.round(interrupted.done / interrupted.total * 100) : 0}%),
            可继续扫描(跳过已完成, 已扫结果保留)
          </span>
          <Button onClick={() => run(interrupted.task_id)} className="h-7 px-2 text-xs">继续扫描</Button>
          <Button kind="ghost" onClick={() => run()} className="h-7 px-2 text-xs">重新扫描</Button>
          <button
            type="button"
            onClick={discardTask}
            className="cursor-pointer border-none bg-transparent text-[11px] text-ink-faint hover:text-rise"
          >
            丢弃
          </button>
        </div>
      )}
      {task?.error && <div className="mt-2 text-xs text-orange-500">任务异常: {task.error}</div>}

      {/* ---------- 分析记录(持久化, 点击回看) ---------- */}
      <Card title={`分析记录(${history.length})`} className="mt-3">
        {history.length === 0 ? (
          <EmptyState>暂无历史记录。每次扫描完成后自动保存, 点击记录即可回看结果, 无需重新分析。</EmptyState>
        ) : (
          <div className="max-h-56 overflow-y-auto">
            {history.map((h) => {
              const active = activeHistory === h.id
              return (
                <div
                  key={h.id}
                  className={cn(
                    'group flex cursor-pointer items-center justify-between gap-2 border-b border-divider py-2 text-[13px] last:border-b-0',
                    active ? 'bg-[#fff5f5]' : 'hover:bg-[#fafbfc]',
                  )}
                  onClick={() => loadHistory(h.id)}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="shrink-0 font-semibold text-ink-secondary">{h.time.slice(5, 16)}</span>
                    <span className="truncate text-ink-muted">{historySummary(h)}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className="text-xs text-ink-faint">{h.result_count} 只</span>
                    <button
                      type="button"
                      onClick={(e) => removeHistory(e, h.id)}
                      className="text-[11px] text-ink-faint opacity-0 transition-opacity group-hover:opacity-100 hover:text-rise"
                    >
                      删除
                    </button>
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      {/* ---------- 结果 ---------- */}
      {task && task.status === 'done' && (
        <Card title={`排名 Top ${task.result.length}(日成交额 ≥ 5000万)`} className="mt-3">
          {task.result.length === 0 ? (
            <EmptyState>
              {conditionChips.length > 0 ? (
                <>
                  当前条件组合无匹配股票：{conditionChips.map((c) => c.label).join(' ∩ ')} 交集可能为空。
                  常见原因: 指数池(如上证50)内没有所选行业/板块的票。建议点「全部A股」清除指数池限制，或放宽板块/行业条件。
                </>
              ) : (
                '无结果。当前股票列表可能降级为自选池(东财列表接口风控期), 或均未达评分/流动性门槛。'
              )}
            </EmptyState>
          ) : filteredResult.length === 0 ? (
            <EmptyState>该阶段筛选下无结果, 切换其他阶段查看。</EmptyState>
          ) : (
            <>
              <div className="mb-2 text-[11px] text-ink-faint">
                点击任意一行可展开三因子拆解与风险提示。标签配色: <span className="text-rise">红=利多</span> ·{' '}
                <span className="text-[#ea580c]">橙=需注意</span> · <span className="text-fall">绿=偏空</span>
              </div>
              {/* 阶段筛选条: 与结果同源, 历史回看同样生效 */}
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-ink-secondary">阶段筛选:</span>
                {STAGE_FILTERS.map((f) => (
                  <button
                    key={f.value}
                    type="button"
                    onClick={() => setStageFilter(f.value)}
                    className={cn(
                      'rounded-full border px-2.5 py-1 text-[12px] transition-colors',
                      stageFilter === f.value
                        ? 'border-[#dc2626] bg-[#dc2626] text-white'
                        : 'border-[#d0d3d9] hover:border-[#a0a5ad]',
                    )}
                  >
                    {f.label}({stageCounts[f.value] ?? 0})
                  </button>
                ))}
              </div>
              <Table>
                <thead>
                  <tr>
                    <Th>#</Th>
                    <Th>代码</Th>
                    <Th>名称</Th>
                    <Th right>总分</Th>
                    <Th>阶段</Th>
                    <Th right>趋势</Th>
                    <Th right>动量</Th>
                    <Th right>量能</Th>
                    <Th right>现价</Th>
                    <Th right>ADX</Th>
                    <Th right>RSI</Th>
                    <Th right>量比</Th>
                    <Th right>额(亿)</Th>
                    <Th>关注度</Th>
                    <Th />
                    <Th />
                    <Th />
                  </tr>
                </thead>
                <tbody>
                  {filteredResult.map((r, i) => {
                    const open = expanded === r.symbol
                    const tags = r.tags ?? []
                    const risks = r.risk ? r.risk.split('；').filter(Boolean) : []
                    return (
                      <Fragment key={r.symbol}>
                        <tr
                          className="cursor-pointer border-t border-divider hover:bg-[#fafbfc]"
                          onClick={() => setExpanded(open ? null : r.symbol)}
                        >
                          <Td className="text-ink-faint">{i + 1}</Td>
                          <Td className="font-semibold">{r.symbol}</Td>
                          <Td>{r.name || '-'}</Td>
                          <Td right className={cn('font-bold', r.total >= 60 ? 'text-rise' : 'text-ink')}>{r.total.toFixed(1)}</Td>
                          <Td>
                            {r.stage && r.stage !== 'none' ? (
                              <span className={cn('text-[12px] font-medium', STAGE_COLOR[r.stage] ?? 'text-ink-muted')}>
                                {STAGE_LABEL[r.stage] ?? r.stage}
                              </span>
                            ) : (
                              <span className="text-ink-faint">-</span>
                            )}
                          </Td>
                          <Td right>{r.trend_score.toFixed(1)}</Td>
                          <Td right>{r.momentum_score.toFixed(1)}</Td>
                          <Td right>{r.volume_score.toFixed(1)}</Td>
                          <Td right>{r.close.toFixed(2)}</Td>
                          <Td right>{r.adx.toFixed(1)}</Td>
                          <Td right className={cn(r.rsi > 80 ? 'text-[#ea580c]' : r.rsi < 40 ? 'text-fall' : '')}>
                            {r.rsi?.toFixed(0) ?? '-'}
                          </Td>
                          <Td right>{r.volume_ratio.toFixed(2)}</Td>
                          <Td right className="text-ink-muted">{r.amount_avg?.toFixed(1) ?? '-'}</Td>
                          <Td>
                            <Tag color={r.attention === '强烈关注' ? '#dc2626' : r.attention === '重点观察' ? '#ea580c' : '#64748b'}>{r.attention}</Tag>
                          </Td>
                          {/* 快速进入信号流程: 跳转信号中心并自动评估该票 */}
                          <Td className="text-center">
                            <div onClick={(e) => e.stopPropagation()}>
                              <Button
                                kind="primary"
                                className="h-6 px-2 text-[11px]"
                                onClick={() => navigate(`/signals?symbol=${r.symbol}&name=${encodeURIComponent(r.name || '')}`)}
                              >
                                看信号
                              </Button>
                            </div>
                          </Td>
                          {/* 加入得分追踪: 每日 3 次采样得分, 观察分数与涨跌关系 */}
                          <Td className="text-center">
                            <div onClick={(e) => e.stopPropagation()}>
                              <Button
                                kind="ghost"
                                className="h-6 px-2 text-[11px]"
                                onClick={() => {
                                  api.trackingAdd({
                                    symbol: r.symbol,
                                    name: r.name || '',
                                    score: r.total,
                                    stage: r.stage,
                                  }).then(() => {
                                    toast.success(`已加入得分追踪: ${r.symbol}`)
                                  }).catch((err) => toast.error(String((err as Error).message)))
                                }}
                              >
                                追踪
                              </Button>
                            </div>
                          </Td>
                          <Td className="text-center text-[10px] text-ink-faint">{open ? '▲' : '▼'}</Td>
                        </tr>

                        {/* 理由行: 标签常驻, 展开后追加三因子拆解与风险 */}
                        <tr className={cn(open ? 'bg-[#fafbfc]' : '')}>
                          <Td colSpan={15} className="pb-2">
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
                                    乖离率(现价相对短期均线) {r.bias?.toFixed(1) ?? '-'}% · 近20日均成交额 {r.amount_avg?.toFixed(2) ?? '-'} 亿
                                    <div className="mt-1">
                                      评分口径 趋势40 + 动量40 + 量能20
                                      {(r.stage_bonus ?? 0) > 0 || (r.stage_penalty ?? 0) > 0 ? (
                                        <span>
                                          {' '}· 阶段调整 {(r.stage_bonus ?? 0) > 0 ? `+${r.stage_bonus}` : ''}{(r.stage_penalty ?? 0) > 0 ? `-${r.stage_penalty}` : ''}
                                        </span>
                                      ) : null}
                                      {typeof r.factor_delta === 'number' && r.factor_delta !== 0 && (
                                        <span>{' '}· 因子调整 {r.factor_delta > 0 ? '+' : ''}{r.factor_delta.toFixed(1)}</span>
                                      )}
                                      <span className="font-semibold text-ink-secondary">{' '}· 合计 {r.total.toFixed(1)}</span>
                                    </div>
                                  </div>
                                </div>
                              )}
                            </Td>
                          </tr>
                        </Fragment>
                      )
                    })}
                  </tbody>
                </Table>
            </>
          )}
        </Card>
      )}

      {/* 删除二次确认(项目规则) */}
      {confirmDel && (
        <ConfirmDialog
          title={confirmDel.type === 'history' ? '删除分析记录' : '删除条件预设'}
          message={confirmDel.type === 'history' ? '删除后无法恢复，确定删除该条分析记录？' : '删除后无法恢复，确定删除该条件预设？'}
          onConfirm={doDelete}
          onCancel={() => setConfirmDel(null)}
        />
      )}
    </div>
  )
}

// 申万三级行业树行(递归): 箭头展开/收起 + 复选框多选, 任意级节点均可选中
function TreeRow({ node, depth, expanded, selected, onToggleSelect, onToggleExpand }: {
  node: IndustryNode
  depth: number
  expanded: Set<string>
  selected: string[]
  onToggleSelect: (name: string) => void
  onToggleExpand: (name: string) => void
}) {
  const hasKids = !!node.children?.length
  const isExpanded = expanded.has(node.name)
  return (
    <div>
      <div className="flex items-center gap-1 py-[3px] text-[13px]" style={{ paddingLeft: depth * 16 }}>
        <button
          type="button"
          onClick={() => hasKids && onToggleExpand(node.name)}
          className={`w-4 shrink-0 text-center text-[10px] ${hasKids ? 'text-ink-muted hover:text-ink' : 'text-transparent'}`}
        >
          {hasKids ? (isExpanded ? '▾' : '▸') : '·'}
        </button>
        <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 select-none">
          <input
            type="checkbox"
            checked={selected.includes(node.name)}
            onChange={() => onToggleSelect(node.name)}
            className="h-3.5 w-3.5 shrink-0 accent-[#dc2626]"
          />
          <span className={cn('truncate', depth === 0 ? 'font-medium text-ink' : depth === 1 ? 'text-ink-secondary' : 'text-ink-muted')}>
            {node.name}
          </span>
          <span className="ml-auto shrink-0 text-[11px] text-ink-faint">{node.count}</span>
        </label>
      </div>
      {hasKids && isExpanded && node.children!.map((c) => (
        <TreeRow
          key={`${depth + 1}-${c.name}`}
          node={c}
          depth={depth + 1}
          expanded={expanded}
          selected={selected}
          onToggleSelect={onToggleSelect}
          onToggleExpand={onToggleExpand}
        />
      ))}
    </div>
  )
}
