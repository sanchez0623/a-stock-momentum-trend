import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ScorePoint, TrackedHistory, TrackedStock } from '../api/client'
import { Button, Card, ConfirmDialog, EmptyState, Loading, PageHeader, Tag, toast } from '../components/ui'
import { ScoreChart } from '../components/charts/ScoreChart'

const STAGE_LABEL: Record<string, string> = {
  launch: '启动期', accelerate: '加速期', overheat: '过热期', exhaust: '衰竭期',
  // 加速期细分(与选股页一致): 前期=首仓区 / 中期=主升段 / 后期=逼近过热
  'accelerate:early': '加速前期', 'accelerate:mid': '加速中期', 'accelerate:late': '加速后期',
}
const STAGE_COLOR: Record<string, string> = {
  launch: '#dc2626', accelerate: '#dc2626', overheat: '#ea580c', exhaust: '#16a34a',
  'accelerate:early': '#dc2626', 'accelerate:mid': '#b91c1c', 'accelerate:late': '#ea580c',
}
/** 阶段组合键: 加速期细分为 accelerate:子阶段(旧数据无子阶段时保留原值) */
const stageKeyOf = (stage?: string, sub?: string) => {
  if (!stage) return ''
  return stage === 'accelerate' && sub ? `accelerate:${sub}` : stage
}
const KIND_LABEL: Record<string, string> = { pre: '盘前', noon: '午间', after: '盘后', manual: '手动' }
const SIM_ACTION_LABEL: Record<string, string> = {
  open: '建仓', add: '加仓', reduce: '减仓', close: '平仓', hold: '持有',
}
// 归档原因: manual 手动 / expired 30天到期 / exhaust 衰竭自动退出
const REASON_LABEL: Record<string, string> = {
  manual: '手动停止', expired: '观察期满(30天)', exhaust: '衰竭退出',
}
const REASON_COLOR: Record<string, string> = {
  manual: '#64748b', expired: '#64748b', exhaust: '#ea580c',
}

