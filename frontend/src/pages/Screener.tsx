import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ScreenerTask } from '../api/client'
import { Button, Card, ErrorBox, Loading, Tag, toast } from '../components/ui'

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
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>选股</h1>
      {error && <ErrorBox message={error} />}

      <Card title="全市场扫描(三因子: 趋势40 + 动量40 + 量能20)">
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
            市场
            <select value={market} onChange={(e) => setMarket(e.target.value)} style={{ padding: '7px 10px', border: '1px solid #d0d3d9', borderRadius: 6, fontSize: 13 }}>
              <option value="all">全部A股</option>
              <option value="sh">沪市</option>
              <option value="sz">深市</option>
              <option value="bj">北交所</option>
            </select>
          </label>
          <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
            板块
            <select value={board} onChange={(e) => setBoard(e.target.value)} style={{ padding: '7px 10px', border: '1px solid #d0d3d9', borderRadius: 6, fontSize: 13 }}>
              <option value="">不限</option>
              <option value="main">主板</option>
              <option value="chinext">创业板</option>
              <option value="star">科创板</option>
              <option value="bj">北交所</option>
            </select>
          </label>
          <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
            申万行业
            <input
              value={industry}
              onChange={(e) => setIndustry(e.target.value)}
              placeholder="如 半导体"
              style={{ padding: '7px 10px', border: '1px solid #d0d3d9', borderRadius: 6, fontSize: 13, width: 110 }}
            />
          </label>
          <Button onClick={run} disabled={scanning}>
            {scanning ? '扫描中...' : '开始扫描'}
          </Button>
          {task && <span style={{ fontSize: 12, color: '#888' }}>最近任务: {task.status} · {task.done}/{task.total} · {task.progress}%</span>}
        </div>
        <div style={{ fontSize: 11, color: '#999', marginTop: 8 }}>
          市场/板块/行业可组合缩小范围(如 科创板+半导体). 行业需本地股票列表含行业数据(东财列表成功拉取一次后自动填充).
        </div>
        {task?.error && <div style={{ color: '#ea580c', fontSize: 12, marginTop: 8 }}>任务异常: {task.error}</div>}
      </Card>

      {task && task.status === 'done' && (
        <Card title={`排名 Top ${task.result.length}(日成交额 ≥ 5000万)`}>
          {task.result.length === 0 ? (
            <div style={{ color: '#999', fontSize: 13 }}>
              无结果。当前股票列表可能降级为自选池(东财列表接口风控期), 或均未达评分/流动性门槛。
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: '#888', textAlign: 'left' }}>
                  <th style={{ padding: '8px 6px' }}>#</th>
                  <th style={{ padding: '8px 6px' }}>代码</th>
                  <th style={{ padding: '8px 6px' }}>名称</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>总分</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>趋势</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>动量</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>量能</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>现价</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>ADX</th>
                  <th style={{ padding: '8px 6px', textAlign: 'right' }}>量比</th>
                  <th style={{ padding: '8px 6px' }}>关注度</th>
                </tr>
              </thead>
              <tbody>
                {task.result.map((r, i) => (
                  <tr key={r.symbol} style={{ borderTop: '1px solid #f0f1f3' }}>
                    <td style={{ padding: '8px 6px', color: '#bbb' }}>{i + 1}</td>
                    <td style={{ padding: '8px 6px', fontWeight: 600 }}>{r.symbol}</td>
                    <td style={{ padding: '8px 6px' }}>{r.name || '-'}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right', fontWeight: 700, color: r.total >= 60 ? '#dc2626' : '#333' }}>{r.total.toFixed(1)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.trend_score.toFixed(1)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.momentum_score.toFixed(1)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.volume_score.toFixed(1)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.close.toFixed(2)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.adx.toFixed(1)}</td>
                    <td style={{ padding: '8px 6px', textAlign: 'right' }}>{r.volume_ratio.toFixed(2)}</td>
                    <td style={{ padding: '8px 6px' }}>
                      <Tag color={r.attention === '强烈关注' ? '#dc2626' : r.attention === '重点观察' ? '#ea580c' : '#64748b'}>{r.attention}</Tag>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </div>
  )
}
