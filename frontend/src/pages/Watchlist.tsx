import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AccountInfo, PositionItem, Quote } from '../api/client'
import { colorByPct, fmtPct } from '../const/colors'
import { Button, Card, ConfirmDialog, EmptyState, ErrorBox, Field, ListRow, Loading, PageHeader, Tag, inputStyle, toast } from '../components/ui'
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
  const queryClient = useQueryClient()
  const [error, setError] = useState('')

  // 表单
  const [watchSymbol, setWatchSymbol] = useState('')
  const [posSymbol, setPosSymbol] = useState('')
  const [posName, setPosName] = useState('')
  const [posQty, setPosQty] = useState('100')
  const [posPrice, setPosPrice] = useState('')
  // 删除二次确认(项目规则): 待移除的自选代码
  const [confirmDel, setConfirmDel] = useState<string | null>(null)
  // 强制录入确认(加仓价低于成本时弹出)
  const [forceConfirm, setForceConfirm] = useState(false)
  // 卖出快捷入口(点持仓行「卖出」弹出, 复用后端 reduce/close 逻辑)
  const [sellTarget, setSellTarget] = useState<PositionItem | null>(null)

  // 自选/持仓/账户: 每 10s 轮询(react-query 统一管理缓存/去重/清理)
  const { data: watch = [], isLoading } = useQuery({
    queryKey: ['watchlist'],
    queryFn: api.watchlist,
    refetchInterval: 10_000,
  })
  const { data: portfolio, error: posQueryError } = useQuery({
    queryKey: ['positions'],
    queryFn: api.positions,
    refetchInterval: 10_000,
  })
  const { data: account } = useQuery({
    queryKey: ['account'],
    queryFn: api.account,
    refetchInterval: 10_000,
  })
  // 自选行情: 批量接口一次请求, queryKey 随自选列表变化自动重建轮询
  const watchKey = watch.map((w) => w.symbol).join(',')
  const { data: quotes = {} } = useQuery({
    queryKey: ['quotes', watchKey],
    queryFn: () => api.quoteBatch(watch.map((w) => w.symbol)),
    enabled: watch.length > 0,
    refetchInterval: 10_000,
    select: (qs) => Object.fromEntries(qs.map((q) => [q.symbol, q])),
  })
  const err = error || (posQueryError ? String((posQueryError as Error).message || posQueryError) : '')

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    queryClient.invalidateQueries({ queryKey: ['positions'] })
    queryClient.invalidateQueries({ queryKey: ['account'] })
  }

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

  const removeWatch = (symbol: string) => setConfirmDel(symbol)

  const doRemoveWatch = async () => {
    if (!confirmDel) return
    try {
      await api.removeWatch(confirmDel)
      toast.info(`已移除 ${confirmDel}`)
      setConfirmDel(null)
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
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
      const msg = String((e as Error).message)
      // 加仓价低于成本被规则拒绝 -> 弹强制录入确认(摊薄成本, 原因自动标注)
      if (msg.includes('低于当前成本')) {
        setForceConfirm(true)
        return
      }
      setError(msg)
      toast.error(msg)
    }
  }

  // 强制录入: 确认后带 force=true 重新提交(允许低于成本加仓)
  const doForceAdd = async () => {
    setForceConfirm(false)
    try {
      await api.addPosition({
        symbol: posSymbol.trim(), name: posName.trim(),
        qty: Number(posQty), price: Number(posPrice), reason: '界面录入', force: true,
      })
      setPosSymbol(''); setPosName(''); setPosPrice('')
      toast.success(`已强制录入持仓 ${posSymbol.trim()}`)
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

  if (isLoading) return <Loading />

  return (
    <div>
      <PageHeader title="自选与持仓" />
      {err && <ErrorBox message={err} />}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[2fr_1fr]">
        {/* 左: 自选 + 持仓 */}
        <div className="flex flex-col gap-4">
          <Card title={`自选股(${watch.length})`}>
            {watch.length === 0 ? (
              <EmptyState>暂无自选。右侧添加,如 300750。</EmptyState>
            ) : (
              watch.map((w) => (
                <WatchRow key={w.symbol} symbol={w.symbol} name={w.name} quote={quotes[w.symbol]} onRemove={() => removeWatch(w.symbol)} />
              ))
            )}
          </Card>

          <Card title={`持仓(${portfolio?.positions.length ?? 0})`}>
            {!portfolio || portfolio.positions.length === 0 ? (
              <EmptyState>暂无持仓。右侧录入,如 600519。</EmptyState>
            ) : (
              <div>
                {portfolio.positions.map((p) => (
                  <PosRow key={p.symbol} p={p} onChanged={refresh} onSell={setSellTarget} />
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
            account={account ?? null}
            availableCap={availableCap}
            marketValue={portfolio?.market_value ?? 0}
            totalEquity={totalEquity}
            onChanged={refresh}
          />
        </div>
      </div>

      {/* 删除二次确认(项目规则) */}
      {confirmDel && (
        <ConfirmDialog
          title="移除自选股"
          message={`确定将 ${confirmDel} 从自选移除？`}
          onConfirm={doRemoveWatch}
          onCancel={() => setConfirmDel(null)}
        />
      )}
      {/* 强制录入确认(低于成本加仓) */}
      {forceConfirm && (
        <ConfirmDialog
          title="强制录入持仓"
          message={`加仓价低于当前成本，将摊薄成本并计入金字塔档位。确定以 ${posPrice} 强制录入 ${posSymbol.trim()} ${posQty} 股？`}
          confirmText="仍要录入"
          onConfirm={doForceAdd}
          onCancel={() => setForceConfirm(false)}
        />
      )}
      {/* 卖出快捷入口(点持仓行「卖出」弹出; 复用后端 reduce/close 逻辑) */}
      {sellTarget && (
        <SellDialog
          p={sellTarget}
          onDone={() => {
            setSellTarget(null)
            refresh()
          }}
          onClose={() => setSellTarget(null)}
        />
      )}
    </div>
  )
}

// 卖出对话框: 减仓/清仓(复用 Trades 页同款后端接口与 T+1 校验)
function SellDialog({ p, onDone, onClose }: {
  p: PositionItem
  onDone: () => void
  onClose: () => void
}) {
  const [mode, setMode] = useState<'reduce' | 'close'>('reduce')
  const [qty, setQty] = useState('')
  const [price, setPrice] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const locked = isToday(p.opened_at) // T+1: 今日买入不可减仓(后端亦校验)

  const submit = async () => {
    setErr('')
    if (!price || Number(price) <= 0) {
      setErr('请填写卖出价格')
      return
    }
    if (mode === 'reduce' && (!qty || Number(qty) <= 0)) {
      setErr('请填写减仓数量')
      return
    }
    setBusy(true)
    try {
      if (mode === 'close') {
        const r = await api.closePosition(p.symbol, Number(price))
        toast.success(`已清仓 ${p.symbol}${r.realized_pnl ? `, 实现盈亏 ¥${r.realized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` : ''}`)
      } else {
        const r = await api.reducePosition(p.symbol, Number(qty), Number(price))
        toast.success(`已减仓 ${p.symbol} ${qty} 股${r.realized_pnl ? `, 实现盈亏 ¥${r.realized_pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` : ''}`)
      }
      onDone()
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={onClose}>
      <div className="w-80 rounded-lg border border-line bg-white p-4 shadow-cardHover" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 text-[14px] font-semibold">
          卖出 {p.name} <span className="font-normal text-ink-faint">{p.symbol}</span>
        </div>
        <div className="mb-3 text-[11px] text-ink-muted">当前持仓 {p.qty} 股 · 成本 {p.cost.toFixed(2)}(含费) · 现价 {p.price.toFixed(2)}</div>
        <div className="mb-3 flex gap-2">
          <Button kind={mode === 'reduce' ? 'primary' : 'ghost'} className="h-7 flex-1 px-2 text-[12px]" onClick={() => setMode('reduce')}>
            减仓
          </Button>
          <Button kind={mode === 'close' ? 'primary' : 'ghost'} className="h-7 flex-1 px-2 text-[12px]" onClick={() => setMode('close')}>
            清仓
          </Button>
        </div>
        {mode === 'reduce' && (
          <div className="mb-3">
            <Field label={`减仓数量(最多 ${p.qty} 股)`}>
              <input style={inputStyle} type="number" value={qty} onChange={(e) => setQty(e.target.value)} placeholder={String(p.qty)} />
            </Field>
          </div>
        )}
        <div className="mb-3">
          <Field label="卖出价格">
            <input style={inputStyle} type="number" value={price} onChange={(e) => setPrice(e.target.value)} placeholder={String(p.price)} />
          </Field>
        </div>
        {locked && <div className="mb-2 text-[11px] text-[#d48806]">T+1: 今日买入, 后端将拒绝卖出(明日可操作)</div>}
        {err && <div className="mb-2 text-[11px] text-rise">{err}</div>}
        <div className="flex justify-end gap-2">
          <Button kind="ghost" className="h-7 px-3 text-[12px]" onClick={onClose}>取消</Button>
          <Button kind="danger" className="h-7 px-3 text-[12px]" onClick={submit} disabled={busy || locked}>
            {busy ? '提交中...' : mode === 'close' ? '确认清仓' : '确认减仓'}
          </Button>
        </div>
      </div>
    </div>
  )
}

function WatchRow({ symbol, name, quote, onRemove }: {
  symbol: string
  name: string
  quote?: Quote | null
  onRemove: () => void
}) {
  // 行情由页面级批量轮询(Watchlist)统一拉取后通过 props 注入, 行组件不持有轮询
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

/** 操作时间线标签颜色: 建仓=蓝 加仓=绿 减仓=橙 */
const ACTION_COLORS: Record<string, string> = { build: '#2563eb', add: '#16a34a', reduce: '#d97706' }

function PosRow({ p, onChanged, onSell }: {
  p: PositionItem
  onChanged: () => void
  onSell: (p: PositionItem) => void
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(toLocalInput(p.opened_at))
  const [saving, setSaving] = useState(false)
  const locked = isToday(p.opened_at)
  // 操作时间线(后端聚合): 仅首仓时保持原"建仓时间"简洁样式; 多笔时逐笔展示
  const acts = p.actions ?? []
  const multi = acts.length > 1

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
    <ListRow className="flex-wrap py-2.5">
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
      <div className="mt-1 flex w-full flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-faint">
        {editing ? (
          <>
            <span>建仓时间:</span>
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
            {multi ? (
              <>
                <span>操作时间:</span>
                {acts.map((a, i) => (
                  <span key={i} className="flex items-center gap-1 whitespace-nowrap" title={`${a.time} ${a.qty}股 @ ${a.price.toFixed(2)}`}>
                    <Tag color={ACTION_COLORS[a.type] ?? '#64748b'}>{a.label}</Tag>
                    <span>{a.time ? a.time.slice(5, 16) : '—'}</span>
                    <span>{a.qty}股@{a.price.toFixed(2)}</span>
                    {a.type === 'reduce' && a.pnl != null && (
                      <span className={colorByPct(a.pnl)}>
                        {a.pnl >= 0 ? '+' : ''}{a.pnl.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}元
                      </span>
                    )}
                  </span>
                ))}
              </>
            ) : (
              <>
                <span>建仓时间:</span>
                <span>{p.opened_at || '—'}</span>
              </>
            )}
            <button className="cursor-pointer border-none bg-transparent text-[11px] text-ink hover:underline" onClick={startEdit}>
              {multi ? '修改建仓时间' : '修改'}
            </button>
            <button
              className="cursor-pointer border-none bg-transparent text-[11px] text-fall hover:underline"
              onClick={() => onSell(p)}
            >
              卖出
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
          <div className="flex items-center justify-between border-t border-line pt-2 text-[13px] font-semibold">
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