function StockCard({ st }: { st: TrackedStock }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [delPoint, setDelPoint] = useState<ScorePoint | null>(null) // 删除单条采样(二次确认)
  const { data: pts = [] } = useQuery({
    queryKey: ['tracking', 'points', st.symbol],
    queryFn: () => api.trackingPoints(st.symbol),
    select: (d) => d.items,
    enabled: expanded,
  })
  const first = st.score_at_track
  const last = st.latest?.score
  const delta = last !== undefined ? last - first : null
  const chg = st.latest?.price && pts.length > 0
    ? ((st.latest.price - pts[0].price) / pts[0].price) * 100 : null
  return (
    <Card className="mb-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <button type="button" onClick={() => setExpanded(!expanded)} className="cursor-pointer border-none bg-transparent text-left">
          <span className="text-[14px] font-semibold text-ink">{st.name || st.symbol} <span className="font-normal text-ink-faint">{st.symbol}</span></span>
        </button>
        {st.stage_at_track && (
          <Tag color={STAGE_COLOR[stageKeyOf(st.stage_at_track, st.stage_sub_at_track)] ?? '#64748b'}>
            追踪时:{STAGE_LABEL[stageKeyOf(st.stage_at_track, st.stage_sub_at_track)] ?? st.stage_at_track}
          </Tag>
        )}
        {st.latest?.stage && (
          <Tag color={STAGE_COLOR[stageKeyOf(st.latest.stage, st.latest.stage_sub)] ?? '#64748b'}>
            最新:{STAGE_LABEL[stageKeyOf(st.latest.stage, st.latest.stage_sub)] ?? st.latest.stage}
          </Tag>
        )}
        {st.latest?.stage === 'overheat' && (
          <Tag color="#ea580c">⚠ 已过热·注意减仓节奏</Tag>
        )}
        <span className="text-[12px] text-ink-muted">追踪分 <b className="text-ink">{first.toFixed(1)}</b></span>
        {last !== undefined && (
          <span className="text-[12px] text-ink-muted">
            最新 <b className="text-ink">{last.toFixed(1)}</b>
            <span className={delta! >= 0 ? 'text-rise' : 'text-fall'}> ({delta! >= 0 ? '+' : ''}{delta!.toFixed(1)})</span>
          </span>
        )}
        {chg !== null && (
          <span className={`text-[12px] ${chg >= 0 ? 'text-rise' : 'text-fall'}`}>{chg >= 0 ? '+' : ''}{chg.toFixed(1)}%</span>
        )}
        {st.latest && (
          <span className="text-[11px] text-ink-faint">采样 {st.latest.time.slice(5, 16)} ({KIND_LABEL[st.latest.sample_kind] ?? st.latest.sample_kind})</span>
        )}
        {/* 模拟交易状态 */}
        <span className="text-[11px]">
          {st.sim_qty > 0 ? (
            <>
              <span className="text-ink-muted">模拟持有 {st.sim_qty}股 @{st.sim_cost.toFixed(2)}</span>
              {st.latest && (
                <span className={st.latest.sim_pnl >= 0 ? 'text-rise' : 'text-fall'}>
                  {' '}{st.latest.sim_pnl >= 0 ? '+' : ''}{st.latest.sim_pnl.toFixed(1)}%
                </span>
              )}
            </>
          ) : (
            <span className="text-ink-faint">模拟空仓{st.sim_realized_pnl ? <span className={st.sim_realized_pnl >= 0 ? ' text-rise' : ' text-fall'}> · 累计 {st.sim_realized_pnl >= 0 ? '+' : ''}{st.sim_realized_pnl.toFixed(1)}%</span> : ''}</span>
          )}
        </span>
        <span className="ml-auto flex gap-2">
          {st.latest?.signal_type && <Tag color="#9333ea">{st.latest.signal_type}</Tag>}
          <button
            type="button"
            onClick={() => {
              api.trackingRemove(st.symbol).then(() => {
                toast.success(`已停止追踪 ${st.symbol}`)
                window.location.reload()
              }).catch((e) => toast.error(String((e as Error).message)))
            }}
            className="cursor-pointer border-none bg-transparent text-[11px] text-ink-faint hover:text-rise"
          >
            停止追踪
          </button>
        </span>
      </div>
      {expanded && (
        <div className="mt-3 border-t border-divider pt-3">
          <ScoreChart points={pts as ScorePoint[]} />
          <table className="mt-2 w-full border-collapse text-[11px]">
            <thead>
              <tr className="border-b border-divider text-left text-ink-faint">
                <th className="px-1 py-1 font-medium">采样时间</th>
                <th className="px-1 py-1 font-medium">类型</th>
                <th className="px-1 py-1 font-medium">总分</th>
                <th className="px-1 py-1 font-medium">趋势</th>
                <th className="px-1 py-1 font-medium">动量</th>
                <th className="px-1 py-1 font-medium">量能</th>
                <th className="px-1 py-1 font-medium">阶段</th>
                <th className="px-1 py-1 font-medium">价格</th>
                <th className="px-1 py-1 font-medium">量比</th>
                <th className="px-1 py-1 font-medium">信号</th>
                <th className="px-1 py-1 font-medium">模拟</th>
              </tr>
            </thead>
            <tbody>
              {pts.map((p, i) => (
                <tr key={i} className="border-b border-divider/60 last:border-b-0">
                  <td className="px-1 py-1">{p.time.slice(5, 16)}</td>
                  <td className="px-1 py-1 text-ink-faint">{KIND_LABEL[p.sample_kind] ?? p.sample_kind}</td>
                  <td className="px-1 py-1 font-semibold">{p.score.toFixed(1)}</td>
                  <td className="px-1 py-1">{p.trend_score.toFixed(1)}</td>
                  <td className="px-1 py-1">{p.momentum_score.toFixed(1)}</td>
                  <td className="px-1 py-1">{p.volume_score.toFixed(1)}</td>
                  <td className="px-1 py-1"><span style={{ color: STAGE_COLOR[stageKeyOf(p.stage, p.stage_sub)] }}>{STAGE_LABEL[stageKeyOf(p.stage, p.stage_sub)] ?? (p.stage || '-')}</span></td>
                  <td className="px-1 py-1">{p.price.toFixed(2)}</td>
                  <td className="px-1 py-1">{p.volume_ratio.toFixed(2)}</td>
                  <td className="px-1 py-1">{p.signal_type || '-'}</td>
                  <td className="px-1 py-1">
                    <span className={p.sim_action === 'open' || p.sim_action === 'add' ? 'text-rise' : p.sim_action === 'close' || p.sim_action === 'reduce' ? 'text-fall' : 'text-ink-faint'}>
                      {SIM_ACTION_LABEL[p.sim_action] ?? (p.sim_qty > 0 ? '持有' : '-')}
                    </span>
                    {p.sim_pnl !== 0 && (
                      <span className={p.sim_pnl >= 0 ? 'text-rise' : 'text-fall'}>
                        {' '}{p.sim_pnl >= 0 ? '+' : ''}{p.sim_pnl.toFixed(1)}%
                      </span>
                    )}
                  </td>
                  <td className="px-1 py-1 text-center">
                    <button
                      type="button"
                      onClick={() => setDelPoint(p)}
                      className="cursor-pointer border-none bg-transparent text-[10px] text-ink-faint hover:text-rise"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {delPoint && (
            <ConfirmDialog
              title="删除采样点"
              message={`确定删除 ${delPoint.time.slice(5, 16)} 的采样(得分 ${delPoint.score.toFixed(1)} / 价格 ${delPoint.price.toFixed(2)})？删除后无法恢复。`}
              confirmText="删除"
              onConfirm={async () => {
                await api.trackingDeletePoint(delPoint.id as number)
                toast.success('采样点已删除')
                setDelPoint(null)
                queryClient.invalidateQueries({ queryKey: ['tracking', 'points', st.symbol] })
              }}
              onCancel={() => setDelPoint(null)}
            />
          )}
        </div>
      )}
    </Card>
  )
}

/** 历史档成绩卡片: 追踪区间/纯持有 vs 模拟交易/动作统计/得分轨迹, 可展开采样明细 */
function HistoryCard({ h }: { h: TrackedHistory }) {
  const [expanded, setExpanded] = useState(false)
  const { data: pts = [] } = useQuery({
    queryKey: ['tracking', 'points', h.symbol],
    queryFn: () => api.trackingPoints(h.symbol),
    select: (d) => d.items,
    enabled: expanded,
  })
  const finalPnl = h.final_pnl
  const holdPnl = h.hold_pnl
  // 模拟 vs 持有: 旧归档数据可能缺 final_pnl(未结算), 显示 '-'
  const beats = finalPnl !== undefined && holdPnl !== null && holdPnl !== undefined
    ? finalPnl - holdPnl : null
  const ac = h.action_counts
  return (
    <Card className="mb-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <button type="button" onClick={() => setExpanded(!expanded)} className="cursor-pointer border-none bg-transparent text-left">
          <span className="text-[14px] font-semibold text-ink">{h.name || h.symbol} <span className="font-normal text-ink-faint">{h.symbol}</span></span>
        </button>
        <Tag color={REASON_COLOR[h.archive_reason ?? ''] ?? '#64748b'}>
          {REASON_LABEL[h.archive_reason ?? ''] ?? h.archive_reason ?? '已归档'}
        </Tag>
        {h.stage_at_track && (
          <Tag color={STAGE_COLOR[stageKeyOf(h.stage_at_track, h.stage_sub_at_track)] ?? '#64748b'}>
            追踪时:{STAGE_LABEL[stageKeyOf(h.stage_at_track, h.stage_sub_at_track)] ?? h.stage_at_track}
          </Tag>
        )}
        {h.final_stage && (
          <Tag color={STAGE_COLOR[stageKeyOf(h.final_stage, h.final_stage_sub)] ?? '#64748b'}>
            退出时:{STAGE_LABEL[stageKeyOf(h.final_stage, h.final_stage_sub)] ?? h.final_stage}
          </Tag>
        )}
        <span className="text-[11px] text-ink-faint">
          {h.first_time ? `${h.first_time.slice(5, 10)} → ${(h.last_time ?? '').slice(5, 10)}` : '-'}
          {h.days ? ` (${h.days}个交易日)` : ''}
        </span>
        {/* 成绩: 模拟交易 vs 纯持有 */}
        <span className="text-[12px] text-ink-muted">
          模拟 <b className={finalPnl !== undefined ? (finalPnl >= 0 ? 'text-rise' : 'text-fall') : 'text-ink'}>
            {finalPnl !== undefined ? `${finalPnl >= 0 ? '+' : ''}${finalPnl.toFixed(1)}%` : '-'}
          </b>
        </span>
        {holdPnl !== null && holdPnl !== undefined && (
          <span className={`text-[12px] ${holdPnl >= 0 ? 'text-rise' : 'text-fall'}`}>
            持有 {holdPnl >= 0 ? '+' : ''}{holdPnl.toFixed(1)}%
          </span>
        )}
        {beats !== null && (
          <span className="text-[11px] text-ink-faint">
            {beats >= 0 ? '跑赢持有' : '跑输持有'} {beats >= 0 ? '+' : ''}{beats.toFixed(1)}pct
          </span>
        )}
        {/* 动作统计 */}
        {ac && (ac.open + ac.add + ac.reduce + ac.close > 0) && (
          <span className="text-[11px] text-ink-faint">
            {ac.open > 0 && `建仓${ac.open} `}
            {ac.add > 0 && `加仓${ac.add} `}
            {ac.reduce > 0 && `减仓${ac.reduce} `}
            {ac.close > 0 && `平仓${ac.close}`}
          </span>
        )}
        <span className="ml-auto text-[11px] text-ink-faint">
          得分 {h.first_score?.toFixed(1) ?? '-'} → {h.last_score?.toFixed(1) ?? '-'}{h.max_score !== undefined && ` (峰值 ${h.max_score.toFixed(1)})`}
        </span>
      </div>
      {expanded && (
        <div className="mt-3 border-t border-divider pt-3">
          {pts.length === 0 ? (
            <div className="text-[12px] text-ink-faint">无采样记录(追踪期间未采样或采样点已被清理)。</div>
          ) : (
            <>
              <ScoreChart points={pts as ScorePoint[]} />
              <table className="mt-2 w-full border-collapse text-[11px]">
                <thead>
                  <tr className="border-b border-divider text-left text-ink-faint">
                    <th className="px-1 py-1 font-medium">采样时间</th>
                    <th className="px-1 py-1 font-medium">类型</th>
                    <th className="px-1 py-1 font-medium">总分</th>
                    <th className="px-1 py-1 font-medium">阶段</th>
                    <th className="px-1 py-1 font-medium">价格</th>
                    <th className="px-1 py-1 font-medium">信号</th>
                    <th className="px-1 py-1 font-medium">模拟</th>
                  </tr>
                </thead>
                <tbody>
                  {pts.map((p, i) => (
                    <tr key={i} className="border-b border-divider/60 last:border-b-0">
                      <td className="px-1 py-1">{p.time.slice(5, 16)}</td>
                      <td className="px-1 py-1 text-ink-faint">{KIND_LABEL[p.sample_kind] ?? p.sample_kind}</td>
                      <td className="px-1 py-1 font-semibold">{p.score.toFixed(1)}</td>
                      <td className="px-1 py-1"><span style={{ color: STAGE_COLOR[stageKeyOf(p.stage, p.stage_sub)] }}>{STAGE_LABEL[stageKeyOf(p.stage, p.stage_sub)] ?? (p.stage || '-')}</span></td>
                      <td className="px-1 py-1">{p.price.toFixed(2)}</td>
                      <td className="px-1 py-1">{p.signal_type || '-'}</td>
                      <td className="px-1 py-1">
                        <span className={p.sim_action === 'open' || p.sim_action === 'add' ? 'text-rise' : p.sim_action === 'close' || p.sim_action === 'reduce' ? 'text-fall' : 'text-ink-faint'}>
                          {SIM_ACTION_LABEL[p.sim_action] ?? (p.sim_qty > 0 ? '持有' : '-')}
                        </span>
                        {p.sim_pnl !== 0 && (
                          <span className={p.sim_pnl >= 0 ? 'text-rise' : 'text-fall'}>
                            {' '}{p.sim_pnl >= 0 ? '+' : ''}{p.sim_pnl.toFixed(1)}%
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </Card>
  )
}

export default function Tracking() {
  const queryClient = useQueryClient()
  const [sampling, setSampling] = useState(false)
  const [tab, setTab] = useState<'active' | 'history'>('active')
  const { data: items = [], isLoading } = useQuery({
    queryKey: ['tracking'],
    queryFn: api.trackingList,
    select: (d) => d.items,
    refetchInterval: 30_000, // 30s 轮询(采样任务在后台跑, 页面自动更新)
  })
  const { data: history = [] } = useQuery({
    queryKey: ['tracking', 'history'],
    queryFn: api.trackingHistory,
    select: (d) => d.items,
    enabled: tab === 'history', // 切到历史档才拉取
  })

  const sampleNow = async () => {
    setSampling(true)
    try {
      const r = await api.trackingSampleNow()
      toast.success(`采样完成: ${r.ok}/${r.total} 成功`)
      queryClient.invalidateQueries({ queryKey: ['tracking'] })
    } catch (e) {
      toast.error(String((e as Error).message))
    } finally {
      setSampling(false)
    }
  }

  return (
    <div>
      <PageHeader title="得分追踪" />
      <Card className="mb-3">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-ink-muted">
          <span>从「选股」结果点击追踪后, 每日自动采样 2 次得分(午间 12:30 / 盘后 16:00), 观察得分与股价走势关系, 验证动量筛选的可操作性。衰竭期自动结束追踪并结算, 过热期仅预警。</span>
          <Button onClick={sampleNow} disabled={sampling} className="ml-auto h-7 px-2 text-xs">
            {sampling ? '采集中...' : '立即采样'}
          </Button>
        </div>
      </Card>

      {/* 追踪中 / 历史档 Tab */}
      <div className="mb-2 flex items-center gap-1.5">
        {([
          { value: 'active', label: `追踪中(${items.length})` },
          { value: 'history', label: `历史档(${history.length})` },
        ] as const).map((t) => (
          <button
            key={t.value}
            type="button"
            onClick={() => setTab(t.value)}
            className={`rounded-full border px-3 py-1 text-[12px] transition-colors ${
              tab === t.value
                ? 'border-[#dc2626] bg-[#dc2626] text-white'
                : 'border-[#d0d3d9] hover:border-[#a0a5ad]'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'active' ? (
        isLoading ? <Loading /> : items.length === 0 ? (
          <Card><EmptyState>暂无追踪股票。到「选股」页对结果点「追踪」即可加入。</EmptyState></Card>
        ) : (
          items.map((st) => <StockCard key={st.symbol} st={st} />)
        )
      ) : (
        history.length === 0 ? (
          <Card><EmptyState>历史档为空。追踪结束(衰竭自动退出 / 手动停止 / 30 天期满)后在此查看本次效果。</EmptyState></Card>
        ) : (
          history.map((h) => <HistoryCard key={`${h.symbol}-${h.archived_at}`} h={h} />)
        )
      )}
    </div>
  )
}
