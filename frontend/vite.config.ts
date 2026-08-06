import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 端口约定: 前端开发 5173(不改); /api 与 /ws 代理到后端 8002
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8002', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8002', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 800,
  },
})
