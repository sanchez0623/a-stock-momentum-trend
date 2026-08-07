import type { ReactNode } from 'react'
import { hexToRgba } from './utils'

// 接受任意 hex: 浅色背景 + 同色系文字(antd 呈现方式)
export function Tag({ children, color }: { children: ReactNode; color: string }) {
  return (
    <span
      className="inline-flex items-center rounded px-1.5 py-0.5 text-xs leading-none"
      style={{ backgroundColor: hexToRgba(color, 0.12), color }}
    >
      {children}
    </span>
  )
}

// 信号类型 -> 标签颜色/文案
export const SIGNAL_META: Record<string, { label: string; color: string }> = {
  BUY_FIRST: { label: '首仓', color: '#dc2626' },
  BUY_ADD: { label: '加仓', color: '#ea580c' },
  SELL_REDUCE: { label: '减仓', color: '#16a34a' },
  SELL_STOP: { label: '止损', color: '#111827' },
  T_BUY: { label: '做T买', color: '#db2777' },
  T_SELL: { label: '做T卖', color: '#0891b2' },
}
