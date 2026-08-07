// 涨跌色全局常量(中文约定: 涨红跌绿) — 内部使用, 外部一律走 colorByPct/fmtPct
const UP = '#ef4142' // 红: 涨
const DOWN = '#22a55b' // 绿: 跌
const FLAT = '#8a8f99' // 平

export function colorByPct(pct: number | undefined | null): string {
  if (pct === undefined || pct === null || pct === 0) return FLAT
  return pct > 0 ? UP : DOWN
}

export function fmtPct(pct: number | undefined | null): string {
  if (pct === undefined || pct === null || Number.isNaN(pct)) return '-'
  return `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`
}
