<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { NButton } from 'naive-ui'
import { store, startAuth, startRestore, cancelOperation, updateStatusFromCheck, refreshNetworkDetail, setParticleCanvas } from '../store'
import { api } from '../bridge'

const STATUS_ICONS = {
  wifi: '<path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.56 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  cross: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  loader: '<line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/>',
  warn: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
}

const particleRef = ref(null)

// ===== 网络详情折叠 =====
function toggleDetail() {
  store.detailCollapsed = !store.detailCollapsed
  store.detailUserCollapsed = store.detailCollapsed
  try {
    api()?.save_ui_prefs({ network_detail_collapsed: store.detailCollapsed })?.catch(() => {})
  } catch (e) {
    console.warn('save_ui_prefs failed:', e)
  }
}

const DETAIL_ITEMS = [
  { key: 'ipv4', label: 'IPv4', get: (d) => d.ipv4 || '—', cls: (d) => (d.ipv4 ? '' : 'empty') },
  { key: 'ipv6', label: 'IPv6', get: (d) => d.ipv6 || '无公网IPv6', cls: (d) => (d.ipv6 ? 'success' : 'warning') },
  { key: 'mac', label: 'MAC', get: (d) => d.mac || '—', cls: (d) => (d.mac ? '' : 'empty') },
  { key: 'wifi', label: 'WiFi', get: (d) => d.wifi_ssid || '未连接', cls: (d) => (d.wifi_ssid ? '' : 'warning') },
  { key: 'iface', label: '接口', get: (d) => d.interface || '未知', cls: () => '' },
  { key: 'warp', label: 'WARP', get: (d) => (d.warp_connected ? '已连接' : '未连接'), cls: (d) => (d.warp_connected ? 'success' : 'warning') },
]

// ===== 初始化与轮询 =====
async function initStatus() {
  try {
    const status = await api().check_network_status()
    updateStatusFromCheck(status)
  } catch (e) {
    console.error('Failed to check network status:', e)
  }
}

let statusCheckTimer = null

function startStatusPolling() {
  stopStatusPolling()
  statusCheckTimer = setInterval(() => {
    if (!store.authRunning && api()) {
      api()
        .check_network_status()
        .then((s) => {
          if (!store.authRunning) updateStatusFromCheck(s)
        })
        .catch(() => {})
    }
  }, 30000)
}

function stopStatusPolling() {
  if (statusCheckTimer) {
    clearInterval(statusCheckTimer)
    statusCheckTimer = null
  }
}

// 切回主页：刷新状态与详情；离开主页停止轮询
watch(
  () => store.activeTab,
  (name) => {
    if (name === 'home') {
      refreshNetworkDetail()
      if (api() && !store.authRunning) {
        api()
          .check_network_status()
          .then((s) => {
            if (!store.authRunning) updateStatusFromCheck(s)
          })
          .catch(() => {})
      }
      startStatusPolling()
    } else {
      stopStatusPolling()
    }
  }
)

watch(
  () => store.apiReady,
  async (ready) => {
    if (!ready) return
    initStatus()
    refreshNetworkDetail()
    startStatusPolling()
  },
  { immediate: true }
)

onMounted(() => {
  if (particleRef.value) setParticleCanvas(particleRef.value)
})

onBeforeUnmount(() => {
  stopStatusPolling()
})
</script>

