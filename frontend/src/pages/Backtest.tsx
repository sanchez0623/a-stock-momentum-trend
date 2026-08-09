import { useState } from 'react'
import { api, type BacktestFactorReport, type BacktestHoldStats } from '../api/client'
import { Button, Card, ErrorBox, Loading } from '../components/ui'

// 阶段展示顺序(按风险递增)与配色: 红=利多/橙=需注意/绿=偏空
const STAGE_META: Record<string, { label: string; color: string }> = {
  launch: { label: '启动期', color: '#dc2626' },
  accelerate: { label: '加速期', color: '#dc2626' },
  overheat: { label: '过热期', color: '#ea580c' },
  exhaust: { label: '衰竭期', color: '#16a34a' },
  none: { label: '无趋势', color: '#64748b' },
}
const HOLD_LABELS: Record<string, string> = { hold_5: '5日', hold_10: '10日', hold_20: '20日' }

function fmt(v: number, suffix = '') {
  const s = v > 0 ? '+' : ''
  return `${s}${v.toFixed(2)}${suffix}`
}

function HoldCell({ s }: { s: BacktestHoldStats }) {
  const color = s.expectancy > 0 ? '#dc2626' : s.expectancy < 0 ? '#16a34a' : '#334155'
  return (
    <td className="px-3 py-2 align-top">
      <div className="text-[15px] font-bold" style={{ color }}>
        胜率 {s.win_rate.toFixed(1)}%
      </div>
      <div className="mt-0.5 text-[11px] leading-snug text-ink-muted">
        均值 {fmt(s.avg, '%')} · 中位 {fmt(s.median, '%')}
      </div>
      <div className="text-[11px] text-ink-faint">样本 {s.n}</div>
    </td>
  )
}

export default function Backtest() {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [report, setReport] = useState<BacktestFactorReport | null>(null)

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const r = await api.backtestFactor({ hold_days: [5, 10, 20], cost: true })
      setReport(r)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setRunning(false)
    }
  }

  const stages = report ? Object.entries(report.by_stage) : []
  const distTotal = report
    ? Object.values(report.stage_distribution).reduce((a, b) => a + b, 0)
    : 0

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4">
        <h1 className="text-[20px] font-semibold">回测中心</h1>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">
          阶段分桶验证（方案 C）：对缓存的历史 K 线逐日判定趋势阶段，按「信号日 T 收盘判定 → T+1 开盘买入
          → T+N 收盘卖出」统计各阶段在未来 5/10/20 日的胜率与期望收益（已扣双边手续费）。
          用于验证「启动期买入优于过热期追高」的打法假设。
        </p>
      </div>

      <Card className="mb-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={run} disabled={running}>
            {running ? '回测运行中…' : '运行阶段分桶回测'}
          </Button>
          <span className="text-[12px] text-ink-faint">
            数据源：本地 K 线缓存（盘后预热落库），全市场约 {report?.meta.symbols_total ?? '3600+'} 只 · 耗时约 1 分钟
          </span>
        </div>
        {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      </Card>

      {running && (
        <Card className="p-6">
          <Loading text="正在逐日回放阶段判定与收益统计…" />
        </Card>
      )}

      {report && !running && (
        <>
          {/* 样本范围 */}
          <Card className="mb-4 p-3 text-[12px] leading-relaxed text-ink-muted">
            参与股票 <b className="text-ink">{report.meta.symbols_used}</b> / {report.meta.symbols_total} 只
            {report.meta.date_from && report.meta.date_to && (
              <> · 信号日期 {report.meta.date_from} ~ {report.meta.date_to}</>
            )}
            {' '}· {report.meta.cost_included ? '已扣双边手续费' : '未扣费'}
          </Card>

          {/* 阶段分布 */}
          <Card className="mb-4 p-4">
            <div className="mb-2 text-[13px] font-semibold text-ink">信号阶段分布（全部信号日）</div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(report.stage_distribution)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => {
                  const m = STAGE_META[k] ?? { label: k, color: '#64748b' }
                  const pct = distTotal ? (v / distTotal) * 100 : 0
                  return (
                    <div key={k} className="rounded-lg border border-line bg-white px-3 py-1.5 text-[12px]">
                      <span className="font-semibold" style={{ color: m.color }}>{m.label}</span>
                      <span className="ml-1.5 text-ink-muted">{v} 次 · {pct.toFixed(1)}%</span>
                    </div>
                  )
                })}
            </div>
          </Card>

          {/* 分桶胜率表 */}
          <Card className="overflow-x-auto p-2">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-left text-ink-muted">
                  <th className="px-3 py-2">阶段</th>
                  {report.meta.hold_days.map((h) => (
                    <th key={h} className="px-3 py-2 text-right">{HOLD_LABELS[`hold_${h}`] ?? `${h}日`}持有</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stages.map(([k, info]) => {
                  const m = STAGE_META[k] ?? { label: k, color: '#64748b' }
                  return (
                    <tr key={k} className="border-t border-divider">
                      <td className="px-3 py-2">
                        <div className="text-[14px] font-semibold" style={{ color: m.color }}>{info.label}</div>
                      </td>
                      {report.meta.hold_days.map((h) => {
                        const s = info.holds[`hold_${h}`]
                        return s ? <HoldCell key={h} s={s} /> : <td key={h} className="px-3 py-2 text-ink-faint">-</td>
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Card>

          {/* 解读提示 */}
          <Card className="mt-4 p-3 text-[12px] leading-relaxed text-ink-muted">
            <b className="text-ink">怎么看：</b>
            胜率为正收益比例，均值/中位数为净收益率（%）。优先看 <b className="text-ink">20 日期望</b>：
            正期望越大越适合当前阶段买入持有；无趋势/衰竭期期望为负时应回避。
            样本区间覆盖较短（多数股票仅近几个月），结论仅供参考，建议持续回测积累。
          </Card>
        </>
      )}

      {!report && !running && !error && (
        <Card className="p-8 text-center text-[13px] text-ink-faint">
          点击上方按钮，用本地缓存历史数据运行阶段分桶回测
        </Card>
      )}
    </div>
  )
}
