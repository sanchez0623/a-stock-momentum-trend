import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Portfolio, WatchlistItem } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, ErrorBox, Field, Loading, Tag, inputStyle } from '../components/ui'
import SymbolInput from '../components/SymbolInput'

export default function Watchlist() {
  const [watch, setWatch] = useState<WatchlistItem[]>([])
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 表单
  const [watchSymbol, setWatchSymbol] = useState('')
  const [posSymbol, setPosSymbol] = useState('')
  const [posName, setPosName] = useState('')
  const [posQty, setPosQty] = useState('100')
  const [posPrice, setPosPrice] = useState('')

  const refresh = () =>
    Promise.all([
      api.watchlist().then(setWatch).catch(() => {}),
      api.positions().then(setPortfolio).catch((e) => setError(String(e.message || e))),
    ])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    const timer = setInterval(refresh, 10000)
    return () => clearInterval(timer)
  }, [])

  const addWatch = async () => {
    if (!watchSymbol.trim()) return
    try {
      await api.addWatch(watchSymbol.trim())
      setWatchSymbol('')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
    }
  }

  const removeWatch = async (symbol: string) => {
    await api.removeWatch(symbol)
    refresh()
  }

  const addPosition = async () => {
    try {
      await api.addPosition({
        symbol: posSymbol.trim(), name: posName.trim(),
        qty: Number(posQty), price: Number(posPrice), reason: '界面录入',
      })
      setPosSymbol(''); setPosName(''); setPosPrice('')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>自选与持仓</h1>
      {error && <ErrorBox message={error} />}

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 16 }}>
        {/* 左: 自选 + 持仓 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Card title={`自选股(${watch.length})`}>
            {watch.length === 0 ? (
              <div style={{ color: '#999', fontSize: 13 }}>暂无自选。右侧添加,如 300750。</div>
            ) : (
              watch.map((w) => (
                <WatchRow key={w.symbol} symbol={w.symbol} name={w.name} onRemove={() => removeWatch(w.symbol)} />
              ))
            )}
          </Card>

          <Card title={`持仓(${portfolio?.positions.length ?? 0})`}>
            {!portfolio || portfolio.positions.length === 0 ? (
              <div style={{ color: '#999', fontSize: 13 }}>暂无持仓。右侧录入,如 600519。</div>
            ) : (
              <div>
                {portfolio.positions.map((p) => (
                  <div key={p.symbol} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                    <span style={{ fontWeight: 600 }}>{p.symbol} <span style={{ fontWeight: 400, color: '#888' }}>{p.name}</span></span>
                    <span style={{ color: '#666' }}>{p.qty} 股 · 成本 {p.cost.toFixed(2)}</span>
                    <span style={{ color: colorByPct(p.unrealized_pct), fontWeight: 600 }}>
                      {p.price.toFixed(2)} ({fmtPct(p.unrealized_pct)})
                    </span>
                  </div>
                ))}
                <div style={{ display: 'flex', justifyContent: 'space-between', paddingTop: 10, fontSize: 13, fontWeight: 600 }}>
                  <span>合计盈亏</span>
                  <span style={{ color: colorByPct(portfolio?.unrealized_pnl) }}>
                    ¥{portfolio.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                  </span>
                </div>
              </div>
            )}
          </Card>
        </div>

        {/* 右: 表单 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Card title="添加自选">
            <Field label="股票代码">
              <SymbolInput value={watchSymbol} onChange={setWatchSymbol} onNameFound={() => {}} placeholder="如 300750 / 600519" />
            </Field>
            <Button onClick={addWatch} disabled={!watchSymbol.trim()}>添加</Button>
          </Card>

          <Card title="录入持仓(虚拟)">
            <Field label="股票代码">
              <SymbolInput value={posSymbol} onChange={setPosSymbol} onNameFound={setPosName} placeholder="如 600519" />
            </Field>
            <Field label="名称(自动带出,可改)">
              <input style={inputStyle} value={posName} onChange={(e) => setPosName(e.target.value)} placeholder="如 贵州茅台" />
            </Field>
            <Field label="数量(股)">
              <input style={inputStyle} type="number" value={posQty} onChange={(e) => setPosQty(e.target.value)} />
            </Field>
            <Field label="成交价">
              <input style={inputStyle} type="number" value={posPrice} onChange={(e) => setPosPrice(e.target.value)} placeholder="如 1300" />
            </Field>
            <Button onClick={addPosition} disabled={!posSymbol.trim() || !posPrice}>录入</Button>
            <div style={{ fontSize: 11, color: '#999', marginTop: 8 }}>加仓价须高于当前成本(顺向); 减仓到「交易日志」页(三期)。</div>
          </Card>
        </div>
      </div>
    </div>
  )
}

function WatchRow({ symbol, name, onRemove }: { symbol: string; name: string; onRemove: () => void }) {
  const [quote, setQuote] = useState<import('../api/client').Quote | null>(null)
  useEffect(() => {
    api.quote(symbol).then((q) => setQuote(q)).catch(() => {})
    const t = setInterval(() => api.quote(symbol).then(setQuote).catch(() => {}), 10000)
    return () => clearInterval(t)
  }, [symbol])
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
      <span style={{ fontWeight: 600 }}>{symbol} <span style={{ fontWeight: 400, color: '#888' }}>{name || quote?.name || ''}</span></span>
      <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        {quote ? (
          <>
            <span style={{ fontWeight: 600 }}>{quote.price.toFixed(2)}</span>
            <Tag color={colorByPct(quote.change_pct)}>{fmtPct(quote.change_pct)}</Tag>
          </>
        ) : <span style={{ color: '#bbb' }}>--</span>}
        <button onClick={onRemove} style={{ border: 'none', background: 'none', color: '#999', cursor: 'pointer', fontSize: 12 }}>✕</button>
      </span>
    </div>
  )
}
