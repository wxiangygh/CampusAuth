import { createApp } from 'vue'
import App from './App.vue'
import './styles/global.css'
import { store, handleAuthProgress, handleAppState } from './store'

// ===== Python evaluate_js 全局入口（契约保持，勿改名）=====
window.onAuthProgress = handleAuthProgress
window.onAppState = handleAppState
window.switchTab = (name) => {
  if (['home', 'workflow', 'warp', 'traffic', 'settings'].includes(name)) store.activeTab = name
}

const app = createApp(App)
app.mount('#app')
