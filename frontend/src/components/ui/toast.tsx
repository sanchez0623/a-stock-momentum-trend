import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, AlertCircle, Info, AlertTriangle, X } from 'lucide-react'
import { cn } from './utils'

type ToastType = 'success' | 'error' | 'info' | 'warning'

interface ToastItem {
  id: number
  type: ToastType
  message: string
}

const TOAST_META: Record<ToastType, { icon: typeof Info; color: string; ring: string }> = {
  success: { icon: CheckCircle2, color: 'text-fall', ring: 'border-fall/20' },
  error: { icon: AlertCircle, color: 'text-rise', ring: 'border-rise/20' },
  info: { icon: Info, color: 'text-link', ring: 'border-link/20' },
  warning: { icon: AlertTriangle, color: 'text-orange-500', ring: 'border-orange-500/20' },
}

let toastSeq = 0
const toastListeners = new Set<(t: ToastItem) => void>()

function pushToast(type: ToastType, message: string) {
  const item: ToastItem = { id: ++toastSeq, type, message }
  toastListeners.forEach((l) => l(item))
}

export const toast = {
  success: (m: string) => pushToast('success', m),
  error: (m: string) => pushToast('error', m),
  info: (m: string) => pushToast('info', m),
  warning: (m: string) => pushToast('warning', m),
}

function ToastCard({ item, onDone }: { item: ToastItem; onDone: (id: number) => void }) {
  const meta = TOAST_META[item.type]
  const Icon = meta.icon
  useEffect(() => {
    const timer = setTimeout(() => onDone(item.id), 3000)
    return () => clearTimeout(timer)
  }, [item.id, onDone])
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: -14, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -10, scale: 0.96 }}
      transition={{ duration: 0.18 }}
      className={cn('pointer-events-auto flex w-80 items-start gap-2 rounded-lg border bg-white px-3 py-2.5 shadow-cardHover', meta.ring)}
    >
      <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', meta.color)} />
      <span className="flex-1 break-all text-[13px] leading-snug text-ink">{item.message}</span>
      <button
        onClick={() => onDone(item.id)}
        className="shrink-0 text-ink-faint transition-colors hover:text-ink"
        aria-label="关闭"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </motion.div>
  )
}

// Toast 挂载点: 在 App 根部渲染一次
export function ToastHost() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  useEffect(() => {
    const listener = (t: ToastItem) => setToasts((prev) => [...prev, t])
    toastListeners.add(listener)
    return () => { toastListeners.delete(listener) }
  }, [])
  const remove = (id: number) => setToasts((prev) => prev.filter((t) => t.id !== id))
  return createPortal(
    <div className="pointer-events-none fixed left-1/2 top-4 z-50 flex w-full max-w-md -translate-x-1/2 flex-col items-center gap-2 px-4">
      <AnimatePresence mode="popLayout">
        {toasts.map((t) => <ToastCard key={t.id} item={t} onDone={remove} />)}
      </AnimatePresence>
    </div>,
    document.body,
  )
}
