import type { CSSProperties } from 'react'
import { cn } from './utils'

// 骨架屏基础块: 微光脉冲动画
export function Skeleton({ className, style }: { className?: string; style?: CSSProperties }) {
  return (
    <div
      className={cn('animate-pulse rounded bg-divider', className)}
      style={style}
    />
  )
}

// 统计卡骨架: 模拟 StatCard 的标题+大数字+副文本
export function StatCardSkeleton() {
  return (
    <div className="rounded-[10px] border border-line bg-white p-4 shadow-card">
      <Skeleton className="mb-3 h-3.5 w-16" />
      <Skeleton className="h-7 w-24" />
      <Skeleton className="mt-2 h-3 w-20" />
    </div>
  )
}

// 列表骨架: N 行模拟 ListRow
export function ListSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="flex flex-col">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center justify-between border-b border-divider py-2.5 last:border-b-0">
          <Skeleton className="h-3.5 w-32" />
          <Skeleton className="h-3.5 w-20" />
        </div>
      ))}
    </div>
  )
}

// 页面骨架: 标题 + 统计卡组 + 两栏列表(仪表盘等首页用)
export function PageSkeleton() {
  return (
    <div>
      <Skeleton className="mb-1 h-6 w-24" />
      <Skeleton className="mb-4 h-3 w-56" />
      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        {Array.from({ length: 4 }, (_, i) => <StatCardSkeleton key={i} />)}
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded-[10px] border border-line bg-white p-4 shadow-card"><ListSkeleton rows={4} /></div>
        <div className="rounded-[10px] border border-line bg-white p-4 shadow-card"><ListSkeleton rows={4} /></div>
      </div>
    </div>
  )
}
