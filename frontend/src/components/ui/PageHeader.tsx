import type { ReactNode } from 'react'

// 页面页头: 统一所有页面标题区(标题 + 可选副标题 + 可选操作区)
// 各路由页必须使用本组件, 保证切换页面时顶部标题位置/间距一致
export function PageHeader({ title, subtitle, extra }: {
  title: ReactNode
  subtitle?: ReactNode
  extra?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-[20px] font-semibold leading-snug text-ink">{title}</h1>
        {subtitle !== undefined && <div className="mt-1 text-xs leading-relaxed text-ink-muted">{subtitle}</div>}
      </div>
      {extra !== undefined && <div className="shrink-0">{extra}</div>}
    </div>
  )
}
