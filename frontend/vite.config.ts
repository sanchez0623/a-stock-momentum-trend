import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 端口约定: 前端开发 5175(2026-08-07 调整, 避免与短线波段系统 5173 冲突); /api 与 /ws 代理到后端 8002
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5175,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8002', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8002', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 800,
    // 临时: WorkBuddy 安全删除保护异常导致 vite 清空 dist(emptyDir→rmSync)失败, 跳过清空直接覆盖写
    emptyOutDir: false,
  },
})
