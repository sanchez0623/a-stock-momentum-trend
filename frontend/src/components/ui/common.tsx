import type { ReactNode } from 'react'
import { cn } from './utils'

// 空状态提示: 统一灰字弱化文案
export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="text-[13px] text-ink-faint">{children}</div>
}

// 列表行: 左右分布 + 下边框 + 等宽数字, 仪表盘/信号/持仓列表通用
export function ListRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex items-center justify-between border-b border-divider py-2 text-[13px] last:border-b-0', className)}>
      {children}
    </div>
  )
}

// 表单操作行: 标签(hint 次级说明) + 控件 + 操作按钮, 统一底对齐
// 约定: 控件(inputStyle 约 33px)与操作按钮默认高度一致, 保证视觉对齐
export function FormRow({ label, hint, children, action, className }: {
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex items-end gap-2', className)}>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-baseline gap-2">
          <span className="shrink-0 text-xs text-ink-muted">{label}</span>
          {hint !== undefined && <span className="truncate text-[11px] text-ink-faint">{hint}</span>}
        </div>
        {children}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}
