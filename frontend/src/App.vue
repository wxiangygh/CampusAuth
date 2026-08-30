<script setup>
import { onMounted, onBeforeUnmount, watch } from 'vue'
import { NConfigProvider } from 'naive-ui'
import { naiveTheme, themeOverrides, setThemeMode, isDark, themeMode } from './theme'
import { store } from './store'
import { waitForApi, api as bridgeApi } from './bridge'
import { ui } from './ui'
import TitleBar from './components/TitleBar.vue'
import SideNav from './components/SideNav.vue'
import StatusBar from './components/StatusBar.vue'
import HomeView from './views/HomeView.vue'
import WorkflowView from './views/WorkflowView.vue'
import WarpView from './views/WarpView.vue'
import TrafficView from './views/TrafficView.vue'
import SettingsView from './views/SettingsView.vue'

// 最小尺寸：MIN_W/MIN_H 为 CSS 像素（设计值）；resize_move_window/SetWindowPos
// 使用物理像素，原生 MinimumSize = 逻辑最小值 × DPI，因此钳制值需乘 devicePixelRatio
// 对齐，否则拖到最小窗口时 JS 与原生限制互相拉扯、窗口尺寸来回跳动
const MIN_W = 1210
const MIN_H = 770
const MIN_W_PX = Math.round(MIN_W * (window.devicePixelRatio || 1))
const MIN_H_PX = Math.round(MIN_H * (window.devicePixelRatio || 1))

// 持久化当前激活的视图
watch(
  () => store.activeTab,
  (name) => {
    if (['home', 'workflow', 'warp', 'traffic', 'settings'].includes(name)) {
      bridgeApi()?.save_ui_prefs({ active_tab: name })?.catch(() => {})
    }
  }
)

// ===== frameless 窗口拖拽缩放（JS mousemove 驱动）=====
let resizeState = null
// 拖拽会话号：get_window_rect 是异步的，用于丢弃迟到响应；
// 每次 mouseup / 异常收尾都会递增，旧会话的异步回调不再生效
let dragSession = 0
let geometryTimer = null
let lastGeometrySignature = ''

function onHandleMouseDown(e, dir) {
  e.preventDefault()
  e.stopPropagation()
  const a = bridgeApi()
  if (!a || !a.get_window_rect) return
  const session = ++dragSession
  a.get_window_rect().then((rect) => {
    if (session !== dragSession) return // 期间拖拽已结束/开启新会话，丢弃
    resizeState = {
      dir,
      startScreenX: e.screenX,
      startScreenY: e.screenY,
      winX: rect.x,
      winY: rect.y,
      winW: rect.width,
      winH: rect.height,
    }
  })
}

function onMouseMove(e) {
  if (!resizeState) return
  // 拖拽缩放时鼠标常被移出窗口边界，window 外的 mouseup 收不到，
  // resizeState 会泄漏：之后任意 mousemove 都会按旧锚点改窗口大小，
  // 表现为"调整好的窗口尺寸莫名其妙跳回拖拽前"。左键已释放即收尾。
  if (!(e.buttons & 1)) {
    resizeState = null
    dragSession++
    persistWindowGeometry()
    return
  }
  const s = resizeState
  const dx = e.screenX - s.startScreenX
  const dy = e.screenY - s.startScreenY
  let newX = s.winX
  let newY = s.winY
  let newW = s.winW
  let newH = s.winH

  if (s.dir.indexOf('left') !== -1) {
    newX = s.winX + dx
    newW = s.winW - dx
  }
  if (s.dir.indexOf('right') !== -1) newW = s.winW + dx
  if (s.dir.indexOf('top') !== -1) {
    newY = s.winY + dy
    newH = s.winH - dy
  }
  if (s.dir.indexOf('bottom') !== -1) newH = s.winH + dy

  if (newW < MIN_W_PX) {
    if (s.dir.indexOf('left') !== -1) newX = s.winX + (s.winW - MIN_W_PX)
    newW = MIN_W_PX
  }
  if (newH < MIN_H_PX) {
    if (s.dir.indexOf('top') !== -1) newY = s.winY + (s.winH - MIN_H_PX)
    newH = MIN_H_PX
  }

  bridgeApi()?.resize_move_window(newW, newH, newX, newY)
}

async function persistWindowGeometry() {
  const a = bridgeApi()
  if (!a?.get_window_rect || !a?.save_window_geometry) return
  try {
    const rect = await a.get_window_rect()
    const signature = JSON.stringify(rect)
    if (signature === lastGeometrySignature) return
    const result = await a.save_window_geometry(rect)
    if (result?.success !== false) lastGeometrySignature = signature
  } catch (e) {
    console.warn('persistWindowGeometry failed:', e)
  }
}

