import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AccountInfo, Portfolio, PositionItem, Quote, WatchlistItem } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, EmptyState, ErrorBox, Field, ListRow, Loading, Tag, inputStyle, toast } from '../components/ui'
import SymbolInput from '../components/SymbolInput'

/** "YYYY-MM-DD HH:MM:SS" -> datetime-local 值 "YYYY-MM-DDTHH:MM" */
function toLocalInput(v: string): string {
  if (!v) return ''
  return v.replace(' ', 'T').slice(0, 16)
}
/** datetime-local 值 -> "YYYY-MM-DD HH:MM:SS" */
function fromLocalInput(v: string): string {
  if (!v) return ''
  return v.replace('T', ' ') + ':00'
}
function isToday(v: string): boolean {
  if (!v) return false
  const d = new Date()
  const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return v.slice(0, 10) === today
}
function fmtMoney(n: number): string {
  return '¥' + n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

export default function Watchlist() {
  const [watch, setWatch] = useState<WatchlistItem[]>([])
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [account, setAccount] = useState<AccountInfo | null>(null)
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
      api.account().then(setAccount).catch(() => {}),
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
      toast.success('已加入自选')
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    }
  }

  const removeWatch = async (symbol: string) => {
    try {
      await api.removeWatch(symbol)
      toast.info(`已移除 ${symbol}`)
      refresh()
    } catch (e) {
      toast.error(String((e as Error).message))
    }
  }

  const addPosition = async () => {
    try {
      await api.addPosition({
        symbol: posSymbol.trim(), name: posName.trim(),
        qty: Number(posQty), price: Number(posPrice), reason: '界面录入',
      })
      setPosSymbol(''); setPosName(''); setPosPrice('')
      toast.success(`已录入持仓 ${posSymbol.trim()}`)
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    }
  }

  // 资金账户派生(后端只存启动资金; 可用/总权益按 已实现盈亏 + 实时持仓 计算):
  //   可用资金 = 启动资金 + 已实现盈亏 - 持仓成本(含费)
  //   总权益   = 可用资金 + 持仓市值 = 启动资金 + 已实现盈亏 + 浮动盈亏
  const availableCap = account && portfolio
    ? account.start_capital + portfolio.realized_pnl - portfolio.cost_value
    : null
  const totalEquity = account && portfolio
    ? account.start_capital + portfolio.realized_pnl + portfolio.unrealized_pnl
    : null

  if (loading) return <Loading />

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">自选与持仓</h1>
      {error && <ErrorBox message={error} />}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[2fr_1fr]">
        {/* 左: 自选 + 持仓 */}
        <div className="flex flex-col gap-4">
          <Card title={`自选股(${watch.length})`}>
            {watch.length === 0 ? (
              <EmptyState>暂无自选。右侧添加,如 300750。</EmptyState>
            ) : (
              watch.map((w) => (
                <WatchRow key={w.symbol} symbol={w.symbol} name={w.name} onRemove={() => removeWatch(w.symbol)} />
              ))
            )}
          </Card>

          <Card title={`持仓(${portfolio?.positions.length ?? 0})`}>
            {!portfolio || portfolio.positions.length === 0 ? (
              <EmptyState>暂无持仓。右侧录入,如 600519。</EmptyState>
            ) : (
              <div>
                {portfolio.positions.map((p) => (
                  <PosRow key={p.symbol} p={p} onChanged={refresh} />
                ))}
                <div className="flex justify-between pt-2.5 text-[13px] font-semibold">
                  <span>合计盈亏</span>
                  <span className={colorByPct(portfolio?.unrealized_pnl)}>
                    ¥{portfolio.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
                  </span>
                </div>
                <div className="text-[11px] text-ink-faint">
                  成本为含费摊薄成本(券商口径), 浮盈已扣买入手续费 ¥
                  {portfolio.fee_cost.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  ; 卖出时另计印花税等费用。
                </div>
              </div>
            )}
          </Card>
        </div>

        {/* 右: 表单 + 资金账户 */}
        <div className="flex flex-col gap-4">
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
            <div className="mt-2 text-[11px] text-ink-faint">
              加仓价须高于当前成交均价(顺向); 手续费按设置的费率自动摊入成本。减仓到「交易日志」页操作。
            </div>
          </Card>

          <AccountCard
            account={account}
            availableCap={availableCap}
            marketValue={portfolio?.market_value ?? 0}
            totalEquity={totalEquity}
            onChanged={refresh}
          />
        </div>
      </div>
    </div>
  )
}

function WatchRow({ symbol, name, onRemove }: { symbol: string; name: string; onRemove: () => void }) {
  const [quote, setQuote] = useState<Quote | null>(null)
  useEffect(() => {
    api.quote(symbol).then((q) => setQuote(q)).catch(() => {})
    const t = setInterval(() => api.quote(symbol).then(setQuote).catch(() => {}), 10000)
    return () => clearInterval(t)
  }, [symbol])
  return (
    <ListRow className="py-2.5">
      <span className="font-semibold">{symbol} <span className="font-normal text-ink-muted">{name || quote?.name || ''}</span></span>
      <span className="flex items-center gap-2.5">
        {quote ? (
          <>
            <span className="font-semibold">{quote.price.toFixed(2)}</span>
            <Tag color={colorByPct(quote.change_pct)}>{fmtPct(quote.change_pct)}</Tag>
          </>
        ) : <span className="text-ink-faint">--</span>}
        <button onClick={onRemove} className="cursor-pointer border-none bg-transparent text-[12px] text-ink-faint hover:text-ink">✕</button>
      </span>
    </ListRow>
  )
}

