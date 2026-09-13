<script setup>
import { ref, computed, watch, h, onMounted, onBeforeUnmount } from 'vue'
import { NButton, NDropdown } from 'naive-ui'
import { store, startAuth, startRestore, cancelOperation, updateStatusFromCheck, refreshNetworkDetail, setParticleCanvas, doAutoSave } from '../store'
import { api } from '../bridge'
import { ui } from '../ui'
import AppIcon from '../components/AppIcon.vue'

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

// 网络详情按重要性分组（PM：先回答「免流了吗」，再看链路，硬件垫底）。
// 每项: { key, label, value, cls, mono?, copyable?, badge? }
// 注意：WARP 连接状态不在此重复展示——英雄区大标题 + 底部状态条已各承担一次
const detailGroups = computed(() => {
  const d = store.detail || {}
  const wired = d.link_type === 'wired'
  // IPv4 在线但 WARP 未走 IPv6 底层 = 流量可能按校园网计费，在 IPv4 行显式提醒
  const billingRisk = !!d.warp_connected && d.warp_underlay === 'ipv4' && !!d.ipv4
  return [
    {
      title: '核心状态',
      items: [
        { key: 'ipv6', label: 'IPv6 公网地址', value: d.ipv6 || '无公网IPv6',
          cls: d.ipv6 ? 'success' : 'warning', copyable: !!d.ipv6, mono: true, wide: true },
        { key: 'ipv4', label: 'IPv4 地址', value: d.ipv4 || (d.ipv4_disabled ? '已禁用' : '—'),
          cls: d.ipv4 ? '' : 'empty', mono: true, wide: true, badge: billingRisk ? '计费风险' : '' },
      ],
    },
    {
      title: '网络链路',
      items: [
        { key: 'link', label: '连接方式', value: d.link_type ? (wired ? '有线' : '无线') : '未知',
          cls: d.link_type ? 'success' : 'empty' },
        { key: 'iface', label: '接口', value: d.interface || '未知', cls: '' },
        wired
          ? { key: 'wired', label: '有线网卡', value: d.wired_interface || '未连接',
              cls: d.wired_interface ? 'success' : 'empty' }
          : { key: 'wifi', label: 'WiFi', value: d.wifi_ssid || '未连接',
              cls: d.wifi_ssid ? '' : 'warning' },
        { key: 'mac', label: 'MAC', value: d.mac || '—', cls: d.mac ? '' : 'empty', mono: true },
      ],
    },
  ]
})

// ===== 复制详情值（剪贴板优先，file:// 等非安全上下文回退 execCommand）=====
async function copyText(text) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ui.toast('已复制到剪贴板', 'success')
  } catch (e) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      ui.toast('已复制到剪贴板', 'success')
    } catch (e2) {
      ui.toast('复制失败', 'error')
    }
  }
}

// ===== 详情手动刷新（图标旋转反馈）=====
const detailRefreshing = ref(false)

async function refreshDetail() {
  if (detailRefreshing.value) return
  detailRefreshing.value = true
  try {
    const status = await api()?.check_network_status?.()
    if (status && !store.authRunning) updateStatusFromCheck(status)
    await refreshNetworkDetail()
  } catch (e) {
    console.warn('refreshDetail failed:', e)
  } finally {
    setTimeout(() => { detailRefreshing.value = false }, 500)
  }
}

// ===== 免流元信息 chips（底层 / 锁定方式 / 已持续时长）=====
const nowTick = ref(Date.now())
let elapsedTimer = null

