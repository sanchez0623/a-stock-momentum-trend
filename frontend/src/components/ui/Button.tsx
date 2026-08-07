import type { CSSProperties, ReactNode } from 'react'
import { cn } from './utils'

const KIND_CLASS: Record<string, string> = {
  primary: 'bg-primary text-white hover:bg-primary-dark focus-visible:ring-2 focus-visible:ring-primary/30',
  ghost: 'border border-line bg-white text-ink hover:border-link hover:text-link',
  danger: 'bg-rise text-white hover:opacity-90',
  default: 'border border-line bg-white text-ink hover:border-link hover:text-link',
  dashed: 'border border-dashed border-line bg-white text-ink hover:border-link hover:text-link',
}

export function Button({ children, onClick, kind = 'primary', disabled, style, type, danger, className }: {
  children: ReactNode
  onClick?: () => void
  kind?: 'primary' | 'ghost' | 'danger' | 'default' | 'dashed'
  disabled?: boolean
  style?: CSSProperties
  type?: 'button' | 'submit'
  danger?: boolean
  className?: string
}) {
  const kindKey = danger || kind === 'danger' ? 'danger' : KIND_CLASS[kind] ? kind : 'primary'
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'inline-flex items-center justify-center rounded px-4 py-1.5 text-[13px] transition-colors duration-150 select-none',
        'disabled:cursor-not-allowed disabled:opacity-50',
        KIND_CLASS[kindKey],
        className,
      )}
      style={{ fontSize: 13, ...style }}
    >
      {children}
    </button>
  )
}
