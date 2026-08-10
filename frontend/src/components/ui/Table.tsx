import type { CSSProperties, ReactNode } from 'react'
import { cn } from './utils'

// 统一表格外壳: 横向滚动容器 + 13px 正文基准(表格仅用于数据陈列)
export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={cn('w-full border-collapse text-[13px]', className)}>{children}</table>
    </div>
  )
}

// 表头单元格: 左对齐灰字, right 用于数值列右对齐
export function Th({ children, right, className }: { children?: ReactNode; right?: boolean; className?: string }) {
  return (
    <th className={cn('px-2 py-2 text-left font-medium whitespace-nowrap text-ink-muted', right && 'text-right', className)}>
      {children}
    </th>
  )
}

// 数据单元格: 统一内边距/顶部对齐, right 用于数值列右对齐
// 预留 style 供行内特殊样式(如涨跌色)使用
export function Td({ children, right, className, colSpan, style }: {
  children?: ReactNode
  right?: boolean
  className?: string
  colSpan?: number
  style?: CSSProperties
}) {
  return (
    <td colSpan={colSpan} style={style} className={cn('px-2 py-2 align-top', right && 'text-right', className)}>
      {children}
    </td>
  )
}
