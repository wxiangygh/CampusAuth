import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base: './' 保证 dist 以相对路径引用资源，
// 可直接通过 file:/// 协议被 pywebview 加载（校园网认证前无网，禁止任何 CDN 依赖）
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500
  }
})
