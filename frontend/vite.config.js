import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base: './' 保证 dist 以相对路径引用资源，
// 可直接通过 file:/// 协议被 pywebview 加载（校园网认证前无网，禁止任何 CDN 依赖）

// dev-only：serve 模式下向 index.html 注入 mock 桥（frontend/mock/bridge.js），
// 浏览器直开即可预览界面；build 不包含该脚本，产物不带任何 mock。
function devMockBridge() {
  return {
    name: 'cauth-dev-mock-bridge',
    apply: 'serve',
    transformIndexHtml(html) {
      return html.replace(
        '</head>',
        '<script type="module">import("/mock/bridge.js").catch(() => {})</script></head>'
      )
    },
  }
}

export default defineConfig({
  plugins: [vue(), devMockBridge()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500
  }
})