function onMouseUp() {
  dragSession++
  resizeState = null
  // 拖拽结束立即持久化窗口几何（"调整完及时保存"）
  persistWindowGeometry()
}

onMounted(async () => {
  // 防御：挂载后依据存储重新校准主题状态，覆盖存储延迟恢复等异常时序导致的初始化偏差
  try {
    const stored = localStorage.getItem('cauth-theme')
    if (stored && ['light', 'dark', 'system'].includes(stored) && stored !== themeMode.value) {
      setThemeMode(stored)
    }
  } catch (e) {
    /* ignore */
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
  window.addEventListener('blur', () => {
    // 失焦时结束可能残留的拖拽会话并持久化当前几何
    dragSession++
    resizeState = null
    persistWindowGeometry()
  })
  geometryTimer = setInterval(persistWindowGeometry, 2000)

  // dev server 预览（http 协议）无 pywebview，跳过等待直接渲染 UI
  const isDevServer = location.protocol.startsWith('http')
  if (isDevServer) {
    store.initDone = true
    return
  }
  try {
    await waitForApi(15000)
    // 先加载 UI 偏好，再置 apiReady，避免视图初始化时读到默认值
    try {
      const prefs = await bridgeApi().get_ui_prefs()
      if (prefs.page_size) store.pageSize = prefs.page_size
      if (['list', 'canvas'].includes(prefs.traffic_subview)) store.trafficSubview = prefs.traffic_subview
      store.detailUserCollapsed = !!prefs.network_detail_collapsed
      if (['home', 'workflow', 'warp', 'traffic', 'settings'].includes(prefs.active_tab)) {
        store.activeTab = prefs.active_tab
      }
      if (['light', 'dark', 'system'].includes(prefs.theme)) setThemeMode(prefs.theme)
    } catch (e) {
      console.warn('get_ui_prefs failed:', e)
    }
    store.apiReady = true
    store.initDone = true
  } catch (e) {
    store.initError = e.message
    ui.toast('API 加载超时: ' + e.message, 'error')
    store.initDone = true
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  if (geometryTimer) clearInterval(geometryTimer)
})
</script>

<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
    <div class="app-shell" :data-theme="isDark ? 'dark' : 'light'">
      <TitleBar />
      <div class="app-body">
        <SideNav />
        <main class="app-content">
          <HomeView v-show="store.activeTab === 'home'" />
          <WorkflowView v-show="store.activeTab === 'workflow'" />
          <WarpView v-show="store.activeTab === 'warp'" />
          <TrafficView v-show="store.activeTab === 'traffic'" />
          <SettingsView v-show="store.activeTab === 'settings'" />
        </main>
      </div>
      <StatusBar />

      <!-- frameless 边缘缩放手柄 -->
      <div class="resize-handle top" data-dir="top" @mousedown="onHandleMouseDown($event, 'top')"></div>
      <div class="resize-handle bottom" data-dir="bottom" @mousedown="onHandleMouseDown($event, 'bottom')"></div>
      <div class="resize-handle left" data-dir="left" @mousedown="onHandleMouseDown($event, 'left')"></div>
      <div class="resize-handle right" data-dir="right" @mousedown="onHandleMouseDown($event, 'right')"></div>
      <div class="resize-handle tl" @mousedown="onHandleMouseDown($event, 'top left')"></div>
      <div class="resize-handle tr" @mousedown="onHandleMouseDown($event, 'top right')"></div>
      <div class="resize-handle bl" @mousedown="onHandleMouseDown($event, 'bottom left')"></div>
      <div class="resize-handle br" @mousedown="onHandleMouseDown($event, 'bottom right')"></div>

      <!-- 全屏操作遮罩 -->
      <div class="op-mask" v-if="ui.loadingState.active">
        <div class="op-mask-inner">
          <div class="op-mask-spinner"></div>
          <div class="op-mask-text">{{ ui.loadingState.text }}</div>
        </div>
      </div>

      <!-- 启动遮罩 -->
      <div class="boot-overlay" v-if="!store.initDone">
        <div class="boot-spinner"></div>
        <div class="boot-text">CAuth 启动中...</div>
      </div>
    </div>
  </n-config-provider>
</template>

<style scoped>
.app-shell {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-base);
  /* 文本颜色继承入口：body 在 .app-shell 之外，无法解析应用内主题变量 */
  color: var(--text-primary);
}

.app-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.app-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
</style>
