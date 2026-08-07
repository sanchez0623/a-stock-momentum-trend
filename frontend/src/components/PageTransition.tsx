import { motion } from 'framer-motion'
import type { ReactNode } from 'react'

// 页面切换过渡: 淡入 + 轻微上移(路由切换时包裹 Suspense 内的页面)
export function PageTransition({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.2, ease: 'easeOut' }}
    >
      {children}
    </motion.div>
  )
}