const freeElapsedText = computed(() => {
  if (!store.freeSince) return ''
  const seconds = Math.max(0, Math.floor((nowTick.value - store.freeSince) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
})

const metaChips = computed(() => {
  const d = store.detail || {}
  if (!d.warp_connected) return []
  const chips = [
    { key: 'underlay', text: d.warp_underlay === 'ipv4' ? 'IPv4 底层' : 'IPv6 底层',
      cls: d.warp_underlay === 'ipv4' ? 'warn' : 'ok' },
  ]
  if (d.warp_free) {
    chips.push({ key: 'lock', text: d.ipv4_disabled ? 'IPv4 已禁用' : '防火墙锁定', cls: '' })
    if (freeElapsedText.value) {
      chips.push({ key: 'since', text: `已持续 ${freeElapsedText.value}`, cls: '' })
    }
  }
  return chips
})

// 进行中块的一行文案：步骤序号 + 当前节点消息（可见反馈取代 hover title）
const progressText = computed(() => {
  const p = store.progress
  const stepText = p.total > 0 && p.step > 0 ? `步骤 ${p.step}/${p.total} · ` : ''
  return `${stepText}${p.detail || p.label}`
})

// 副标题去重：每条信息只在主页出现一次——
// - 认证中：进度块已展示当前步骤 → 副标题隐藏
// - 成功态且元信息胶囊已就位：胶囊已表达「底层/锁定/持续」→ 副标题隐藏
//   （认证刚完成、胶囊未就位的窗口期仍显示，让「认证成功」结果文案可见）
// - 其余状态（未免流警告/错误/空闲）：副标题是唯一的解释文本 → 显示
const showSubtitle = computed(() => {
  if (store.authRunning) return false
  if (!store.status.subtitle) return false
  if (store.status.state === 'success' && metaChips.value.length) return false
  return true
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
  // 尊重系统「减少动态效果」设置：跳过粒子动画，静态六边形保持完整信息
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches
  if (!reduceMotion && particleRef.value) setParticleCanvas(particleRef.value)
  document.addEventListener('visibilitychange', onVisibilityChange)
  // 免流持续时长每 30 秒刷新一次
  elapsedTimer = setInterval(() => { nowTick.value = Date.now() }, 30000)
})

onBeforeUnmount(() => {
  stopStatusPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  if (elapsedTimer) clearInterval(elapsedTimer)
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
      <div class="status-title" aria-live="polite">{{ store.status.title }}</div>
      <div class="status-subtitle" v-if="showSubtitle">{{ store.status.subtitle }}</div>

      <!-- 免流元信息：1 秒回答「为什么算免流、持续多久」 -->
      <div class="meta-chips" v-if="metaChips.length && !store.authRunning">
        <span v-for="chip in metaChips" :key="chip.key" class="meta-chip"
          :class="chip.cls">{{ chip.text }}</span>
      </div>

      <div class="progress-container" :class="{ active: store.progress.visible || store.authRunning }">
        <div class="progress-track">
          <div class="progress-fill" :style="{ width: store.progress.pct + '%' }"></div>
        </div>
        <div class="step-dots" v-if="store.progress.total > 0">
          <span v-for="n in store.progress.total" :key="n" class="step-dot"
            :class="{ done: n < store.progress.step, current: n === store.progress.step }"></span>
        </div>
        <div class="progress-info">
          <div class="progress-label">{{ progressText }}</div>
          <div class="progress-right">
            <span class="progress-text">{{ Math.round(store.progress.pct) }}%</span>
            <button class="interrupt-btn" title="中断当前操作（在当前节点停止并回滚）"
              @click="cancelOperation">中断</button>
          </div>
        </div>
      </div>

      <div class="action-hint" v-if="!store.authRunning">
        <AppIcon name="info" :size="12" />
        <span>将执行「{{ boundAuthName }}」· 右键可切换</span>
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
          <n-button class="btn-ghost" quaternary size="large"
            :title="`恢复网络执行：${boundRestoreName}（右键可切换绑定的工作流）`" @click="startRestore"
            @contextmenu.prevent="toggleRestoreMenu">恢复网络</n-button>
          <n-dropdown trigger="manual" size="small" placement="bottom" :show="restoreMenu"
            :options="restoreWfOptions" @select="onSelectRestoreWf" @clickoutside="closeAllMenus">
            <span class="split-caret" :class="{ open: restoreMenu }"
              title="选择「恢复网络」执行的工作流" @click.stop.prevent="toggleRestoreMenu"><i
                class="caret-glyph"><CaretDown /></i></span>
          </n-dropdown>
        </div>
      </div>
    </section>

    <!-- 网络详情（可折叠） -->
    <section class="card detail-card" :class="{ collapsed: store.detailCollapsed }">
      <div class="detail-header" @click="toggleDetail">
        <span class="detail-title">网络详情</span>
        <span class="detail-tools">
          <button class="icon-btn" title="重新检测网络状态"
            @click.stop="refreshDetail">
            <AppIcon name="refresh" :size="14" :class="{ spinning: detailRefreshing }" />
          </button>
          <span class="detail-arrow" :class="{ collapsed: store.detailCollapsed }">▾</span>
        </span>
      </div>
      <div class="detail-body">
        <div class="detail-groups" v-if="store.detail">
          <div class="detail-group" v-for="group in detailGroups" :key="group.title">
            <div class="group-title">{{ group.title }}</div>
            <div class="detail-grid">
              <div class="detail-item" :class="[item.cls, { wide: item.wide }]" v-for="item in group.items" :key="item.key">
                <div class="detail-label">
                  {{ item.label }}
                  <span class="risk-badge" v-if="item.badge">{{ item.badge }}</span>
                </div>
                <div class="detail-value-row">
                  <div class="detail-value" :class="item.cls" :title="item.value">{{ item.value }}</div>
                  <button class="icon-btn tiny" v-if="item.copyable" title="复制完整地址"
                    @click="copyText(item.value)">
                    <AppIcon name="copy" :size="12" />
                  </button>
                </div>
              </div>
            </div>
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
/* 分组详情：统一 6 列网格，全部左对齐——跨组的盒子边缘垂直对齐，
   普通项占 2 列（一行三个），核心状态长地址项占 3 列（一行两个） */
.detail-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  padding: 2px 16px 16px;
}

.detail-item {
  grid-column: span 2;
  min-width: 0;
  box-sizing: border-box;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 13px;
}

.detail-item.wide {
  grid-column: span 3;
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

/* ===== 免流元信息 chips ===== */
.meta-chips {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
}

.meta-chip {
  font-size: 11px;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-family: var(--font-mono);
  letter-spacing: 0.2px;
}

.meta-chip.ok {
  color: var(--success);
  border-color: color-mix(in srgb, var(--success) 32%, transparent);
}

.meta-chip.warn {
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 32%, transparent);
}

/* ===== 可见的操作提示行（取代 hover-only title）===== */
.action-hint {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-tertiary);
  max-width: 420px;
}

/* ===== 进行中一体块：进度 + 步骤点阵 + 中断 ===== */
.progress-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.progress-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.progress-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.interrupt-btn {
  height: 22px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--error) 55%, transparent);
  border-radius: 6px;
  background: transparent;
  color: var(--error);
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}

