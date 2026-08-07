import type { CSSProperties, ReactNode } from 'react'
import { cn } from './utils'

export function Card({ title, children, style, extra, className }: {
  title?: ReactNode; children: ReactNode; style?: CSSProperties; extra?: ReactNode; className?: string
}) {
  return (
    <div className={cn('rounded-[10px] border border-line bg-white shadow-card', className)} style={style}>
      {(title !== undefined || extra !== undefined) && (
        <div className="flex items-center justify-between border-b border-divider px-4 py-3">
          <div className="text-[14px] font-semibold text-ink">{title}</div>
          {extra}
        </div>
      )}
      <div className={cn('p-4', !title && !extra && 'pt-4')}>{children}</div>
    </div>
  )
}
