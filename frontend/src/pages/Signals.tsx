import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PositionItem, Signal, SignalRecord } from '../api/client'
import { Button, Card, ErrorBox, Field, Loading, Tag, inputStyle } from '../components/ui'
import { SIGNAL_META } from '../components/ui'
import { fmtPct } from '../const/colors'
import SymbolInput from '../components/SymbolInput'

export default function Signals() {
  const [records, setRecords] = useState<SignalRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [evaluating, setEvaluating] = useState(false)
  const [evalResult, setEvalResult] = useState<Signal | null>(null)
  const [positions, setPositions] = useState<PositionItem[]>([])

  const refresh = () => api.signals(undefined, 30).then(setRecords).catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    api.positions().then((p) => setPositions(p.positions)).catch(() => {})
  }, [])

  const evaluate = async (target?: string) => {
    const sym = (target ?? symbol).trim()
    if (!sym) return
    setEvaluating(true)
    setError('')
    try {
      const r = await api.evaluateSignal(sym)
      setEvalResult(r.signal)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setEvaluating(false)
    }
  }

  // 从持仓选择: 填充代码并自动评估(信号引擎结合持仓成本判断加仓/减仓/止损)
  const analyzePosition = async (sym: string) => {
    setSymbol(sym)
    const p = positions.find((x) => x.symbol === sym)
    if (p?.name) setName(p.name)
    await evaluate(sym)
  }

  // 批量分析全部持仓
  const [batchResults, setBatchResults] = useState<Array<{ symbol: string; name: string; price: number; signal: Signal | null; error?: string }> | null>(null)
  const [analyzing, setAnalyzing] = useState(false)

  const analyzeAll = async () => {
    if (positions.length === 0 || analyzing) return
    setAnalyzing(true)
    setError('')
    try {
      const results = await api.evaluateBatch(positions.map((p) => p.symbol))
      setBatchResults(results)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setAnalyzing(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>信号中心</h1>
      {error && <ErrorBox message={error} />}

      <Card title="手动评估(生成信号后可到「交易计划」生成计划)">
        {positions.length > 0 && (
          <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <Field label="从持仓选择(自动评估, 结合持仓成本判断加仓/减仓/止损)">
                <select
                  style={{ ...inputStyle, width: '100%' }}
                  value=""
                  onChange={(e) => { if (e.target.value) analyzePosition(e.target.value) }}
                >
                  <option value="">-- 选择持仓分析 --</option>
                  {positions.map((p) => (
                    <option key={p.symbol} value={p.symbol}>
                      {p.symbol} {p.name || ''} · {p.qty} 股 · 成本 {p.cost.toFixed(2)} · {fmtPct(p.unrealized_pct)}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Button kind="ghost" onClick={analyzeAll} disabled={analyzing}>
              {analyzing ? '分析中...' : `一键分析全部持仓(${positions.length})`}
            </Button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <div style={{ flex: 1 }}>
            <Field label="股票代码">
              <SymbolInput value={symbol} onChange={setSymbol} onNameFound={setName} placeholder="如 300750" />
            </Field>
          </div>
          {name && <div style={{ color: '#666', fontSize: 13, paddingBottom: 10 }}>{name}</div>}
          <Button onClick={() => evaluate()} disabled={evaluating || !symbol.trim()}>{evaluating ? '评估中...' : '评估'}</Button>
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

      {batchResults && (
        <Card title="持仓批量分析结果" style={{ marginTop: 12 }}>
          {batchResults.length === 0 ? (
            <div style={{ color: '#999', fontSize: 13 }}>无结果。</div>
          ) : (
            batchResults.map((r) => {
              const sig = r.signal
              const meta = sig ? SIGNAL_META[sig.type] ?? { label: sig.type, color: '#64748b' } : null
              return (
                <div key={r.symbol} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                  <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                    <span style={{ fontWeight: 600 }}>{r.symbol}</span>
                    <span style={{ color: '#888' }}>{r.name || ''}</span>
                    {r.price > 0 && <span style={{ color: '#bbb' }}>@{r.price.toFixed(2)}</span>}
                  </span>
                  {r.error ? (
                    <span style={{ color: '#dc2626', fontSize: 12 }}>分析失败: {r.error}</span>
                  ) : sig ? (
                    <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <Tag color={meta!.color}>{meta!.label}</Tag>
                      <b style={{ color: sig.strength >= 70 ? '#dc2626' : '#666' }}>{sig.strength.toFixed(0)}</b>
                      <span style={{ color: '#666', maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sig.reason}</span>
                    </span>
                  ) : (
                    <span style={{ color: '#999', fontSize: 12 }}>无信号(不满足当前条件)</span>
                  )}
                </div>
              )
            })
          )}
        </Card>
      )}

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
