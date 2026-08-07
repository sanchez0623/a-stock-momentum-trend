import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PlanRecord } from '../api/client'
import { Button, Card, ErrorBox, EmptyState, Field, Loading, toast } from '../components/ui'
import SymbolInput from '../components/SymbolInput'

export default function Plans() {
  const [plans, setPlans] = useState<PlanRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [generating, setGenerating] = useState(false)

  const refresh = () => api.currentPlans().then(setPlans).catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [])

  const generate = async () => {
    if (!symbol.trim()) return
    setGenerating(true)
    setError('')
    try {
      await api.generatePlan(symbol.trim(), name)
      setSymbol('')
      setName('')
      toast.success(`已生成 ${symbol.trim()} 的交易计划`)
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    } finally {
      setGenerating(false)
    }
  }

  const mark = async (id: number, status: 'done' | 'ignored') => {
    try {
      await api.planStatus(id, status)
      toast.success(status === 'done' ? '已标记执行' : '已标记忽略')
      refresh()
    } catch (e) {
      toast.error(String((e as Error).message))
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">交易计划</h1>
      {error && <ErrorBox message={error} />}

      <Card title="生成计划(需先有信号, 持仓票评估效果更佳)">
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Field label="股票代码">
              <SymbolInput value={symbol} onChange={setSymbol} onNameFound={setName} placeholder="如 600519(已持仓)" />
            </Field>
          </div>
          {name && <div className="pb-2.5 text-[13px] text-ink-secondary">{name}</div>}
          <Button onClick={generate} disabled={generating || !symbol.trim()}>{generating ? '生成中...' : '生成计划'}</Button>
        </div>
      </Card>

      {plans.length === 0 ? (
        <Card><EmptyState>暂无待执行计划。先到「信号中心」评估, 或直接在持仓票上生成。</EmptyState></Card>
      ) : (
        plans.map((p) => (
          <Card key={p.id} className="mb-3" title={`计划 #${p.id} · ${p.symbol} ${p.name || ''} · ${p.time}`}>
            <pre className="m-0 text-[13px] leading-[1.7]" style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{p.content}</pre>
            <div className="mt-3 flex gap-2">
              <Button kind="primary" onClick={() => mark(p.id, 'done')}>标记已执行</Button>
              <Button kind="ghost" onClick={() => mark(p.id, 'ignored')}>标记已忽略</Button>
            </div>
          </Card>
        ))
      )}
    </div>
  )
}
