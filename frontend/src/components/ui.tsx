// 共享 UI 基础组件(轻量自建, 控制体积)
import type { ReactNode } from 'react'

export function Card({ title, children, style }: { title?: ReactNode; children: ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ border: '1px solid #e5e6eb', borderRadius: 10, padding: '16px 18px', background: '#fff', ...style }}>
      {title !== undefined && <div style={{ fontWeight: 600, marginBottom: 12, fontSize: 14 }}>{title}</div>}
      {children}
    </div>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 10 }}>
      <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>{label}</div>
      {children}
    </label>
  )
}

export const inputStyle: React.CSSProperties = {
  padding: '7px 10px', border: '1px solid #d0d3d9', borderRadius: 6, fontSize: 13, width: '100%',
  boxSizing: 'border-box',
}

export function Button({ children, onClick, kind = 'primary', disabled, style }: {
  children: ReactNode; onClick?: () => void; kind?: 'primary' | 'ghost' | 'danger'; disabled?: boolean; style?: React.CSSProperties
}) {
  const colors = {
    primary: { background: '#2563eb', color: '#fff', border: '1px solid #2563eb' },
    ghost: { background: '#fff', color: '#333', border: '1px solid #d0d3d9' },
    danger: { background: '#dc2626', color: '#fff', border: '1px solid #dc2626' },
  }[kind]
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{ padding: '7px 14px', borderRadius: 6, fontSize: 13, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1, ...colors, ...style }}
    >
      {children}
    </button>
  )
}

export function Tag({ children, color }: { children: ReactNode; color: string }) {
  return (
    <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: 10, fontSize: 12, background: color + '1a', color, fontWeight: 500 }}>
      {children}
    </span>
  )
}

export function Loading({ text = '加载中...' }: { text?: string }) {
  return <div style={{ color: '#888', padding: 24, textAlign: 'center', fontSize: 13 }}>{text}</div>
}

export function ErrorBox({ message }: { message: string }) {
  return <div style={{ color: '#dc2626', padding: 12, fontSize: 13, border: '1px solid #fecaca', background: '#fef2f2', borderRadius: 8, margin: '8px 0' }}>{message}</div>
}

// 信号类型 -> 标签颜色/文案
export const SIGNAL_META: Record<string, { label: string; color: string }> = {
  BUY_FIRST: { label: '首仓', color: '#dc2626' },
  BUY_ADD: { label: '加仓', color: '#ea580c' },
  SELL_REDUCE: { label: '减仓', color: '#16a34a' },
  SELL_STOP: { label: '止损', color: '#111827' },
  T_BUY: { label: '做T买', color: '#db2777' },
  T_SELL: { label: '做T卖', color: '#0891b2' },
}
