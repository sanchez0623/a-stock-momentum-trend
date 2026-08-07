import type { CSSProperties, ReactNode } from 'react'

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="mb-2.5 block">
      <div className="mb-1 text-xs text-ink-muted">{label}</div>
      {children}
    </label>
  )
}

// 注意: 调用方存在 `{ ...inputStyle, width: X }` 展开用法, 必须保持为 JS 样式对象而非 className
export const inputStyle: CSSProperties = {
  padding: '6px 10px', border: '1px solid #d9d9d9', borderRadius: 6, fontSize: 13, width: '100%',
  boxSizing: 'border-box', outline: 'none', transition: 'border-color 0.2s',
}
