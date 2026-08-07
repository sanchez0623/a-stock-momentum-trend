import type { ReactNode } from 'react'

// 空状态提示: 统一灰字弱化文案
export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="text-[13px] text-ink-faint">{children}</div>
}

// 列表行: 左右分布 + 下边框 + 等宽数字, 仪表盘/信号/持仓列表通用
export function ListRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={`flex items-center justify-between border-b border-divider py-2 text-[13px] last:border-b-0 ${className ?? ''}`}>
      {children}
    </div>
  )
}
