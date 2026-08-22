import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { EvaluateResult } from '../api/client'
import { Button, Tag, toast } from './ui'
import { SIGNAL_META } from './ui'
import type { ScreenerResult } from '../api/client'

/**
 * 信号速览弹窗: 选股页内直接查看单票信号评估结果, 不跳转信号中心.
 *
 * - 打开即评估(走缓存 K 线, 秒级); ←/→ 切换上下只, Esc 关闭
 * - 三结果态: 有信号(Tag+强度+理由) / 无信号(展示该行风险提示辅助判断) / 评估失败(红字)
 * - 有信号可直接「生成计划」(不跳页); 保留「在信号中心打开」逃生口
 * - 预扫缓存: 批量看信号扫过的票直接出结果, 弹窗内可「重新评估」
 */

type EvalMap = Record<string, EvaluateResult>

export default function SignalModal({
  rows, index, onClose, onNav, evalCache,
}: {
  /** 打开时的结果快照(按当前筛选后的顺序), 中途改筛选不影响本弹窗 */
  rows: ScreenerResult[]
  index: number
  onClose: () => void
  /** 导航到快照内第 i 只 */
  onNav: (i: number) => void
  /** 批量预扫结果(可选): symbol -> 评估结果, 命中则免请求直接展示 */
  evalCache?: EvalMap
}) {
  const row = rows[index]
  const navigate = useNavigate()
  const [res, setRes] = useState<EvaluateResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [genBusy, setGenBusy] = useState(false)

  const evaluate = useCallback(async (force = false) => {
    if (!row) return
    if (!force && evalCache?.[row.symbol]) {
      setRes(evalCache[row.symbol])
      return
    }
    setLoading(true)
    setRes(null)
    try {
      setRes(await api.evaluateSignal(row.symbol))
    } catch (e) {
      setRes({ symbol: row.symbol, name: row.name || '', price: 0, signal: null, error: String((e as Error).message) })
    } finally {
      setLoading(false)
    }
  }, [row, evalCache])

  // 切换股票即评估
  useEffect(() => {
    evaluate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.symbol])

  // 键盘导航: ←/→ 切换, Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft' && index > 0) onNav(index - 1)
      else if (e.key === 'ArrowRight' && index < rows.length - 1) onNav(index + 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, rows.length, onClose, onNav])

  if (!row) return null

  const sig = res?.signal
  const meta = sig ? SIGNAL_META[sig.type] ?? { label: sig.type, color: '#64748b' } : null

  const doGenerate = async () => {
    if (!sig || genBusy) return
    setGenBusy(true)
    try {
      const plan = await api.generatePlan(row.symbol, row.name || '')
      if (!plan) toast.info('当前无信号, 暂不生成计划')
      else toast.success(`已生成 ${row.symbol} 的交易计划`)
    } catch (e) {
      toast.error(String((e as Error).message))
    } finally {
      setGenBusy(false)
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.15 }}
        className="w-full max-w-[480px] rounded-lg border border-line bg-white p-4 shadow-cardHover"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部: 行内上下文(零请求) + 导航 */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-[15px] font-semibold text-ink">{row.name || row.symbol}</span>
            <span className="shrink-0 text-[12px] text-ink-faint">{row.symbol}</span>
            {res?.price ? <span className="shrink-0 text-[12px] text-ink-muted">@{res.price.toFixed(2)}</span> : null}
            <span className="shrink-0 text-[12px] font-semibold text-ink">{row.total.toFixed(1)}分</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer border-none bg-transparent px-1 text-[16px] leading-none text-ink-faint hover:text-ink"
            aria-label="关闭"
          >
            ×
          </button>
        </div>
        {/* 位置指示 + 上下只导航 */}
        <div className="mt-1 flex items-center justify-between text-[11px] text-ink-faint">
          <span>{index + 1} / {rows.length} · ←/→ 切换, Esc 关闭</span>
          <span className="flex items-center gap-1">
            <Button kind="ghost" className="h-6 px-2 text-[11px]" disabled={index === 0} onClick={() => onNav(index - 1)}>‹ 上一只</Button>
            <Button kind="ghost" className="h-6 px-2 text-[11px]" disabled={index >= rows.length - 1} onClick={() => onNav(index + 1)}>下一只 ›</Button>
          </span>
        </div>

        {/* 主体: 三结果态 */}
        <div className="mt-3 min-h-[120px] rounded border border-divider bg-[#fafbfc] p-3">
          {loading ? (
            <div className="flex h-[120px] items-center justify-center text-[13px] text-ink-faint">评估中...</div>
          ) : res?.error ? (
            <div>
              <div className="text-[13px] font-medium text-rise">评估失败</div>
              <p className="mt-1 border-l-2 border-rise/40 pl-2.5 text-xs leading-relaxed text-rise">{res.error}</p>
            </div>
          ) : sig ? (
            <div>
              <div className="flex items-center gap-2">
                <Tag color={meta!.color}>{meta!.label}</Tag>
                <b className={sig.strength >= 70 ? 'text-[14px] text-rise' : 'text-[14px] text-ink-secondary'}>{sig.strength.toFixed(0)}</b>
                <span className="text-[11px] text-ink-faint">强度</span>
              </div>
              {sig.reason && (
                <p className="mt-2 border-l-2 border-divider pl-2.5 text-xs leading-relaxed text-ink-secondary">{sig.reason}</p>
              )}
            </div>
          ) : res ? (
            /* 无信号: 借助行内数据解释"为什么没戏/差多远", 辅助判断 */
            <div>
              <div className="text-[13px] font-medium text-ink-muted">当前无信号(不满足触发条件)</div>
              {row.risk ? (
                <p className="mt-1.5 border-l-2 border-divider pl-2.5 text-xs leading-relaxed text-ink-secondary">{row.risk}</p>
              ) : row.reason ? (
                <p className="mt-1.5 border-l-2 border-divider pl-2.5 text-xs leading-relaxed text-ink-secondary">{row.reason}</p>
              ) : (
                <p className="mt-1.5 text-xs text-ink-faint">可点「重新评估」或到信号中心查看该票历史信号。</p>
              )}
            </div>
          ) : (
            <div className="flex h-[120px] items-center justify-center text-[13px] text-ink-faint">准备评估...</div>
          )}
        </div>

        {/* 底部操作 */}
        <div className="mt-3 flex items-center justify-between gap-2">
          <Button kind="ghost" className="h-7 px-2 text-xs" onClick={() => evaluate(true)} disabled={loading}>
            重新评估
          </Button>
          <div className="flex items-center gap-2">
            <Button
              kind="ghost"
              className="h-7 px-2 text-xs"
              onClick={() => navigate(`/signals?symbol=${row.symbol}&name=${encodeURIComponent(row.name || '')}`)}
            >
              在信号中心打开
            </Button>
            {sig && (
              <Button className="h-7 px-3 text-xs" onClick={doGenerate} disabled={genBusy}>
                {genBusy ? '生成中...' : '生成计划'}
              </Button>
            )}
          </div>
        </div>
      </motion.div>
    </div>,
    document.body,
  )
}
