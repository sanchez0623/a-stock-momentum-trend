import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { PlanRecord } from '../api/client'
import { Button, Card, ErrorBox, EmptyState, Field, Loading, PageHeader, Tag, Tip, toast } from '../components/ui'
import SymbolInput from '../components/SymbolInput'

// 计划正文三段式解析(兼容旧版无分组标记的纯文本):
//   行动(建议操作/价位) -> 依据(状态/模式/信号) -> 风控与纪律
function parsePlan(content: string): { action: string; prices: string[]; context: string[]; control: string[]; raw: boolean } {
  const lines = content.split('\n')
  const out = { action: '', prices: [] as string[], context: [] as string[], control: [] as string[], raw: false }
  let section = ''
  for (const raw of lines) {
    const line = raw.trim()
    if (line.startsWith('━━')) {
      section = line.includes('依据') ? 'context' : line.includes('风控') ? 'control' : 'action'
      continue
    }
    if (!line) continue
    if (section === 'action') {
      if (line.startsWith('建议操作:')) out.action = line.slice('建议操作:'.length).trim()
      else if (line.startsWith('触发') || line.startsWith('止损') || line.startsWith('止盈')) out.prices.push(line)
    } else if (section === 'context') {
      out.context.push(line)
    } else if (section === 'control') {
      out.control.push(line)
    }
  }
  if (!out.action && out.prices.length === 0) out.raw = true // 旧版格式, 回退纯文本
  return out
}

// 风控/纪律行渲染: 术语字段悬停提示含义(交易说明书同源精简版)
function ControlLine({ line }: { line: string }) {
  if (line.startsWith('风控检查:')) {
    const rest = line.slice('风控检查:'.length).trim()
    const parts = rest.split('|')
    return (
      <div>
        <span className="mr-1">风控检查:</span>
        {parts.map((p, i) => {
          const seg = p.trim()
          const tip =
            seg.startsWith('日亏损熔断')
              ? '当日账户浮亏达 3% 时, 当天禁止一切新开仓(已有持仓的止损/减仓照常)'
              : seg.startsWith('防守模式')
                ? '账户净值从高点回撤超 10% 时进入防守, 只减不加, 买入类信号全部拦截'
                : seg.startsWith('总仓位')
                  ? '持仓市值 ÷ 总资金的比例, 超过 80% 上限禁止加仓'
                  : ''
          return (
            <span key={i}>
              {i > 0 && <span className="mx-1">|</span>}
              {tip ? <Tip text={tip}>{seg}</Tip> : <span>{seg}</span>}
            </span>
          )
        })}
      </div>
    )
  }
  if (line.startsWith('一致性:')) {
    return (
      <div>
        <Tip text="信号建议动作与计划最终动作的对应关系; 不一致时注明原因(档位用尽/市况不明/T+1等)">一致性:</Tip>
        <span>{line.slice('一致性:'.length)}</span>
      </div>
    )
  }
  if (line.startsWith('纪律提醒:')) {
    return (
      <div>
        <Tip text="计划执行纪律: 信号与计划一致时才动手; 加仓后总仓位不超上限">纪律提醒:</Tip>
        <span>{line.slice('纪律提醒:'.length)}</span>
      </div>
    )
  }
  return <div>{line}</div>
}

// 行动卡片配色: 买入=红(利多) / 卖出=绿(偏空) / 观望=灰
const ACTION_STYLE: Record<string, { card: string; label: string }> = {
  buy_first: { card: 'border-rise/30 bg-[#fef2f2]', label: '建仓' },
  buy_add: { card: 'border-rise/30 bg-[#fef2f2]', label: '加仓' },
  t_buy: { card: 'border-rise/30 bg-[#fef2f2]', label: '做T买入' },
  sell_reduce: { card: 'border-fall/30 bg-[#f0fdf4]', label: '减仓' },
  sell_stop: { card: 'border-fall/30 bg-[#f0fdf4]', label: '止损' },
  t_sell: { card: 'border-fall/30 bg-[#f0fdf4]', label: '做T卖出' },
  hold: { card: 'border-line bg-[#fafafa]', label: '观望' },
}

