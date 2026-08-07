// 代码输入框: 输入股票代码后自动查询并带出名称(失焦/回车/300ms 防抖)
import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { inputStyle } from './ui'

interface Props {
  value: string
  onChange: (v: string) => void
  onNameFound: (name: string) => void
  placeholder?: string
  style?: React.CSSProperties
}

export default function SymbolInput({ value, onChange, onNameFound, placeholder, style }: Props) {
  const [hint, setHint] = useState('')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const lookup = (symbol: string) => {
    const sym = symbol.trim()
    if (!sym) return
    setHint('查询中...')
    api
      .quote(sym)
      .then((q) => {
        if (q.name) {
          onNameFound(q.name)
          setHint('')
        } else {
          setHint('未找到')
        }
      })
      .catch(() => setHint('未找到'))
  }

  const handleChange = (v: string) => {
    onChange(v)
    // 防抖: 停止输入 300ms 后自动查询
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => lookup(v), 300)
  }

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])

  return (
    <div style={{ position: 'relative' }}>
      <input
        style={style || inputStyle}
        value={value}
        onChange={(e) => handleChange(e.target.value)}
        onBlur={(e) => lookup(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') lookup((e.target as HTMLInputElement).value)
        }}
        placeholder={placeholder || '如 300750'}
      />
      {hint && <span style={{ position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)', fontSize: 11, color: hint === '未找到' ? '#dc2626' : '#888' }}>{hint}</span>}
    </div>
  )
}