<template>
  <div class="home-view">
    <!-- 认证状态英雄区 -->
    <section class="hero-card card">
      <canvas ref="particleRef" class="particle-canvas" width="300" height="300"></canvas>

      <div class="status-indicator" :class="store.status.state">
        <svg class="hexagon-svg" viewBox="0 0 140 140">
          <polygon class="hexagon-shape" points="70,5 125,35 125,105 70,135 15,105 15,35" />
        </svg>
        <svg class="hexagon-rotate" viewBox="0 0 156 156">
          <polygon class="hexagon-rotate-line" points="78,5 141,39 141,117 78,151 15,117 15,39" />
        </svg>
        <div class="status-icon">
          <!-- :key 让状态切换时重建 SVG，确保对勾划出动画每次进入 success 都重播 -->
          <svg :key="store.status.state + store.status.icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            v-html="STATUS_ICONS[store.status.icon] || STATUS_ICONS.wifi"></svg>
        </div>
      </div>

      <div class="status-label">STATUS</div>
      <div class="status-title">{{ store.status.title }}</div>
      <div class="status-subtitle">{{ store.status.subtitle }}</div>

      <div class="progress-container" :class="{ active: store.progress.visible }">
        <div class="progress-track">
          <div class="progress-fill" :style="{ width: store.progress.pct + '%' }"></div>
        </div>
        <div class="progress-info">
          <div class="progress-label">{{ store.progress.label }}</div>
          <div class="progress-text">{{ Math.round(store.progress.pct) }}%</div>
        </div>
      </div>

      <div class="status-actions">
        <n-button type="primary" size="large" :disabled="store.authDisabled" @click="startAuth">开始认证</n-button>
        <n-button size="large" :disabled="store.restoreDisabled" @click="startRestore">恢复网络</n-button>
        <n-button size="large" type="error" secondary v-if="store.authRunning" @click="cancelOperation">取消</n-button>
      </div>
    </section>

    <!-- 网络详情（可折叠） -->
    <section class="card detail-card" :class="{ collapsed: store.detailCollapsed }">
      <div class="detail-header" @click="toggleDetail">
        <span class="detail-title">网络详情</span>
        <span class="detail-arrow" :class="{ collapsed: store.detailCollapsed }">▾</span>
      </div>
      <div class="detail-body">
        <div class="detail-grid" v-if="store.detail">
          <div class="detail-item" v-for="item in DETAIL_ITEMS" :key="item.key">
            <div class="detail-label">{{ item.label }}</div>
            <div class="detail-value" :class="item.cls(store.detail)">{{ item.get(store.detail) }}</div>
          </div>
        </div>
        <div v-else class="detail-loading">请稍后...</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.home-view {
  padding: 20px 22px 30px;
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ===== 状态英雄区 ===== */
.hero-card {
  position: relative;
  padding: 34px 20px 30px;
  display: flex;
  flex-direction: column;
  align-items: center;
  overflow: hidden;
}

.particle-canvas {
  position: absolute;
  left: 50%;
  top: 20px;
  transform: translateX(-50%);
  width: 300px;
  height: 300px;
  pointer-events: none;
}

.status-indicator {
  position: relative;
  width: 140px;
  height: 140px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.hexagon-svg {
  position: absolute;
  inset: 0;
}

.hexagon-shape {
  fill: var(--bg-elevated);
  stroke: var(--border-strong);
  stroke-width: 1.5;
  transition: all 0.4s;
}

.hexagon-rotate {
  position: absolute;
  inset: -8px;
  animation: hexspin 14s linear infinite;
}

.hexagon-rotate-line {
  fill: none;
  stroke: var(--border);
  stroke-width: 1;
  stroke-dasharray: 30 14;
}

@keyframes hexspin {
  to {
    transform: rotate(360deg);
  }
}

.status-icon {
  /* relative + z-index：六边形两层 svg 是绝对定位且有不透明填充，
     按 CSS 绘制顺序会盖住普通文档流的图标，导致状态图标永远不可见 */
  position: relative;
  z-index: 1;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-tertiary);
  transition: color 0.4s;
}

.status-icon svg {
  width: 40px;
  height: 40px;
}

/* 状态配色 */
.status-indicator.running .hexagon-shape {
  stroke: var(--accent);
}

.status-indicator.running .hexagon-rotate-line {
  stroke: var(--accent);
}

.status-indicator.running .status-icon {
  color: var(--accent);
  animation: pulse 1.2s ease-in-out infinite;
}

.status-indicator.success .hexagon-shape,
.status-indicator.normal .hexagon-shape {
  stroke: var(--success);
}

.status-indicator.success .hexagon-rotate-line,
.status-indicator.normal .hexagon-rotate-line {
  stroke: var(--success);
  opacity: 0.5;
}

.status-indicator.success .status-icon,
.status-indicator.normal .status-icon {
  color: var(--success);
}

/* 认证成功（IPv4 禁用 + WARP 连接）时的醒目对勾：
   放大加粗 + 划出动画（结束后常驻显示），与 IPv4 启用状态的感叹号显眼度对齐 */
.status-indicator.success .status-icon {
  /* 容器随放大图标扩容，防止 svg 被 flex 压扁变形 */
  width: 56px;
  height: 56px;
}

.status-indicator.success .status-icon svg {
  width: 56px;
  height: 56px;
  flex-shrink: 0;
  stroke-width: 3.4;
}

/* v-html 注入的 polyline 无 scoped 属性，需 :deep() 穿透才能命中。
   基态 dashoffset: 0 保证对勾始终可见（动画未触发/被禁用时兜底），
   动画用 backwards 填充：延迟期间取 from 值(26 隐藏)，播完后回落到基态(0 可见) */
.status-indicator.success .status-icon svg :deep(polyline) {
  /* 对勾路径 (20,6→9,17→4,12) 弧长约 23，dasharray 略大确保完整覆盖 */
  stroke-dasharray: 26;
  stroke-dashoffset: 0;
  animation: check-draw 0.5s cubic-bezier(0.3, 1, 0.4, 1) 0.15s backwards;
}

@keyframes check-draw {
  from {
    stroke-dashoffset: 26;
  }

  to {
    stroke-dashoffset: 0;
  }
}

/* 恢复网络后（normal 模式）的对勾保持原尺寸即可，不做划出动画 */

.status-indicator.error .hexagon-shape {
  stroke: var(--error);
}

.status-indicator.error .hexagon-rotate-line {
  stroke: var(--error);
  opacity: 0.5;
}

.status-indicator.error .status-icon {
  color: var(--error);
}

@keyframes pulse {

  0%,
  100% {
    transform: scale(1);
  }

  50% {
    transform: scale(1.08);
  }
}

.status-label {
  margin-top: 18px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 3px;
  color: var(--text-tertiary);
}

.status-title {
  margin-top: 6px;
  font-size: 20px;
  font-weight: 700;
}

.status-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-secondary);
  max-width: 420px;
  text-align: center;
  word-break: break-all;
}

