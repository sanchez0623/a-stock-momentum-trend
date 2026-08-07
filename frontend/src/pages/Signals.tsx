import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Signal, SignalRecord } from '../api/client'
import { Button, Card, ErrorBox, Field, Loading, Tag } from '../components/ui'
import { SIGNAL_META } from '../components/ui'
import SymbolInput from '../components/SymbolInput'

export default function Signals() {
  const [records, setRecords] = useState<SignalRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [evaluating, setEvaluating] = useState(false)
  const [evalResult, setEvalResult] = useState<Signal | null>(null)

  const refresh = () => api.signals(undefined, 30).then(setRecords).catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [])

  const evaluate = async () => {
    if (!symbol.trim()) return
    setEvaluating(true)
    setError('')
    try {
      const r = await api.evaluateSignal(symbol.trim())
      setEvalResult(r.signal)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setEvaluating(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>信号中心</h1>
      {error && <ErrorBox message={error} />}

      <Card title="手动评估(生成信号后可到「交易计划」生成计划)">
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <div style={{ flex: 1 }}>
            <Field label="股票代码">
              <SymbolInput value={symbol} onChange={setSymbol} onNameFound={setName} placeholder="如 300750" />
            </Field>
          </div>
          {name && <div style={{ color: '#666', fontSize: 13, paddingBottom: 10 }}>{name}</div>}
          <Button onClick={evaluate} disabled={evaluating || !symbol.trim()}>{evaluating ? '评估中...' : '评估'}</Button>
        </div>
        {evalResult && (
          <div style={{ marginTop: 12, padding: 12, background: '#f8fafc', borderRadius: 8, fontSize: 13 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
              <Tag color={SIGNAL_META[evalResult.type]?.color ?? '#64748b'}>{SIGNAL_META[evalResult.type]?.label ?? evalResult.type}</Tag>
              <b>强度 {evalResult.strength.toFixed(0)}</b>
              <span style={{ color: '#888' }}>{evalResult.symbol} @ {evalResult.price.toFixed(2)}</span>
            </div>
            <div>{evalResult.reason}</div>
          </div>
        )}
        {!evalResult && !evaluating && (
          <div style={{ color: '#999', fontSize: 12, marginTop: 8 }}>提示: 当前无满足条件的信号时返回"无信号", 属正常(如超买不追、趋势破坏)。</div>
        )}
      </Card>

      <Card title={`信号记录(${records.length})`}>
        {records.length === 0 ? (
          <div style={{ color: '#999', fontSize: 13 }}>暂无历史信号记录。</div>
        ) : (
          records.map((s) => {
            const meta = SIGNAL_META[s.type] ?? { label: s.type, color: '#64748b' }
            return (
              <div key={s.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <Tag color={meta.color}>{meta.label}</Tag>
                  <span style={{ fontWeight: 600 }}>{s.symbol}</span>
                  <span style={{ color: '#888' }}>{s.name}</span>
                </span>
                <span style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                  <span style={{ color: '#666', maxWidth: 380, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.reason}</span>
                  <b style={{ color: s.strength >= 70 ? '#dc2626' : '#666' }}>{s.strength.toFixed(0)}</b>
                  <span style={{ color: '#bbb', fontSize: 12 }}>{s.time.slice(5, 16)}</span>
                </span>
              </div>
            )
          })
        )}
      </Card>
    </div>
  )
}
