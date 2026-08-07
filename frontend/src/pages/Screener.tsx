import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ScreenerTask } from '../api/client'
import { Button, Card, EmptyState, ErrorBox, Loading, Tag, toast } from '../components/ui'
import { cn } from '../components/ui'

export default function Screener() {
  const [task, setTask] = useState<ScreenerTask | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [market, setMarket] = useState('all')
  const [board, setBoard] = useState('')
  const [industry, setIndustry] = useState('')
  const [running, setRunning] = useState(false)
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
    return stopPoll
  }, [])

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const { task_id } = await api.screenerRun(market, 30, board || undefined, industry.trim() || undefined)
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

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">选股</h1>
      {error && <ErrorBox message={error} />}

      <Card title="全市场扫描(三因子: 趋势40 + 动量40 + 量能20)">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex items-center gap-1.5 text-[13px]">
            市场
            <select value={market} onChange={(e) => setMarket(e.target.value)} className="rounded border border-[#d0d3d9] px-2.5 py-[7px] text-[13px]">
              <option value="all">全部A股</option>
              <option value="sh">沪市</option>
              <option value="sz">深市</option>
              <option value="bj">北交所</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[13px]">
            板块
            <select value={board} onChange={(e) => setBoard(e.target.value)} className="rounded border border-[#d0d3d9] px-2.5 py-[7px] text-[13px]">
              <option value="">不限</option>
              <option value="main">主板</option>
              <option value="chinext">创业板</option>
              <option value="star">科创板</option>
              <option value="bj">北交所</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[13px]">
            申万行业
            <input
              value={industry}
              onChange={(e) => setIndustry(e.target.value)}
              placeholder="如 半导体"
              className="w-[110px] rounded border border-[#d0d3d9] px-2.5 py-[7px] text-[13px]"
            />
          </label>
          <Button onClick={run} disabled={scanning}>
            {scanning ? '扫描中...' : '开始扫描'}
          </Button>
          {task && <span className="text-xs text-ink-muted">最近任务: {task.status} · {task.done}/{task.total} · {task.progress}%</span>}
        </div>
        <div className="mt-2 text-[11px] text-ink-faint">
          市场/板块/行业可组合缩小范围(如 科创板+半导体). 行业需本地股票列表含行业数据(东财列表成功拉取一次后自动填充).
        </div>
        {task?.error && <div className="mt-2 text-xs text-orange-500">任务异常: {task.error}</div>}
      </Card>

      {task && task.status === 'done' && (
        <Card title={`排名 Top ${task.result.length}(日成交额 ≥ 5000万)`} className="mt-3">
          {task.result.length === 0 ? (
            <EmptyState>
              无结果。当前股票列表可能降级为自选池(东财列表接口风控期), 或均未达评分/流动性门槛。
            </EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="text-left text-ink-muted">
                    <th className="px-1.5 py-2">#</th>
                    <th className="px-1.5 py-2">代码</th>
                    <th className="px-1.5 py-2">名称</th>
                    <th className="px-1.5 py-2 text-right">总分</th>
                    <th className="px-1.5 py-2 text-right">趋势</th>
                    <th className="px-1.5 py-2 text-right">动量</th>
                    <th className="px-1.5 py-2 text-right">量能</th>
                    <th className="px-1.5 py-2 text-right">现价</th>
                    <th className="px-1.5 py-2 text-right">ADX</th>
                    <th className="px-1.5 py-2 text-right">量比</th>
                    <th className="px-1.5 py-2">关注度</th>
                  </tr>
                </thead>
                <tbody>
                  {task.result.map((r, i) => (
                    <tr key={r.symbol} className="border-t border-divider">
                      <td className="px-1.5 py-2 text-ink-faint">{i + 1}</td>
                      <td className="px-1.5 py-2 font-semibold">{r.symbol}</td>
                      <td className="px-1.5 py-2">{r.name || '-'}</td>
                      <td className={cn('px-1.5 py-2 text-right font-bold', r.total >= 60 ? 'text-rise' : 'text-ink')}>{r.total.toFixed(1)}</td>
                      <td className="px-1.5 py-2 text-right">{r.trend_score.toFixed(1)}</td>
                      <td className="px-1.5 py-2 text-right">{r.momentum_score.toFixed(1)}</td>
                      <td className="px-1.5 py-2 text-right">{r.volume_score.toFixed(1)}</td>
                      <td className="px-1.5 py-2 text-right">{r.close.toFixed(2)}</td>
                      <td className="px-1.5 py-2 text-right">{r.adx.toFixed(1)}</td>
                      <td className="px-1.5 py-2 text-right">{r.volume_ratio.toFixed(2)}</td>
                      <td className="px-1.5 py-2">
                        <Tag color={r.attention === '强烈关注' ? '#dc2626' : r.attention === '重点观察' ? '#ea580c' : '#64748b'}>{r.attention}</Tag>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  )
}
