import type { ReactNode } from 'react'

// 轻量 hover 提示: 术语字段悬停显示一句话含义(无依赖, 纯 CSS)
export function Tip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <span className="group relative inline-flex">
      {children}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 hidden w-60 -translate-x-1/2 rounded-md border border-line bg-white p-2 text-left text-[11px] leading-snug text-ink shadow-cardHover group-hover:block">
        {text}
      </span>
    </span>
  )
}
