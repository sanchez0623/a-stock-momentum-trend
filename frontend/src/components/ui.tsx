// 共享 UI 基础组件(基于 Ant Design 6, API 与旧版自建组件兼容)
import type { ReactNode } from 'react'
import { Alert, Button as AntdButton, Card as AntdCard, message as antdMessage, Spin, Tag as AntdTag } from 'antd'

// 卡片: antd 圆角 + 浅边框 + 悬浮阴影, 现代化观感
export function Card({ title, children, style, extra }: {
  title?: ReactNode; children: ReactNode; style?: React.CSSProperties; extra?: ReactNode
}) {
  return (
    <AntdCard
      title={title !== undefined ? title : undefined}
      extra={extra}
      style={{ borderRadius: 10, ...style }}
      styles={{ header: { fontWeight: 600, fontSize: 14 } }}
    >
      {children}
    </AntdCard>
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
  padding: '6px 10px', border: '1px solid #d9d9d9', borderRadius: 6, fontSize: 13, width: '100%',
  boxSizing: 'border-box', outline: 'none', transition: 'border-color 0.2s',
}

// 按钮: antd 自带 hover/active 动效与焦点态
const KIND_MAP: Record<string, 'primary' | 'default' | 'dashed'> = {
  primary: 'primary',
  ghost: 'default',
  danger: 'primary',   // v6 无 danger type, 由 danger 属性表达
  default: 'default',
  dashed: 'dashed',
}

export function Button({ children, onClick, kind = 'primary', disabled, style, type, danger }: {
  children: ReactNode
  onClick?: () => void
  kind?: 'primary' | 'ghost' | 'danger' | 'default' | 'dashed'
  disabled?: boolean
  style?: React.CSSProperties
  type?: 'button' | 'submit'
  danger?: boolean
}) {
  return (
    <AntdButton
      type={KIND_MAP[kind] ?? 'primary'}
      danger={danger || kind === 'danger'}
      onClick={onClick}
      disabled={disabled}
      style={{ fontSize: 13, ...style }}
      htmlType={type}
    >
      {children}
    </AntdButton>
  )
}

// 标签: antd 支持自定义色
export function Tag({ children, color }: { children: ReactNode; color: string }) {
  return <AntdTag color={color} style={{ borderRadius: 4, marginInlineEnd: 0 }}>{children}</AntdTag>
}

export function Loading({ text = '加载中...' }: { text?: string }) {
  return (
    <div style={{ padding: 48, textAlign: 'center', color: '#999' }}>
      <Spin />
      <div style={{ marginTop: 8, fontSize: 13 }}>{text}</div>
    </div>
  )
}

export function ErrorBox({ message }: { message: string }) {
  return <Alert type="error" message={message} showIcon style={{ marginBottom: 12, borderRadius: 8 }} />
}

// 全局弹窗提示(替代仅页面顶部的弱错误提示)
export const toast = {
  success: (m: string) => antdMessage.success(m),
  error: (m: string) => antdMessage.error(m),
  info: (m: string) => antdMessage.info(m),
  warning: (m: string) => antdMessage.warning(m),
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
