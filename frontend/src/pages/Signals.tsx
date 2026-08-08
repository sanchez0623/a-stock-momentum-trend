import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { PositionItem, Signal, SignalRecord } from '../api/client'
import { Button, Card, ErrorBox, EmptyState, FormRow, ListRow, Loading, Tag, inputStyle, toast } from '../components/ui'
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
  const navigate = useNavigate()
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

  // 单个/多个代码评估(逗号或空格分隔), 统一走批量接口
  const evaluate = async (codesArg?: string[]) => {
    const codes = codesArg ?? symbol.split(/[,，\s]+/).filter(Boolean)
    if (codes.length === 0 || busy) return
    setBusy(true)
    setError('')
    try {
      setResults(await api.evaluateBatch(codes))
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  // 从持仓选择: 填充代码并自动评估
  const analyzePosition = async (sym: string) => {
    setSymbol(sym)
    const p = positions.find((x) => x.symbol === sym)
    if (p?.name) setName(p.name)
    await evaluate([sym])
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
      toast.error(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  // 有信号 -> 生成交易计划并跳转「交易计划」页(打通流程)
  const generatePlan = async (symbol: string, nm: string) => {
    setBusy(true)
    setError('')
    try {
      const plan = await api.generatePlan(symbol, nm)
      if (!plan) {
        // 后端返回 data:null => 当前无信号/无行情, 属预期, 弹信息提示, 不跳转
        setBusy(false)
        toast.info('当前无信号, 暂不生成计划')
        return
      }
      toast.success(`已生成 ${symbol} 的交易计划, 已跳转`)
      navigate('/plans')
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
      setBusy(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">信号中心</h1>
      {error && <ErrorBox message={error} />}

      {/* 操作区: 两条输入通道(持仓快捷入口 / 代码手动输入), 共用 FormRow 对齐 */}
      <Card title="信号分析">
        {positions.length > 0 && (
          <>
            <FormRow
              label="从持仓选择"
              hint="选中后自动填入代码并分析"
              action={
                <Button kind="ghost" className="h-9 shrink-0" onClick={analyzeAll} disabled={busy}>
                  {busy ? '分析中...' : `一键分析全部持仓(${positions.length})`}
                </Button>
              }
            >
              <select
                value=""
                onChange={(e) => { if (e.target.value) analyzePosition(e.target.value) }}
                className="h-9 w-full rounded border border-[#d9d9d9] bg-white px-2.5 text-[13px] outline-none transition-colors"
              >
                <option value="">-- 选择持仓 --</option>
                {positions.map((p) => (
                  <option key={p.symbol} value={p.symbol}>
                    {p.symbol} {p.name || ''} · {p.qty} 股 · {fmtPct(p.unrealized_pct)}
                  </option>
                ))}
              </select>
            </FormRow>
            <div className="my-3 border-t border-divider" />
          </>
        )}

        <FormRow
          label="股票代码"
          hint={
            <>
              多个用逗号或空格分隔, 上限 50
              {name && <span className="ml-2 font-medium text-fall">{name}</span>}
            </>
          }
          action={
            <Button className="h-9 w-20 shrink-0" onClick={() => evaluate()} disabled={busy || !symbol.trim()}>
              {busy ? '分析中...' : '评估'}
            </Button>
          }
        >
          <SymbolInput
            value={symbol}
            onChange={setSymbol}
            onNameFound={setName}
            onEnter={() => evaluate()}
            placeholder="如 300139,688079,688146"
            style={{ ...inputStyle, height: 36 }}
          />
        </FormRow>
      </Card>

      {/* 结果区: 单个与批量统一列表 */}
      <Card title={`分析结果${results ? `(${results.length})` : ''}`} className="mt-3">
        {!results ? (
          <EmptyState>输入一个或多个代码(逗号/空格分隔)点「评估」, 或从持仓选择/一键分析。有信号时可点该行「生成计划」直达交易计划。</EmptyState>
        ) : results.length === 0 ? (
          <EmptyState>无结果。</EmptyState>
        ) : (
          results.map((r) => <ResultRow key={r.symbol} r={r} onGeneratePlan={generatePlan} busy={busy} />)
        )}
      </Card>

      <Card title={`信号记录(${records.length})`} className="mt-3">
        {records.length === 0 ? (
          <EmptyState>暂无历史信号记录。</EmptyState>
        ) : (
          records.map((s) => {
            const meta = SIGNAL_META[s.type] ?? { label: s.type, color: '#64748b' }
            return (
              <ListRow key={s.id} className="py-2.5">
                <span className="flex items-center gap-2.5">
                  <Tag color={meta.color}>{meta.label}</Tag>
                  <span className="font-semibold">{s.symbol}</span>
                  <span className="text-ink-muted">{s.name}</span>
                </span>
                <span className="flex items-center gap-3">
                  <span className="max-w-[380px] truncate text-ink-secondary">{s.reason}</span>
                  <b className={s.strength >= 70 ? 'text-rise' : 'text-ink-secondary'}>{s.strength.toFixed(0)}</b>
                  <span className="text-xs text-ink-faint">{s.time.slice(5, 16)}</span>
                </span>
              </ListRow>
            )
          })
        )}
      </Card>
    </div>
  )
}

// 统一结果行: 单个评估和批量分析共用
// 布局: 主行(股票/信号/强度/操作) + 副行(完整理由, 引用块样式, 不截断)
function ResultRow({ r, onGeneratePlan, busy }: { r: ResultItem; onGeneratePlan: (symbol: string, name: string) => void; busy: boolean }) {
  const sig = r.signal
  const meta = sig ? SIGNAL_META[sig.type] ?? { label: sig.type, color: '#64748b' } : null
  return (
    <div className="border-b border-divider py-2.5 last:border-b-0">
      {/* 主行: 一眼可扫的决策信息 */}
      <div className="flex items-center justify-between gap-2 text-[13px]">
        <span className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-semibold">{r.symbol}</span>
          <span className="truncate text-ink-muted">{r.name || ''}</span>
          {r.price > 0 && <span className="shrink-0 text-ink-faint">@{r.price.toFixed(2)}</span>}
        </span>
        {r.error ? (
          <span className="shrink-0 text-xs text-rise">分析失败</span>
        ) : sig ? (
          <span className="flex shrink-0 items-center gap-2">
            <Tag color={meta!.color}>{meta!.label}</Tag>
            <b className={sig.strength >= 70 ? 'text-rise' : 'text-ink-secondary'}>{sig.strength.toFixed(0)}</b>
            <Button kind="primary" onClick={() => onGeneratePlan(r.symbol, r.name)} disabled={busy} className="h-7 px-3 text-xs">
              {busy ? '生成中...' : '生成计划'}
            </Button>
          </span>
        ) : (
          <span className="shrink-0 text-xs text-ink-faint">无信号(不满足当前条件)</span>
        )}
      </div>
      {/* 副行: 完整理由/错误详情, 引用块样式, 永不截断 */}
      {r.error ? (
        <p className="mt-1.5 border-l-2 border-rise/40 pl-2.5 text-xs leading-relaxed text-rise">{r.error}</p>
      ) : sig?.reason ? (
        <p className="mt-1.5 border-l-2 border-divider pl-2.5 text-xs leading-relaxed text-ink-secondary">{sig.reason}</p>
      ) : null}
    </div>
  )
}
