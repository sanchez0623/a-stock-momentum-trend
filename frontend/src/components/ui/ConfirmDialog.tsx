import { createPortal } from 'react-dom'
import { motion } from 'framer-motion'
import { Button } from './Button'

// 通用删除/危险操作二次确认框(项目规则: 删除操作必须二次确认)
// 用法: 触发删除时先 setState 打开, 确认后执行删除逻辑
export function ConfirmDialog({ title, message, confirmText = '删除', busy, onConfirm, onCancel }: {
  title: string
  message: string
  confirmText?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={busy ? undefined : onCancel}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.15 }}
        className="w-full max-w-sm rounded-lg border border-line bg-white p-4 shadow-cardHover"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 text-[15px] font-semibold text-ink">{title}</div>
        <p className="mb-4 text-[13px] leading-relaxed text-ink-secondary">{message}</p>
        <div className="flex justify-end gap-2">
          <Button kind="ghost" onClick={onCancel}>取消</Button>
          <Button kind="danger" onClick={onConfirm} disabled={busy}>{busy ? '处理中...' : confirmText}</Button>
        </div>
      </motion.div>
    </div>,
    document.body,
  )
}
