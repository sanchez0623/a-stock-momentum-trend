import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PositionItem, TradeRecord } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, ErrorBox, Field, Loading, inputStyle } from '../components/ui'

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
      } else {
        await api.reducePosition(opSymbol.trim(), Number(opQty || 0), Number(opPrice))
      }
      setOpSymbol(''); setOpPrice(''); setOpQty('')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>交易日志</h1>
      {error && <ErrorBox message={error} />}

      <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'center' }}>
        <input style={{ ...inputStyle, width: 120 }} value={filterSymbol} onChange={(e) => setFilterSymbol(e.target.value)} placeholder="代码筛选" />
        <select style={inputStyle} value={filterAction} onChange={(e) => setFilterAction(e.target.value)}>
          <option value="">全部方向</option>
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <span style={{ fontSize: 13, color: '#888' }}>共 {total} 条</span>
        <Button kind="ghost" onClick={() => api.exportTrades()}>导出 CSV</Button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 16 }}>
        {/* 左: 日志列表 */}
        <Card title="成交记录">
          {trades.length === 0 ? (
            <div style={{ color: '#999', fontSize: 13 }}>暂无成交。到「自选与持仓」页录入持仓后,卖出会在此留痕。</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: '#888', textAlign: 'left' }}>
                  <th style={thStyle}>时间</th>
                  <th style={thStyle}>代码</th>
                  <th style={thStyle}>方向</th>
                  <th style={thStyle} align="right">价格</th>
                  <th style={thStyle} align="right">数量</th>
                  <th style={thStyle} align="right">盈亏</th>
                  <th style={thStyle}>原因</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} style={{ borderTop: '1px solid #f0f1f3' }}>
                    <td style={tdStyle}>{t.time.slice(5, 16)}</td>
                    <td style={tdStyle}>{t.symbol} <span style={{ color: '#aaa' }}>{t.name}</span></td>
                    <td style={tdStyle}>
                      <span style={{ color: t.action === 'buy' ? '#dc2626' : '#16a34a', fontWeight: 600 }}>
                        {t.action === 'buy' ? '买入' : '卖出'}
                      </span>
                    </td>
                    <td style={{ ...tdStyle, textAlign: 'right' }}>{t.price.toFixed(2)}</td>
                    <td style={{ ...tdStyle, textAlign: 'right' }}>{t.qty}</td>
                    <td style={{ ...tdStyle, textAlign: 'right', color: colorByPct(t.pnl), fontWeight: 600 }}>
                      {t.action === 'sell' ? (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(0) : '-'}
                    </td>
                    <td style={{ ...tdStyle, color: '#888' }}>{t.reason || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {/* 右: 减仓/清仓 + 持仓提示 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
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
            <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
              <Button kind="ghost" onClick={() => setOpMode('reduce')} style={{ flex: 1, ...(opMode === 'reduce' ? activeBtn : {}) }}>减仓</Button>
              <Button kind="danger" onClick={() => setOpMode('close')} style={{ flex: 1, ...(opMode === 'close' ? activeBtn : {}) }}>清仓</Button>
            </div>
            <div style={{ marginTop: 10 }}>
              <Button onClick={doOperation} disabled={!opSymbol.trim() || !opPrice}>
                {opMode === 'close' ? '确认清仓' : '确认减仓'}
              </Button>
            </div>
          </Card>
          <div style={{ fontSize: 11, color: '#999' }}>卖出会在交易日志留痕并计算已实现盈亏;清仓后持仓消失。CSV 导出含全量记录(Excel 可直接打开)。</div>
        </div>
      </div>
    </div>
  )
}

const thStyle: React.CSSProperties = { padding: '6px 8px', fontWeight: 500 }
const tdStyle: React.CSSProperties = { padding: '7px 8px' }
const activeBtn: React.CSSProperties = { outline: '2px solid #2563eb', outlineOffset: 1 }
