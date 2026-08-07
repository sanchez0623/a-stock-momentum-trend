// 代码输入框: 输入股票代码后自动查询并带出名称(失焦/回车/300ms 防抖)
// - 全角数字自动转半角(输入法常见)
// - 查询失败自动重试一次
// - 区分"未找到"(无效代码)与"查询失败"(网络/源不可用), 均提示可手动填写名称
import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { inputStyle } from './ui'

interface Props {
  value: string
  onChange: (v: string) => void
  onNameFound: (name: string) => void
  onEnter?: () => void
  placeholder?: string
  style?: React.CSSProperties
}

function normalize(s: string): string {
  // 全角数字/字母转半角, 去空格
  return s
    .trim()
    .replace(/[\uff10-\uff19]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
    .replace(/[\uff21-\uff3a\uff41-\uff5a]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
    .toUpperCase()
}

export default function SymbolInput({ value, onChange, onNameFound, onEnter, placeholder, style }: Props) {
  const [hint, setHint] = useState<{ text: string; error: boolean } | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 多代码(含逗号/空格分隔)时不做名称查询, 名称由批量分析结果返回
  const isMulti = (raw: string) => /[,，\s]/.test(raw)

  const lookup = (raw: string, retried = false) => {
    const sym = normalize(raw)
    if (!sym || isMulti(raw)) return
    setHint({ text: '查询中...', error: false })
    api
      .quote(sym)
      .then((q) => {
        if (q.name) {
          onNameFound(q.name)
          setHint(null)
        } else {
          setHint({ text: '未找到,可手动填写名称', error: true })
        }
      })
      .catch(() => {
        if (!retried) {
          // 800ms 后重试一次(瞬时网络/源切换)
          timerRef.current = setTimeout(() => lookup(raw, true), 800)
          setHint({ text: '查询中...', error: false })
        } else {
          setHint({ text: '查询失败,可手动填写名称', error: true })
        }
      })
  }

  const handleChange = (raw: string) => {
    const v = normalize(raw)
    onChange(v)
    if (timerRef.current) clearTimeout(timerRef.current)
    if (!isMulti(v)) timerRef.current = setTimeout(() => lookup(v), 300)
  }

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])

  return (
    <div className="relative">
      <input
        style={style || inputStyle}
        value={value}
        onChange={(e) => handleChange(e.target.value)}
        onBlur={(e) => lookup(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            const raw = (e.target as HTMLInputElement).value
            if (isMulti(raw)) {
              // 多代码回车: 不查名称, 直接交给父组件评估
              onEnter?.()
            } else {
              lookup(raw)
            }
          }
        }}
        placeholder={placeholder || '如 300750'}
      />
      {hint && (
        <span
          className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] whitespace-nowrap"
          style={{ color: hint.error ? '#dc2626' : '#888' }}
        >
          {hint.text}
        </span>
      )}
    </div>
  )
}
