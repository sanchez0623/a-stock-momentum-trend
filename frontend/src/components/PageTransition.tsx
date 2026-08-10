import { useEffect } from 'react'
import { motion, usePresence } from 'framer-motion'
import type { ReactNode } from 'react'

// 页面切换过渡: 淡入 + 轻微上移(路由切换时包裹 Suspense 内的页面)
// framer-motion v13 下 AnimatePresence mode="wait" 无法自动感知自定义组件的退出动画,
// 用 usePresence 显式控制卸载: 退出动画(200ms)结束后再 safeToRemove,
// 否则新路由页面永不挂载(全局路由跳转失效)
export function PageTransition({ children }: { children: ReactNode }) {
  const [isPresent, safeToRemove] = usePresence()
  useEffect(() => {
    if (!isPresent) {
      const t = setTimeout(safeToRemove, 200)
      return () => clearTimeout(t)
    }
  }, [isPresent, safeToRemove])
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
