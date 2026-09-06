<script setup>
import { ref, computed, watch, h, onMounted, onBeforeUnmount } from 'vue'
import { NButton, NDropdown } from 'naive-ui'
import { store, startAuth, startRestore, cancelOperation, updateStatusFromCheck, refreshNetworkDetail, setParticleCanvas, doAutoSave } from '../store'
import { api } from '../bridge'
import { ui } from '../ui'

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

// 链路感知的详情项：有线联网时 WiFi 行明确显示"未使用（有线）"而非警告，
// 并新增 连接方式 / 有线网卡 两行；无线时保持原有展示
const detailItems = computed(() => {
  const d = store.detail || {}
  const wired = d.link_type === 'wired'
  return [
    {
      key: 'link', label: '连接方式',
      get: () => (d.link_type ? (wired ? '有线' : '无线') : '未知'),
      cls: () => (d.link_type ? 'success' : 'empty'),
    },
    { key: 'ipv4', label: 'IPv4', get: (x) => x.ipv4 || (x.ipv4_disabled ? '已禁用' : '—'), cls: (x) => (x.ipv4 ? '' : 'empty') },
    { key: 'ipv6', label: 'IPv6', get: (x) => x.ipv6 || '无公网IPv6', cls: (x) => (x.ipv6 ? 'success' : 'warning') },
    { key: 'mac', label: 'MAC', get: (x) => x.mac || '—', cls: (x) => (x.mac ? '' : 'empty') },
    {
      key: 'wifi', label: 'WiFi',
      get: (x) => (wired ? '未使用（有线联网）' : (x.wifi_ssid || '未连接')),
      cls: () => (wired ? 'empty' : (d.wifi_ssid ? '' : 'warning')),
    },
    {
      key: 'wired', label: '有线网卡',
      get: (x) => x.wired_interface || '未连接',
      cls: () => (d.wired_interface ? (wired ? 'success' : '') : 'empty'),
    },
    { key: 'iface', label: '接口', get: (x) => x.interface || '未知', cls: () => '' },
    {
      key: 'warp', label: 'WARP / 免流',
      get: (x) => (x.warp_connected
        ? (x.warp_underlay === 'ipv4' ? '已连接（IPv4底层·未免流）' : '已连接（IPv6底层·免流）')
        : '未连接'),
      cls: (x) => (x.warp_connected ? (x.warp_underlay === 'ipv4' ? 'warning' : 'success') : 'warning'),
    },
  ]
})

// ===== 初始化与轮询 =====
async function initStatus() {
  try {
    const status = await api().check_network_status()
    updateStatusFromCheck(status)
  } catch (e) {
    console.error('Failed to check network status:', e)
  }
}

// ===== 按钮竖直分割下拉：快速切换按钮绑定的工作流 =====
// 按钮主体照常触发认证/恢复；右侧窄条（或右键按钮）只开下拉选择，选择后立即保存绑定。
// 两个下拉互斥：打开一个会关闭另一个；菜单位于按钮下方；选中项用图形对勾标记。
const workflows = ref([])
const authMenu = ref(false)
const restoreMenu = ref(false)

// 图形对勾（SVG）：选中标记，未选中时以透明占位保持各行对齐
const CheckIcon = () =>
  h('svg', {
    viewBox: '0 0 24 24', width: '13px', height: '13px', fill: 'none',
    stroke: 'currentColor', 'stroke-width': '3.4',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }, [h('polyline', { points: '20 6 9 17 4 12' })])

// 下拉箭头（SVG chevron）：分割按钮上的图案下拉标记
const CaretDown = () =>
  h('svg', {
    viewBox: '0 0 24 24', width: '15px', height: '15px', fill: 'none',
    stroke: 'currentColor', 'stroke-width': '2.6',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }, [h('polyline', { points: '6 9 12 15 18 9' })])

function optionIcon(checked) {
  const base = 'width:15px;display:inline-flex;justify-content:center;align-items:center;'
  return () =>
    h('span', { style: checked ? base + 'color:var(--success);' : base + 'opacity:0;' },
      [h(CheckIcon)])
}

function wfOption(w, boundId) {
  return {
    label: (w.name || w.id) + (w.built_in ? '（内置）' : ''),
    key: w.id,
    icon: optionIcon(w.id === boundId),
  }
}

const authWfOptions = computed(() =>
  workflows.value.map((w) => wfOption(w, store.form.auth_button_workflow))
)

const restoreWfOptions = computed(() => [
  {
    label: '内置恢复流程（断开 WARP 并启用 IPv4）',
    key: '',
    icon: optionIcon(store.form.restore_button_workflow === ''),
  },
  ...workflows.value.map((w) => wfOption(w, store.form.restore_button_workflow)),
])

// 互斥展开：打开一个先收起另一个
function toggleAuthMenu() {
  authMenu.value = !authMenu.value
  if (authMenu.value) restoreMenu.value = false
}

function toggleRestoreMenu() {
  restoreMenu.value = !restoreMenu.value
  if (restoreMenu.value) authMenu.value = false
}

function closeAllMenus() {
  authMenu.value = false
  restoreMenu.value = false
}

// 按钮悬停提示：当前绑定的工作流名
const boundAuthName = computed(() =>
  workflows.value.find((w) => w.id === store.form.auth_button_workflow)?.name
  || store.form.auth_button_workflow || 'default_auth')
const boundRestoreName = computed(() => {
  if (!store.form.restore_button_workflow) return '内置恢复流程'
  return workflows.value.find((w) => w.id === store.form.restore_button_workflow)?.name
    || store.form.restore_button_workflow
})

async function loadWorkflows() {
  try {
    const data = await api().list_workflows()
    workflows.value = data.workflows || []
  } catch (e) {
    console.warn('list_workflows failed:', e)
  }
}

