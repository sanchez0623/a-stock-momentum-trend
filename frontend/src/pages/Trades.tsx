import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, ErrorBox, EmptyState, Field, Loading, PageHeader, Table, Td, Th, inputStyle, toast } from '../components/ui'
import { cn } from '../components/ui'

export default function Trades() {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')

  // 筛选
  const [filterSymbol, setFilterSymbol] = useState('')
  const [filterAction, setFilterAction] = useState('')

  // 减仓/清仓表单
  const [opSymbol, setOpSymbol] = useState('')
  const [opPrice, setOpPrice] = useState('')
  const [opQty, setOpQty] = useState('')
  const [opMode, setOpMode] = useState<'reduce' | 'close'>('reduce')

  // 成交记录: queryKey 含筛选条件, 切换自动重新请求; 保留旧列表避免闪烁
  const { data: tradesData, isLoading, error: queryError } = useQuery({
    queryKey: ['trades', filterSymbol, filterAction],
    queryFn: () => api.trades({ symbol: filterSymbol || undefined, action: filterAction || undefined, limit: 200 }),
    placeholderData: (prev) => prev,
  })
  const trades = tradesData?.items ?? []
  const total = tradesData?.total ?? 0
  const { data: positions = [] } = useQuery({
    queryKey: ['positions'],
    queryFn: api.positions,
    select: (p) => p.positions,
  })
  const err = error || (queryError ? String((queryError as Error).message || queryError) : '')
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['trades'] })
    queryClient.invalidateQueries({ queryKey: ['positions'] })
  }

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

  if (isLoading) return <Loading />

  return (
    <div>
      <PageHeader title="交易日志" />
      {err && <ErrorBox message={err} />}

      <div className="mb-4 flex items-center gap-3">
        <input style={{ ...inputStyle, width: 120 }} value={filterSymbol} onChange={(e) => setFilterSymbol(e.target.value)} placeholder="代码筛选" />
        <select style={{ ...inputStyle, width: 112 }} value={filterAction} onChange={(e) => setFilterAction(e.target.value)}>
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
            <Table>
              <thead>
                <tr>
                  <Th>时间</Th>
                  <Th>代码</Th>
                  <Th>方向</Th>
                  <Th right>价格</Th>
                  <Th right>数量</Th>
                  <Th right>手续费</Th>
                  <Th right>盈亏(净)</Th>
                  <Th>原因</Th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-divider">
                    <Td>{t.time.slice(5, 16)}</Td>
                    <Td>{t.symbol} <span className="text-ink-faint">{t.name}</span></Td>
                    <Td>
                      <span className={cn('font-semibold', t.action === 'buy' ? 'text-rise' : 'text-fall')}>
                        {t.action === 'buy' ? '买入' : '卖出'}
                      </span>
                    </Td>
                    <Td right>{t.price.toFixed(2)}</Td>
                    <Td right>{t.qty}</Td>
                    <Td right className="text-ink-muted">{t.fee.toFixed(2)}</Td>
                    <Td right className={cn('font-semibold', colorByPct(t.pnl))}>
                      {t.action === 'sell' ? (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(0) : '-'}
                    </Td>
                    <Td className="text-ink-muted">{t.reason || '-'}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
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