function PosRow({ p, onChanged }: { p: PositionItem; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(toLocalInput(p.opened_at))
  const [saving, setSaving] = useState(false)
  const locked = isToday(p.opened_at)

  const startEdit = () => {
    setVal(toLocalInput(p.opened_at))
    setEditing(true)
  }
  const save = async () => {
    if (!val) return
    setSaving(true)
    try {
      await api.updatePositionTime(p.symbol, fromLocalInput(val))
      toast.success('持仓时间已更新')
      setEditing(false)
      onChanged()
    } catch (e) {
      toast.error(String((e as Error).message))
    } finally {
      setSaving(false)
    }
  }

  return (
    <ListRow className="py-2.5">
      <span className="font-semibold">{p.symbol} <span className="font-normal text-ink-muted">{p.name}</span></span>
      <span
        className="text-ink-secondary"
        title={`含费成本 ${p.cost.toFixed(4)} = 成交均价 ${p.cost_raw.toFixed(4)} + 摊入手续费 ¥${p.fee_cost.toFixed(2)}`}
      >
        {p.qty} 股 · 成本 {p.cost.toFixed(2)}
        <span className="ml-1 text-[10px] text-ink-faint">含费</span>
      </span>
      <span className="text-right">
        <span className={`font-semibold ${colorByPct(p.unrealized_pct)}`}>
          {p.price.toFixed(2)} ({fmtPct(p.unrealized_pct)})
        </span>
        <span className={`block text-xs ${colorByPct(p.unrealized_pnl)}`}>
          {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 元
        </span>
      </span>
      <div className="col-span-full mt-1 flex items-center gap-2 text-[11px] text-ink-faint">
        <span>持仓时间:</span>
        {editing ? (
          <>
            <input
              type="datetime-local"
              value={val}
              onChange={(e) => setVal(e.target.value)}
              style={{ ...inputStyle, width: 'auto', padding: '2px 6px', fontSize: 11 }}
            />
            <button className="cursor-pointer border-none bg-transparent text-[11px] text-ink hover:underline" onClick={save} disabled={saving}>
              {saving ? '保存中' : '保存'}
            </button>
            <button className="cursor-pointer border-none bg-transparent text-[11px] text-ink-faint hover:underline" onClick={() => setEditing(false)}>
              取消
            </button>
          </>
        ) : (
          <>
            <span>{p.opened_at || '—'}</span>
            <button className="cursor-pointer border-none bg-transparent text-[11px] text-ink hover:underline" onClick={startEdit}>
              修改
            </button>
            {locked && <Tag color="#d48806">T+1 今日买入·不可减仓</Tag>}
          </>
        )}
      </div>
    </ListRow>
  )
}

function AccountCard({
  account, availableCap, marketValue, totalEquity, onChanged,
}: {
  account: AccountInfo | null
  availableCap: number | null
  marketValue: number
  totalEquity: number | null
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState('')
  const [saving, setSaving] = useState(false)

  const startEdit = () => {
    setVal(String(account?.start_capital ?? 500000))
    setEditing(true)
  }
  const save = async () => {
    const n = Number(val)
    if (!n || n <= 0) {
      toast.error('请输入正数启动资金')
      return
    }
    setSaving(true)
    try {
      await api.updateAccount(n)
      toast.success('启动资金已更新, 可用资金已同步')
      setEditing(false)
      onChanged()
    } catch (e) {
      toast.error(String((e as Error).message))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card title="资金账户">
      {!account ? (
        <EmptyState>加载中...</EmptyState>
      ) : (
        <div className="flex flex-col gap-2 text-[13px]">
          <div className="flex items-center justify-between">
            <span className="text-ink-secondary">启动资金</span>
            {editing ? (
              <span className="flex items-center gap-1.5">
                <input
                  type="number"
                  value={val}
                  onChange={(e) => setVal(e.target.value)}
                  style={{ ...inputStyle, width: 130, padding: '3px 8px', fontSize: 12 }}
                />
                <button className="cursor-pointer border-none bg-transparent text-[12px] text-ink hover:underline" onClick={save} disabled={saving}>
                  {saving ? '保存中' : '保存'}
                </button>
                <button className="cursor-pointer border-none bg-transparent text-[12px] text-ink-faint hover:underline" onClick={() => setEditing(false)}>
                  取消
                </button>
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                <span className="font-semibold">{fmtMoney(account.start_capital)}</span>
                <button className="cursor-pointer border-none bg-transparent text-[12px] text-ink hover:underline" onClick={startEdit}>
                  修改
                </button>
              </span>
            )}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-ink-secondary">可用资金</span>
            <span className="font-semibold">{availableCap != null ? fmtMoney(availableCap) : '—'}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-ink-secondary">持仓市值</span>
            <span className="font-semibold">{fmtMoney(marketValue)}</span>
          </div>
          <div className="flex items-center justify-between border-t border-[#eee] pt-2 text-[13px] font-semibold">
            <span>总权益</span>
            <span>{totalEquity != null ? fmtMoney(totalEquity) : '—'}</span>
          </div>
          <div className="text-[11px] text-ink-faint">
            可用资金 = 启动资金 + 已实现盈亏 − 持仓成本(含费); 总权益 = 启动资金 + 已实现盈亏 + 浮动盈亏; 买入消耗资金, 卖出落袋利润, 自动同步。
          </div>
        </div>
      )}
    </Card>
  )
}