.interrupt-btn:hover {
  background: color-mix(in srgb, var(--error) 12%, transparent);
}

.step-dots {
  display: flex;
  justify-content: center;
  gap: 5px;
  margin-top: 10px;
}

.step-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--bg-hover);
  border: 1px solid var(--border-strong);
  transition: all 0.3s ease;
}

.step-dot.done {
  background: var(--accent);
  border-color: var(--accent);
}

.step-dot.current {
  background: transparent;
  border-color: var(--accent);
  animation: dot-pulse 1.2s ease-in-out infinite;
}

@keyframes dot-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.35); }
}

/* ===== 详情分组 ===== */
.detail-groups {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 2px 16px 16px;
}

.detail-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.group-title {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

/* 组内网格与原网格一致：flex 居中排布 */
.detail-groups .detail-grid {
  padding: 0;
}

.detail-value-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
}

.detail-value-row .detail-value {
  margin-top: 0;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.risk-badge {
  font-size: 9px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 4px;
  margin-left: 5px;
  letter-spacing: 0.5px;
  vertical-align: 1px;
  color: var(--warning);
  border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent);
  text-transform: none;
}

/* ===== 详情头工具钮 ===== */
.detail-tools {
  display: flex;
  align-items: center;
  gap: 6px;
}

.icon-btn {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: all 0.15s;
}

.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-btn:focus-visible,
.interrupt-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.icon-btn.tiny {
  width: 18px;
  height: 18px;
}

.icon-btn .spinning {
  animation: icon-spin 0.8s linear infinite;
}

@keyframes icon-spin {
  to { transform: rotate(360deg); }
}

/* ===== 恢复网络 ghost 按钮（低频操作，视觉降级）=====
   quaternary 无边框透明底；ghost 语义下压低文字对比，hover 才升到主文字色 */
.btn-ghost {
  font-weight: 500;
  color: var(--text-secondary);
}

/* ===== 减少动态效果：停用旋转/脉冲/划出动画，信息保持完整 ===== */
@media (prefers-reduced-motion: reduce) {
  .hexagon-rotate,
  .status-indicator.running .status-icon,
  .step-dot.current,
  .icon-btn .spinning {
    animation: none;
  }
}
</style>
