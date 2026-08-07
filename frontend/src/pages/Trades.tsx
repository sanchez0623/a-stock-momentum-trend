import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PositionItem, TradeRecord } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, ErrorBox, EmptyState, Field, Loading, inputStyle, toast } from '../components/ui'
import { cn } from '../components/ui'

export default function Trades() {
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [total, setTotal] = useState(0)
  const [positions, setPositions] = useState<PositionItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 筛选
  const [filterSymbol, setFilterSymbol] = useState('')
  const [filterAction, setFilterAction] = useState('')

  // 减仓/清仓表单
  const [opSymbol, setOpSymbol] = useState('')
  const [opPrice, setOpPrice] = useState('')
  const [opQty, setOpQty] = useState('')
  const [opMode, setOpMode] = useState<'reduce' | 'close'>('reduce')

  const refresh = (): Promise<void> => {
    const p1 = api
      .trades({ symbol: filterSymbol || undefined, action: filterAction || undefined, limit: 200 })
      .then((d) => { setTrades(d.items); setTotal(d.total) })
      .catch((e) => setError(String(e.message || e)))
    const p2 = api.positions().then((p) => setPositions(p.positions)).catch(() => {})
    return Promise.all([p1, p2]).then(() => undefined)
  }

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [filterSymbol, filterAction])

  const doOperation = async () => {
    if (!opSymbol.trim() || !opPrice) return
    try {
      if (opMode === 'close') {
        await api.closePosition(opSymbol.trim(), Number(opPrice))
        toast.success(`已清仓 ${opSymbol.trim()}`)
      } else {
        await api.reducePosition(opSymbol.trim(), Number(opQty || 0), Number(opPrice))
        toast.success(`已减仓 ${opSymbol.trim()} ${opQty || 0} 股`)
      }
      setOpSymbol(''); setOpPrice(''); setOpQty('')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">交易日志</h1>
      {error && <ErrorBox message={error} />}

      <div className="mb-4 flex items-center gap-3">
        <input style={{ ...inputStyle, width: 120 }} value={filterSymbol} onChange={(e) => setFilterSymbol(e.target.value)} placeholder="代码筛选" />
        <select style={inputStyle} value={filterAction} onChange={(e) => setFilterAction(e.target.value)} className="w-28">
          <option value="">全部方向</option>
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <span className="text-[13px] text-ink-muted">共 {total} 条</span>
        <Button kind="ghost" onClick={() => api.exportTrades()}>导出 CSV</Button>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[2fr_1fr]">
        {/* 左: 日志列表 */}
        <Card title="成交记录">
          {trades.length === 0 ? (
            <EmptyState>暂无成交。到「自选与持仓」页录入持仓后,卖出会在此留痕。</EmptyState>
          ) : (
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-ink-muted">
                  <th className="px-2 py-1.5 font-medium">时间</th>
                  <th className="px-2 py-1.5 font-medium">代码</th>
                  <th className="px-2 py-1.5 font-medium">方向</th>
                  <th className="px-2 py-1.5 text-right font-medium">价格</th>
                  <th className="px-2 py-1.5 text-right font-medium">数量</th>
                  <th className="px-2 py-1.5 text-right font-medium">手续费</th>
                  <th className="px-2 py-1.5 text-right font-medium">盈亏(净)</th>
                  <th className="px-2 py-1.5 font-medium">原因</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-divider">
                    <td className="px-2 py-[7px]">{t.time.slice(5, 16)}</td>
                    <td className="px-2 py-[7px]">{t.symbol} <span className="text-ink-faint">{t.name}</span></td>
                    <td className="px-2 py-[7px]">
                      <span className={cn('font-semibold', t.action === 'buy' ? 'text-rise' : 'text-fall')}>
                        {t.action === 'buy' ? '买入' : '卖出'}
                      </span>
                    </td>
                    <td className="px-2 py-[7px] text-right">{t.price.toFixed(2)}</td>
                    <td className="px-2 py-[7px] text-right">{t.qty}</td>
                    <td className="px-2 py-[7px] text-right text-ink-muted">{t.fee.toFixed(2)}</td>
                    <td className={cn('px-2 py-[7px] text-right font-semibold', colorByPct(t.pnl))}>
                      {t.action === 'sell' ? (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(0) : '-'}
                    </td>
                    <td className="px-2 py-[7px] text-ink-muted">{t.reason || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {/* 右: 减仓/清仓 + 持仓提示 */}
        <div className="flex flex-col gap-4">
          <Card title="减仓 / 清仓">
            <Field label="持仓股票">
              <select style={inputStyle} value={opSymbol} onChange={(e) => { setOpSymbol(e.target.value); const p = positions.find((x) => x.symbol === e.target.value); if (p) setOpPrice(String(p.price)) }}>
                <option value="">选择持仓</option>
                {positions.map((p) => (
                  <option key={p.symbol} value={p.symbol}>
                    {p.symbol} {p.name} · {p.qty} 股 · {fmtPct(p.unrealized_pct)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="成交价">
              <input style={inputStyle} type="number" value={opPrice} onChange={(e) => setOpPrice(e.target.value)} />
            </Field>
            {opMode === 'reduce' && (
              <Field label="减仓数量">
                <input style={inputStyle} type="number" value={opQty} onChange={(e) => setOpQty(e.target.value)} placeholder="留 0 表示减到清仓" />
              </Field>
            )}
            <div className="mt-1 flex gap-2">
              <Button kind="ghost" onClick={() => setOpMode('reduce')} style={{ flex: 1, ...(opMode === 'reduce' ? activeBtn : {}) }}>减仓</Button>
              <Button kind="danger" onClick={() => setOpMode('close')} style={{ flex: 1, ...(opMode === 'close' ? activeBtn : {}) }}>清仓</Button>
            </div>
            <div className="mt-2.5">
              <Button onClick={doOperation} disabled={!opSymbol.trim() || !opPrice}>
                {opMode === 'close' ? '确认清仓' : '确认减仓'}
              </Button>
            </div>
          </Card>
          <div className="text-[11px] text-ink-faint">卖出会在交易日志留痕并计算已实现盈亏;清仓后持仓消失。CSV 导出含全量记录(Excel 可直接打开)。</div>
        </div>
      </div>
    </div>
  )
}

const activeBtn: React.CSSProperties = { outline: '2px solid #2563eb', outlineOffset: 1 }
