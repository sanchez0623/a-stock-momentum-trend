import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ScorePoint, TrackedStock } from '../api/client'
import { Button, Card, ConfirmDialog, EmptyState, Loading, PageHeader, Tag, toast } from '../components/ui'
import { ScoreChart } from '../components/charts/ScoreChart'

const STAGE_LABEL: Record<string, string> = {
  launch: '启动期', accelerate: '加速期', overheat: '过热期', exhaust: '衰竭期',
}
const STAGE_COLOR: Record<string, string> = {
  launch: '#dc2626', accelerate: '#dc2626', overheat: '#ea580c', exhaust: '#16a34a',
}
const KIND_LABEL: Record<string, string> = { pre: '盘前', noon: '午间', after: '盘后', manual: '手动' }

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
          <Tag color={STAGE_COLOR[st.stage_at_track] ?? '#64748b'}>追踪时:{STAGE_LABEL[st.stage_at_track] ?? st.stage_at_track}</Tag>
        )}
        {st.latest?.stage && (
          <Tag color={STAGE_COLOR[st.latest.stage] ?? '#64748b'}>最新:{STAGE_LABEL[st.latest.stage] ?? st.latest.stage}</Tag>
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
                  <td className="px-1 py-1"><span style={{ color: STAGE_COLOR[p.stage] }}>{STAGE_LABEL[p.stage] ?? (p.stage || '-')}</span></td>
                  <td className="px-1 py-1">{p.price.toFixed(2)}</td>
                  <td className="px-1 py-1">{p.volume_ratio.toFixed(2)}</td>
                  <td className="px-1 py-1">{p.signal_type || '-'}</td>
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

export default function Tracking() {
  const queryClient = useQueryClient()
  const [sampling, setSampling] = useState(false)
  const { data: items = [], isLoading } = useQuery({
    queryKey: ['tracking'],
    queryFn: api.trackingList,
    select: (d) => d.items,
    refetchInterval: 30_000, // 30s 轮询(采样任务在后台跑, 页面自动更新)
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
          <span>从「选股」结果点击追踪后, 每日自动采样 2 次得分(午间 12:30 / 盘后 16:00), 观察得分与股价走势关系, 验证动量筛选的可操作性。</span>
          <Button onClick={sampleNow} disabled={sampling} className="ml-auto h-7 px-2 text-xs">
            {sampling ? '采集中...' : '立即采样'}
          </Button>
        </div>
      </Card>

      {isLoading ? <Loading /> : items.length === 0 ? (
        <Card><EmptyState>暂无追踪股票。到「选股」页对结果点「追踪」即可加入。</EmptyState></Card>
      ) : (
        items.map((st) => <StockCard key={st.symbol} st={st} />)
      )}
    </div>
  )
}