function PlanCard({ p, onMark }: { p: PlanRecord; onMark: (id: number, status: 'done' | 'ignored') => void }) {
  const parsed = parsePlan(p.content)
  const st = ACTION_STYLE[p.action] ?? ACTION_STYLE.hold
  return (
    <Card className="mb-3" title={`计划 #${p.id} · ${p.symbol} ${p.name || ''} · ${p.time}`}>
      {parsed.raw ? (
        <pre className="m-0 text-[13px] leading-[1.7]" style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{p.content}</pre>
      ) : (
        <>
          {/* 行动区: 建议操作置顶加粗 + 价位行 */}
          <div className={`rounded-lg border p-3 ${st.card}`}>
            <div className="mb-1 flex items-center gap-2 text-[11px] opacity-70">
              <span>建议操作</span>
              <Tag color={p.action === 'hold' ? '#64748b' : p.action.startsWith('sell') ? '#16a34a' : '#dc2626'}>{st.label}</Tag>
            </div>
            <div className="text-[15px] font-bold leading-snug">{parsed.action}</div>
            {parsed.prices.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-ink-secondary">
                {parsed.prices.map((line, i) => {
                  const [k, ...rest] = line.split(':')
                  return (
                    <span key={i}>
                      <span className="text-ink-faint">{k}:</span>
                      <span className="ml-1">{rest.join(':').trim()}</span>
                    </span>
                  )
                })}
              </div>
            )}
          </div>

          {/* 依据区: 状态/模式/信号(为什么) */}
          {parsed.context.length > 0 && (
            <div className="mt-2.5 rounded border border-divider bg-[#fafbfc] px-3 py-2 text-[12px] leading-[1.8] text-ink-muted">
              <div className="mb-0.5 text-[11px] text-ink-faint">依据</div>
              {parsed.context.map((line, i) => (
                <div key={i}>{line}</div>
              ))}
            </div>
          )}

          {/* 风控与纪律区 */}
          {parsed.control.length > 0 && (
            <div className="mt-2 text-[11px] leading-[1.8] text-ink-faint">
              {parsed.control.map((line, i) => (
                <ControlLine key={i} line={line} />
              ))}
            </div>
          )}
        </>
      )}
      <div className="mt-3 flex gap-2">
        <Button kind="primary" onClick={() => onMark(p.id, 'done')}>标记已执行</Button>
        <Button kind="ghost" onClick={() => onMark(p.id, 'ignored')}>标记已忽略</Button>
      </div>
    </Card>
  )
}

export default function Plans() {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [generating, setGenerating] = useState(false)

  const { data: plans = [], isLoading, error: queryError } = useQuery({
    queryKey: ['plans'],
    queryFn: api.currentPlans,
  })
  const err = error || (queryError ? String((queryError as Error).message || queryError) : '')
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['plans'] })

  const generate = async () => {
    if (!symbol.trim()) return
    setGenerating(true)
    setError('')
    try {
      const plan = await api.generatePlan(symbol.trim(), name)
      if (!plan) {
        setGenerating(false)
        toast.info('当前无信号, 暂不生成计划')
        return
      }
      setSymbol('')
      setName('')
      toast.success(`已生成 ${symbol.trim()} 的交易计划`)
      refresh()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    } finally {
      setGenerating(false)
    }
  }

  const mark = async (id: number, status: 'done' | 'ignored') => {
    try {
      await api.planStatus(id, status)
      toast.success(status === 'done' ? '已标记执行' : '已标记忽略')
      refresh()
    } catch (e) {
      toast.error(String((e as Error).message))
    }
  }

  if (isLoading) return <Loading />

  return (
    <div>
      <PageHeader title="交易计划" />
      {err && <ErrorBox message={err} />}

      <Card title="生成计划(需先有信号, 持仓票评估效果更佳)">
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Field label="股票代码">
              <SymbolInput value={symbol} onChange={setSymbol} onNameFound={setName} placeholder="如 600519(已持仓)" />
            </Field>
          </div>
          {name && <div className="pb-2.5 text-[13px] text-ink-secondary">{name}</div>}
          <Button onClick={generate} disabled={generating || !symbol.trim()}>{generating ? '生成中...' : '生成计划'}</Button>
        </div>
      </Card>

      {plans.length === 0 ? (
        <Card><EmptyState>暂无待执行计划。先到「信号中心」评估, 或直接在持仓票上生成。</EmptyState></Card>
      ) : (
        plans.map((p) => <PlanCard key={p.id} p={p} onMark={mark} />)
      )}
    </div>
  )
}
