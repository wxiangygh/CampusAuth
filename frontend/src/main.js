import { createApp } from 'vue'
import App from './App.vue'
import ConfigViewer from './views/ConfigViewer.vue'
import './styles/global.css'
import { store, handleAuthProgress, handleAppState, handleWifiScanUpdate, handleToast } from './store'

// ===== Python evaluate_js 全局入口（契约保持，勿改名）=====
// 悬浮窗（#viewer）是独立窗口，不注册主窗口专用的全局入口
const isViewer = window.location.hash.startsWith('#viewer')

if (!isViewer) {
  window.onAuthProgress = handleAuthProgress
  window.onAppState = handleAppState
  window.onWifiScanUpdate = handleWifiScanUpdate
  window.onToast = handleToast
  window.switchTab = (name) => {
    if (['home', 'workflow', 'warp', 'traffic', 'settings'].includes(name)) store.activeTab = name
  }
}

// ===== 启动错误浮层：白屏时给出可见线索（仅 dev server 下显示） =====
function surfaceBootError(kind, message) {
  console.error('[boot]', kind, message)
  if (!location.protocol.startsWith('http')) return
  let el = document.getElementById('boot-error')
  if (!el) {
    el = document.createElement('pre')
    el.id = 'boot-error'
    el.style.cssText = 'position:fixed;inset:auto 12px 12px 12px;max-height:45vh;overflow:auto;'
      + 'z-index:99999;background:#2a1518;color:#ff8f8f;font-size:12px;padding:10px;'
      + 'border-radius:8px;white-space:pre-wrap;'
    document.body.appendChild(el)
  }
  el.textContent += `[${kind}] ${message}\n`
}

window.addEventListener('error', (e) => {
  surfaceBootError('error', e.message || String(e.error || 'unknown'))
})
window.addEventListener('unhandledrejection', (e) => {
  surfaceBootError('unhandledrejection', String(e.reason?.stack || e.reason || 'unknown'))
})

const app = createApp(isViewer ? ConfigViewer : App)
app.config.errorHandler = (err, _instance, info) => {
  surfaceBootError('vue', `${err?.message || err} (${info})`)
}
app.mount('#app')
