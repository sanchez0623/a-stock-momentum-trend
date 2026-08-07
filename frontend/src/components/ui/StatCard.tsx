import type { CSSProperties, ReactNode } from 'react'
import { Card } from './Card'
import { cn } from './utils'

// 统计卡片: 标题 + 大号数值 + 副文本(仪表盘/复盘页通用)
export function StatCard({ title, value, sub, valueClassName, className, style }: {
  title: string
  value: ReactNode
  sub?: ReactNode
  valueClassName?: string
  className?: string
  style?: CSSProperties
}) {
  return (
    <Card title={title} className={className} style={style}>
      <div className={cn('text-[22px] font-bold leading-tight', valueClassName)}>{value}</div>
      {sub !== undefined && <div className="mt-1 text-xs text-ink-muted">{sub}</div>}
    </Card>
  )
}