async function onSelectAuthWf(key) {
  closeAllMenus()
  if (String(key) === store.form.auth_button_workflow) return
  store.form.auth_button_workflow = String(key)
  doAutoSave()
  const name = workflows.value.find((w) => w.id === key)?.name || key
  ui.toast(`「开始认证」已绑定：${name}`, 'success')
}

async function onSelectRestoreWf(key) {
  closeAllMenus()
  if (String(key) === store.form.restore_button_workflow) return
  store.form.restore_button_workflow = String(key)
  doAutoSave()
  const name = key === '' ? '内置恢复流程'
    : (workflows.value.find((w) => w.id === key)?.name || key)
  ui.toast(`「恢复网络」已绑定：${name}`, 'success')
}

let statusCheckTimer = null

function startStatusPolling() {
  stopStatusPolling()
  statusCheckTimer = setInterval(() => {
    // 资源守卫：窗口隐藏到托盘时状态由后端低频推送兜底，不再主动全量探测
    if (document.hidden) return
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
      loadWorkflows() // 重新拉取工作流列表：绑定可能在设置页/工作流页被修改
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
    loadWorkflows()
    startStatusPolling()
  },
  { immediate: true }
)

// 窗口从托盘恢复可见：立即补一次状态与详情刷新（隐藏期间轮询被守卫暂停）
function onVisibilityChange() {
  if (!document.hidden && store.activeTab === 'home') {
    initStatus()
    refreshNetworkDetail()
  }
}

onMounted(() => {
  if (particleRef.value) setParticleCanvas(particleRef.value)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  stopStatusPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
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
        <!-- 分割按钮（WinUI 风格）：文字按「主体+下拉区」整体宽度居中；
             右侧窄条只负责下拉，主体照常触发；右键按钮同样可弹出工作流列表。
             两个下拉互斥，向下弹出，选中项带图形对勾。 -->
        <div class="split-btn">
          <n-button type="primary" size="large" :disabled="store.authDisabled"
            :title="`开始认证执行：${boundAuthName}（右键可切换绑定的工作流）`" @click="startAuth"
            @contextmenu.prevent="toggleAuthMenu">开始认证</n-button>
          <n-dropdown trigger="manual" size="small" placement="bottom" :show="authMenu"
            :options="authWfOptions" @select="onSelectAuthWf" @clickoutside="closeAllMenus">
            <span class="split-caret split-caret-primary" :class="{ open: authMenu, disabled: store.authDisabled }"
              title="选择「开始认证」执行的工作流" @click.stop.prevent="toggleAuthMenu"><i
                class="caret-glyph"><CaretDown /></i></span>
          </n-dropdown>
        </div>
        <div class="split-btn">
          <n-button size="large"
            :title="`恢复网络执行：${boundRestoreName}（右键可切换绑定的工作流）`" @click="startRestore"
            @contextmenu.prevent="toggleRestoreMenu">恢复网络</n-button>
          <n-dropdown trigger="manual" size="small" placement="bottom" :show="restoreMenu"
            :options="restoreWfOptions" @select="onSelectRestoreWf" @clickoutside="closeAllMenus">
            <span class="split-caret" :class="{ open: restoreMenu }"
              title="选择「恢复网络」执行的工作流" @click.stop.prevent="toggleRestoreMenu"><i
                class="caret-glyph"><CaretDown /></i></span>
          </n-dropdown>
        </div>
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
          <div class="detail-item" v-for="item in detailItems" :key="item.key">
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

/* ===== 分割按钮（借鉴 WinUI SplitButton）=====
   一个整体按钮 + 右缘窄条（下拉热区）的竖直分割：
   - 文字按「主体+下拉区」的整体宽度居中：两侧对称加宽内边距，
     文字既居中又不会压到下拉热区；
   - 分割线是上下留边的 1px 细线，不是通高粗线；
   - 窄条宽度收窄到 21px，动画只旋转箭头图标本身。 */
.split-btn {
  position: relative;
  display: inline-flex;
}

.split-btn :deep(.n-button) {
  padding-left: 30px;
  padding-right: 30px;
}

.split-caret {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 21px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
  border-radius: 0 6px 6px 0;
  transition: background-color 0.2s ease, color 0.2s ease;
}

/* 分割线：上下各留 9px 的细竖线，随窄条文字色淡化 */
.split-caret::before {
  content: '';
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 1px;
  background: var(--border-strong);
}

.split-caret:hover {
  background-color: color-mix(in srgb, currentColor 8%, transparent);
}

.split-caret.disabled {
  color: var(--text-tertiary);
}

/* 动画只作用于箭头图标，窄条与分割线保持不动 */
.caret-glyph {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.2s ease;
}

.split-caret.open .caret-glyph {
  transform: rotate(180deg);
}

/* 主按钮（开始认证）上的窄条：颜色与主按钮文字一致。
   浅色主题主按钮是深底白字；深色主题主按钮是白底黑字（见 theme.js overrides） */
.split-caret-primary {
  color: #ffffff;
}

.split-caret-primary::before {
  background: rgba(255, 255, 255, 0.4);
}

:global([data-theme='dark']) .split-caret-primary {
  color: #0d0d0d;
}

:global([data-theme='dark']) .split-caret-primary::before {
  background: rgba(0, 0, 0, 0.2);
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

/* 网格：flex 居中排布——不足一整行时（如 8 个格子的最后 2 个）
   自动水平居中，而不是靠在左侧 */
.detail-grid {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
  padding: 2px 16px 16px;
}

.detail-item {
  flex: 0 0 calc((100% - 20px) / 3);
  min-width: 200px;
  box-sizing: border-box;
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
