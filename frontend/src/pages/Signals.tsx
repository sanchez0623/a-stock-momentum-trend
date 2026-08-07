import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PositionItem, Signal, SignalRecord } from '../api/client'
import { Button, Card, ErrorBox, Field, Loading, Tag, inputStyle } from '../components/ui'
import { SIGNAL_META } from '../components/ui'
import { fmtPct } from '../const/colors'
import SymbolInput from '../components/SymbolInput'

// 统一分析结果: 单个评估(1 条)与批量分析(N 条)共用同一渲染
interface ResultItem {
  symbol: string
  name: string
  price: number
  signal: Signal | null
  error?: string
}

export default function Signals() {
  const [records, setRecords] = useState<SignalRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false) // 单个评估 / 批量分析 共用
  const [results, setResults] = useState<ResultItem[] | null>(null)
  const [positions, setPositions] = useState<PositionItem[]>([])

  const refresh = () => api.signals(undefined, 30).then(setRecords).catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    api.positions().then((p) => setPositions(p.positions)).catch(() => {})
  }, [])

  // 单个评估(输入代码 / 持仓下拉选中都走这里)
  const evaluate = async (sym?: string) => {
    const target = (sym ?? symbol).trim()
    if (!target || busy) return
    setBusy(true)
    setError('')
    try {
      const r = await api.evaluateSignal(target)
      setResults([{
        symbol: target,
        name: r.signal?.name || name,
        price: r.signal?.price || 0,
        signal: r.signal,
      }])
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  // 从持仓选择: 填充代码并自动评估
  const analyzePosition = async (sym: string) => {
    setSymbol(sym)
    const p = positions.find((x) => x.symbol === sym)
    if (p?.name) setName(p.name)
    await evaluate(sym)
  }

  // 批量分析全部持仓
  const analyzeAll = async () => {
    if (positions.length === 0 || busy) return
    setBusy(true)
    setError('')
    try {
      setResults(await api.evaluateBatch(positions.map((p) => p.symbol)))
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>信号中心</h1>
      {error && <ErrorBox message={error} />}

      {/* 操作区: 只负责输入与触发, 不展示结果 */}
      <Card title="信号分析(输入代码或选持仓, 生成后可到「交易计划」)">
        {positions.length > 0 && (
          <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <Field label="从持仓选择">
                <select
                  style={{ ...inputStyle, width: '100%' }}
                  value=""
                  onChange={(e) => { if (e.target.value) analyzePosition(e.target.value) }}
                >
                  <option value="">-- 选择持仓 --</option>
                  {positions.map((p) => (
                    <option key={p.symbol} value={p.symbol}>
                      {p.symbol} {p.name || ''} · {p.qty} 股 · 成本 {p.cost.toFixed(2)} · {fmtPct(p.unrealized_pct)}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Button kind="ghost" onClick={analyzeAll} disabled={busy}>
              {busy ? '分析中...' : `一键分析全部持仓(${positions.length})`}
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
          <Button onClick={() => evaluate()} disabled={busy || !symbol.trim()}>{busy ? '分析中...' : '评估'}</Button>
        </div>
      </Card>

      {/* 结果区: 单个与批量统一列表 */}
      <Card title={`分析结果${results ? `(${results.length})` : ''}`} style={{ marginTop: 12 }}>
        {!results ? (
          <div style={{ color: '#999', fontSize: 13 }}>输入代码点「评估」, 或从持仓选择/一键分析。无信号时显示"无信号"(超买不追、趋势破坏属正常)。</div>
        ) : results.length === 0 ? (
          <div style={{ color: '#999', fontSize: 13 }}>无结果。</div>
        ) : (
          results.map((r) => <ResultRow key={r.symbol} r={r} />)
        )}
      </Card>

      <Card title={`信号记录(${records.length})`} style={{ marginTop: 12 }}>
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

// 统一结果行: 单个评估和批量分析共用
function ResultRow({ r }: { r: ResultItem }) {
  const sig = r.signal
  const meta = sig ? SIGNAL_META[sig.type] ?? { label: sig.type, color: '#64748b' } : null
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
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
}
