import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PlanRecord } from '../api/client'
import { Button, Card, ErrorBox, Field, Loading, inputStyle } from '../components/ui'

export default function Plans() {
  const [plans, setPlans] = useState<PlanRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
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
      await api.generatePlan(symbol.trim())
      setSymbol('')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setGenerating(false)
    }
  }

  const mark = async (id: number, status: 'done' | 'ignored') => {
    await api.planStatus(id, status)
    refresh()
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>交易计划</h1>
      {error && <ErrorBox message={error} />}

      <Card title="生成计划(需先有信号, 持仓票评估效果更佳)">
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <div style={{ flex: 1 }}>
            <Field label="股票代码">
              <input style={inputStyle} value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="如 600519(已持仓)" />
            </Field>
          </div>
          <Button onClick={generate} disabled={generating || !symbol.trim()}>{generating ? '生成中...' : '生成计划'}</Button>
        </div>
      </Card>

      {plans.length === 0 ? (
        <Card><div style={{ color: '#999', fontSize: 13 }}>暂无待执行计划。先到「信号中心」评估, 或直接在持仓票上生成。</div></Card>
      ) : (
        plans.map((p) => (
          <Card key={p.id} style={{ marginBottom: 12 }} title={`计划 #${p.id} · ${p.symbol} ${p.name || ''} · ${p.time}`}>
            <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 13, lineHeight: 1.7, margin: 0 }}>{p.content}</pre>
            <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
              <Button kind="primary" onClick={() => mark(p.id, 'done')}>标记已执行</Button>
              <Button kind="ghost" onClick={() => mark(p.id, 'ignored')}>标记已忽略</Button>
            </div>
          </Card>
        ))
      )}
    </div>
  )
}
