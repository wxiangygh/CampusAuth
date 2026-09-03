import { createApp } from 'vue'
import App from './App.vue'
import ConfigViewer from './views/ConfigViewer.vue'
import './styles/global.css'
import { store, handleAuthProgress, handleAppState } from './store'

// ===== Python evaluate_js 全局入口（契约保持，勿改名）=====
// 悬浮窗（#viewer）是独立窗口，不注册主窗口专用的全局入口
const isViewer = window.location.hash.startsWith('#viewer')

if (!isViewer) {
  window.onAuthProgress = handleAuthProgress
  window.onAppState = handleAppState
  window.switchTab = (name) => {
    if (['home', 'workflow', 'warp', 'traffic', 'settings'].includes(name)) store.activeTab = name
  }
}

const app = createApp(isViewer ? ConfigViewer : App)
app.mount('#app')