.progress-container {
  width: 100%;
  max-width: 380px;
  margin-top: 18px;
  opacity: 0;
  max-height: 0;
  overflow: hidden;
  transition: all 0.3s ease;
}

.progress-container.active {
  opacity: 1;
  max-height: 60px;
}

.progress-track {
  height: 5px;
  border-radius: 3px;
  background: var(--bg-elevated);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg, var(--accent), var(--accent-hover));
  transition: width 0.4s ease;
}

.progress-info {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.progress-text {
  font-family: var(--font-mono);
}

.status-actions {
  margin-top: 22px;
  display: flex;
  gap: 10px;
}

/* ===== 网络详情 ===== */
.detail-card {
  overflow: hidden;
}

.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
}

.detail-header:hover {
  background: var(--bg-hover);
}

.detail-title {
  font-size: 13px;
  font-weight: 600;
}

.detail-arrow {
  color: var(--text-tertiary);
  font-size: 11px;
  transition: transform 0.25s;
}

.detail-arrow.collapsed {
  transform: rotate(-90deg);
}

.detail-body {
  max-height: 400px;
  transition: max-height 0.3s ease;
  overflow: hidden;
}

.detail-card.collapsed .detail-body {
  max-height: 0;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  padding: 2px 16px 16px;
}

.detail-item {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 13px;
}

.detail-label {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--text-tertiary);
  text-transform: uppercase;
}

.detail-value {
  margin-top: 5px;
  font-size: 13px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  word-break: break-all;
}

.detail-value.success {
  color: var(--success);
}

.detail-value.warning {
  color: var(--warning);
}

.detail-value.empty {
  color: var(--text-tertiary);
}

.detail-loading {
  padding: 0 16px 16px;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
