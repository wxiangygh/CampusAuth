<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { NButton, NInput, NSwitch, NCheckbox } from 'naive-ui'
import { api } from '../bridge'
import { store } from '../store'
import { ui } from '../ui'

// 6 类路由：标签即语义，不再依赖颜色区分（单色设计）
const ROUTE_ORDER = ['ipv4', 'ipv6', 'ipv4_warp', 'ipv4_warp_ipv6', 'ipv6_warp', 'ipv6_warp_ipv4']
const ROUTE_LABEL = {
  ipv4: 'IPv4 直连',
  ipv6: 'IPv6 直连',
  ipv4_warp: 'WARP[v4]→v4',
  ipv4_warp_ipv6: 'WARP[v4]→v6',
  ipv6_warp: 'WARP[v6]→v6',
  ipv6_warp_ipv4: 'WARP[v6]→v4',
}

const traffic = reactive({
  conns: [],
  stats: {},
  warpUnderlay: 'ipv4',
  cumulativeMode: false,
  autoRefresh: true,
  search: '',
  loadingFast: false,
  applying: false,
})

const ROUTE_ACTION_LABEL = {
  ipv4: 'IPv4 直连',
  ipv6: 'IPv6 直连',
  warp: '不直连（走 WARP）',
}

const cumulativeConns = reactive(new Map())
const selectedConns = reactive(new Set())
const collapsedGroups = reactive(new Set())

let loadingSlow = false
let autoTimer = null
let refreshTimer = null

// 操作条相对列表列（.traffic-view）水平居中，而非整个窗口（侧边栏存在时二者中心不同）
const trafficViewRef = ref(null)
const barLeft = ref('50%')
let barObserver = null

function updateBarCenter() {
  const el = trafficViewRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  if (!rect.width) return
  barLeft.value = `${Math.round(rect.left + rect.width / 2)}px`
}

function connId(c) {
  return `${c.process}|${c.remote_ip}|${c.remote_port}`
}

// ===== 数据获取 =====
async function refreshFast() {
  if (!api() || traffic.loadingFast) return
  traffic.loadingFast = true
  try {
    const data = await api().get_traffic_status_fast()
    // 保留上一轮已解析的 hostname，避免自动刷新时域名闪失
    if (traffic.conns.length) {
      const oldHost = new Map()
      for (const c of traffic.conns) {
        if (c.hostname) oldHost.set(c.remote_ip, c.hostname)
      }
      for (const c of data.connections || []) {
        if (!c.hostname && oldHost.has(c.remote_ip)) c.hostname = oldHost.get(c.remote_ip)
      }
    }
    traffic.conns = data.connections || []
    traffic.stats = data.stats || traffic.stats
    traffic.warpUnderlay = data.warp_underlay || 'ipv4'
    if (traffic.cumulativeMode && traffic.conns.length) {
      for (const c of traffic.conns) cumulativeConns.set(connId(c), c)
    }
    pruneSelection()
  } catch (e) {
    ui.toast('获取失败: ' + e, 'error')
  } finally {
    traffic.loadingFast = false
  }
}

async function refreshSlow() {
  if (!api() || loadingSlow) return
  loadingSlow = true
  try {
    const missingIps = []
    for (const c of traffic.conns) {
      if (!c.hostname && c.remote_ip) missingIps.push(c.remote_ip)
    }
    if (!missingIps.length) return
    const ipToHost = await api().get_traffic_status_slow(missingIps)
    let updated = false
    for (const c of traffic.conns) {
      if (!c.hostname && ipToHost[c.remote_ip]) {
        c.hostname = ipToHost[c.remote_ip]
        updated = true
      }
    }
    if (updated && traffic.cumulativeMode) {
      for (const c of traffic.conns) {
        if (c.hostname) cumulativeConns.set(connId(c), c)
      }
    }
  } catch (e) {
    console.warn('refreshSlow failed:', e)
  } finally {
    loadingSlow = false
  }
}

function pruneSelection() {
  const visible = new Set(filteredConns.value.map((c) => connId(c)))
  const toRemove = []
  for (const id of selectedConns) {
    if (!visible.has(id)) toRemove.push(id)
  }
  for (const id of toRemove) selectedConns.delete(id)
}

// ===== 自动刷新 =====
function startAutoRefresh() {
  stopAutoRefresh()
  autoTimer = setInterval(() => refreshFast(), 3000)
}

function stopAutoRefresh() {
  if (autoTimer) {
    clearInterval(autoTimer)
    autoTimer = null
  }
}

function refreshDebounced(delay) {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    refreshFast()
  }, delay)
}

// ===== 概览 =====
const totalCount = computed(() =>
  ROUTE_ORDER.reduce((sum, key) => sum + (traffic.stats[key] || 0), 0)
)

// ===== 连接列表 =====
const filteredConns = computed(() => {
  let conns = traffic.cumulativeMode ? Array.from(cumulativeConns.values()) : traffic.conns
  const kw = traffic.search.trim().toLowerCase()
  if (kw) {
    conns = conns.filter(
      (c) =>
        (c.process || '').toLowerCase().includes(kw) ||
        (c.remote_ip || '').toLowerCase().includes(kw) ||
        (c.hostname || '').toLowerCase().includes(kw)
    )
  }
  return conns
})

const groupedConns = computed(() => {
  const groups = {}
  for (const c of filteredConns.value) {
    const key = c.process || 'unknown'
    ;(groups[key] ||= []).push(c)
  }
  return Object.keys(groups)
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
    .map((name) => ({ name, items: groups[name] }))
})

function toggleGroupCollapse(proc) {
  if (collapsedGroups.has(proc)) collapsedGroups.delete(proc)
  else collapsedGroups.add(proc)
}

function toggleConn(id, checked) {
  if (checked) selectedConns.add(id)
  else selectedConns.delete(id)
}

function toggleGroup(proc, checked) {
  const items = filteredConns.value.filter((c) => (c.process || 'unknown') === proc)
  for (const c of items) {
    if (checked) selectedConns.add(connId(c))
    else selectedConns.delete(connId(c))
  }
}

const allSelected = computed(
  () => filteredConns.value.length > 0 && filteredConns.value.every((c) => selectedConns.has(connId(c)))
)

function toggleSelectAll(checked) {
  if (checked) {
    for (const c of filteredConns.value) selectedConns.add(connId(c))
  } else {
    selectedConns.clear()
  }
}

function clearSelection() {
  selectedConns.clear()
}

function toggleCumulative(enabled) {
  traffic.cumulativeMode = enabled
  if (enabled) {
    for (const c of traffic.conns) cumulativeConns.set(connId(c), c)
    ui.toast(`累计展示已开启，当前 ${cumulativeConns.size} 个连接`)
  } else {
    cumulativeConns.clear()
    ui.toast('已切换为实时展示')
  }
}

// 根据路由选择推断修改后的 route_type（与后端分类逻辑一致）
function inferRouteType(route, conn) {
  const isIpv6 = (conn.remote_ip || '').includes(':') || conn.is_ipv6
  if (route === 'ipv4') return 'ipv4'
  if (route === 'ipv6') return 'ipv6'
  if (traffic.warpUnderlay === 'ipv6') return isIpv6 ? 'ipv6_warp' : 'ipv6_warp_ipv4'
  return isIpv6 ? 'ipv4_warp_ipv6' : 'ipv4_warp'
}

async function setRoute(route) {
  if (selectedConns.size === 0 || traffic.applying) return
  const conns = []
  for (const id of selectedConns) {
    const c = filteredConns.value.find((cc) => connId(cc) === id)
    if (c) conns.push(c)
  }
  if (!conns.length) return
  return applyRoute(conns, route)
}

async function applyRoute(conns, route) {
  if (!conns.length || !api()) return
  traffic.applying = true
  const items = []
  const connIndex = []
  for (const c of conns) {
    items.push({ hostname: c.hostname || '', remote_ip: c.remote_ip })
    connIndex.push(c)
  }
  const routeLabel = ROUTE_ACTION_LABEL[route] || route
  ui.toast(`正在设置 ${items.length} 个连接为 ${routeLabel}...`)
  try {
    const result = await api().set_connections_route(items, route)
    const results = result.results || []
    let successCount = 0
    let failCount = 0
    const failMessages = []
    for (const r of results) {
      const matched = connIndex.find(
        (c) => (c.hostname || '') === r.hostname && c.remote_ip === r.remote_ip
      )
      if (r.success) {
        successCount++
        // 乐观更新：仅对成功的连接更新类型
        if (matched && traffic.cumulativeMode) {
          const c = cumulativeConns.get(connId(matched))
          if (c) {
            c.route_type = inferRouteType(route, c)
            c.is_warp = route === 'warp'
            cumulativeConns.set(connId(matched), c)
          }
        }
      } else {
        failCount++
        failMessages.push(`${r.hostname || r.remote_ip}: ${r.message}`)
      }
    }
    if (failCount === 0) {
      ui.toast(`${routeLabel}: 全部成功 (${successCount}/${items.length})`, 'success')
    } else {
      ui.toast(`${routeLabel}: 成功 ${successCount}，失败 ${failCount}。${failMessages.slice(0, 2).join('; ')}`, 'error')
    }
    // 防抖刷新：延迟获取后端实际数据覆盖乐观更新
    refreshDebounced(1200)
    if (failCount === 0) clearSelection()
  } catch (e) {
    ui.toast('设置失败: ' + e, 'error')
  } finally {
    traffic.applying = false
  }
}

// ===== 生命周期与联动 =====
watch(
  () => store.activeTab,
  async (name) => {
    if (name === 'traffic') {
      if (traffic.autoRefresh) startAutoRefresh()
      await nextTick()
      updateBarCenter()
    } else {
      stopAutoRefresh()
    }
  }
)

watch(
  () => traffic.autoRefresh,
  (enabled) => {
    if (enabled && store.activeTab === 'traffic') startAutoRefresh()
    else stopAutoRefresh()
  }
)

watch(
  () => store.apiReady,
  async (ready) => {
    if (!ready) return
    await nextTick()
    await refreshFast()
    refreshSlow()
  },
  { immediate: true }
)

onMounted(() => {
  if (store.activeTab === 'traffic' && traffic.autoRefresh) startAutoRefresh()
  updateBarCenter()
  window.addEventListener('resize', updateBarCenter)
  if (trafficViewRef.value && typeof ResizeObserver !== 'undefined') {
    barObserver = new ResizeObserver(updateBarCenter)
    barObserver.observe(trafficViewRef.value)
  }
})

onBeforeUnmount(() => {
  stopAutoRefresh()
  if (refreshTimer) clearTimeout(refreshTimer)
  window.removeEventListener('resize', updateBarCenter)
  if (barObserver) barObserver.disconnect()
})
</script>

<template>
  <div ref="trafficViewRef" class="traffic-view">
    <!-- 页头 -->
    <div class="tv-head">
      <div class="tv-head-left">
        <h2 class="tv-title">流量监控</h2>
        <span class="tv-sub">活动 TCP 连接与路由分类</span>
      </div>
      <div class="tv-head-right">
        <span class="tv-chip">WARP 底层 {{ traffic.warpUnderlay === 'ipv6' ? 'IPv6' : 'IPv4' }}</span>
        <span class="tv-chip mono">{{ totalCount }} 连接</span>
      </div>
    </div>

    <!-- 路由流向概览（单色示意） -->
    <section class="tv-card flow-card">
      <div class="flow-schematic">
        <div class="flow-endpoint">
          <div class="flow-node">
            <span class="flow-node-title">本机</span>
            <span class="flow-node-num mono">{{ totalCount }}</span>
          </div>
        </div>
        <div class="flow-lines" aria-hidden="true"><i></i><i></i><i></i></div>
        <div class="flow-tunnel">
          <div class="flow-tunnel-title">
            WARP 隧道
            <span class="flow-tunnel-sub mono">{{ traffic.warpUnderlay === 'ipv6' ? 'IPv6' : 'IPv4' }}</span>
          </div>
          <div class="flow-route" v-for="key in ['ipv4_warp', 'ipv4_warp_ipv6', 'ipv6_warp', 'ipv6_warp_ipv4']"
            :key="key" :class="{ active: (traffic.stats[key] || 0) > 0 }">
            <span class="flow-route-label">{{ ROUTE_LABEL[key] }}</span>
            <i class="flow-route-line"></i>
            <span class="flow-route-num mono">{{ traffic.stats[key] || 0 }}</span>
          </div>
        </div>
        <div class="flow-lines" aria-hidden="true"><i></i><i></i></div>
        <div class="flow-direct">
          <div class="flow-direct-caption">直连</div>
          <div class="flow-route" v-for="key in ['ipv4', 'ipv6']" :key="key"
            :class="{ active: (traffic.stats[key] || 0) > 0 }">
            <span class="flow-route-label">{{ ROUTE_LABEL[key] }}</span>
            <i class="flow-route-line"></i>
            <span class="flow-route-num mono">{{ traffic.stats[key] || 0 }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 工具栏 -->
    <div class="tv-toolbar">
      <n-checkbox :checked="allSelected" @update:checked="toggleSelectAll">全选</n-checkbox>
      <n-input v-model:value="traffic.search" size="small" placeholder="搜索进程名 / 域名 / IP" clearable
        style="width: min(280px, 100%)" />
      <label class="tv-switch">
        <n-switch :value="traffic.cumulativeMode" size="small" @update:value="toggleCumulative" />
        累计展示
      </label>
      <label class="tv-switch">
        <n-switch v-model:value="traffic.autoRefresh" size="small" />
        自动刷新
      </label>
      <div class="tv-toolbar-spacer"></div>
      <n-button quaternary circle size="small" class="refresh-btn" title="刷新" @click="refreshFast()">
        <svg class="refresh-icon" :class="{ spinning: traffic.loadingFast }" viewBox="0 0 24 24" fill="none"
          aria-hidden="true">
          <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round" />
          <path d="M21 3v5h-5" stroke="currentColor" stroke-width="2" stroke-linecap="round"
            stroke-linejoin="round" />
          <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round" />
          <path d="M8 16H3v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"
            stroke-linejoin="round" />
        </svg>
      </n-button>
    </div>
    <div class="loading-bar" :class="{ active: traffic.loadingFast }"></div>

    <!-- 连接分组列表 -->
    <section class="conn-panel">
      <div v-if="!groupedConns.length" class="empty-hint">
        {{ traffic.cumulativeMode ? '暂无累计连接' : '暂无活动连接' }}
      </div>
      <div v-for="group in groupedConns" :key="group.name" class="proc-group"
        :class="{ collapsed: collapsedGroups.has(group.name) }">
        <div class="proc-group-header" @click="toggleGroupCollapse(group.name)">
          <svg class="collapse-icon" viewBox="0 0 10 10">
            <path d="M2 3 L5 7 L8 3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"
              stroke-linejoin="round" />
          </svg>
          <n-checkbox :checked="group.items.every((c) => selectedConns.has(connId(c)))"
            :style="{ opacity: group.items.some((c) => selectedConns.has(connId(c))) && !group.items.every((c) => selectedConns.has(connId(c))) ? 0.5 : 1 }"
            @click.stop @update:checked="(v) => toggleGroup(group.name, v)" />
          <span class="proc-name">{{ group.name }}</span>
          <span class="proc-count">{{ group.items.length }}</span>
        </div>
        <div class="proc-group-body">
          <div v-for="(c, i) in group.items" :key="i" class="conn-row"
            :class="{ selected: selectedConns.has(connId(c)) }"
            @click="toggleConn(connId(c), !selectedConns.has(connId(c)))">
            <n-checkbox :checked="selectedConns.has(connId(c))" @click.stop
              @update:checked="(v) => toggleConn(connId(c), v)" />
            <span class="conn-host">
              <span class="conn-hostname">{{ c.hostname || '(无域名)' }}</span>
              <span class="conn-ip mono">{{ c.remote_ip }}:{{ c.remote_port }}</span>
            </span>
            <span class="conn-tag">{{ ROUTE_LABEL[c.route_type] || ROUTE_LABEL.ipv4 }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 选中操作条（悬浮固定在窗口底部，相对列表列居中） -->
    <!-- defer：.app-shell 由同一组件树渲染，初次挂载时尚未入文档，延迟解析目标避免 Teleport 失效 -->
    <Teleport defer to=".app-shell">
      <div class="action-bar" :style="{ left: barLeft }"
        :class="{ show: selectedConns.size > 0 && store.activeTab === 'traffic' }">
        <span class="selected-count">已选 {{ selectedConns.size }} 项</span>
        <n-button size="tiny" :disabled="traffic.applying" @click="setRoute('ipv4')">IPv4 直连</n-button>
        <n-button size="tiny" :disabled="traffic.applying" @click="setRoute('ipv6')">IPv6 直连</n-button>
        <n-button size="tiny" secondary :disabled="traffic.applying" @click="setRoute('warp')">不直连 (WARP)</n-button>
        <n-button size="tiny" quaternary :disabled="traffic.applying" @click="clearSelection()">取消选择</n-button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.traffic-view {
  padding: clamp(14px, 2cqi, 26px);
  container-type: inline-size;
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.mono {
  font-family: var(--font-mono);
}

/* ===== 页头 ===== */
.tv-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.tv-head-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.tv-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: 0.2px;
}

.tv-sub {
  font-size: 12px;
  color: var(--text-tertiary);
}

.tv-head-right {
  display: flex;
  gap: 8px;
}

.tv-chip {
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 4px 12px;
}

/* ===== 路由流向概览 ===== */
.tv-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 12px;
}

.flow-card {
  padding: 16px 18px;
}

.flow-schematic {
  display: flex;
  align-items: stretch;
  gap: 14px;
}

.flow-endpoint {
  display: flex;
  align-items: center;
}

.flow-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 88px;
  height: 88px;
  border-radius: 50%;
  border: 1.5px solid var(--border-strong);
  background: var(--bg-elevated);
  flex-shrink: 0;
}

.flow-node-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.flow-node-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
}

.flow-lines {
  display: flex;
  flex-direction: column;
  justify-content: space-evenly;
  align-self: stretch;
  min-width: 22px;
}

.flow-lines i {
  display: block;
  height: 1px;
  background: var(--border-strong);
}

.flow-tunnel,
.flow-direct {
  flex: 1;
  min-width: 0;
  border: 1px dashed var(--border-strong);
  border-radius: 10px;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.flow-direct {
  border-style: solid;
  border-color: var(--border);
  max-width: 300px;
}

.flow-tunnel-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.flow-tunnel-sub {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-tertiary);
  background: var(--bg-elevated);
  padding: 1px 8px;
  border-radius: 4px;
}

.flow-direct-caption {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.flow-route {
  display: flex;
  align-items: center;
  gap: 10px;
  opacity: 0.55;
}

.flow-route.active {
  opacity: 1;
}

.flow-route-label {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.flow-route.active .flow-route-label {
  color: var(--text-primary);
}

.flow-route-line {
  flex: 1;
  height: 1px;
  background: var(--border);
  min-width: 12px;
}

.flow-route.active .flow-route-line {
  background: var(--border-strong);
}

.flow-route-num {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
  min-width: 2ch;
  text-align: right;
}

/* ===== 工具栏 ===== */
.tv-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.tv-switch {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}

.tv-toolbar-spacer {
  flex: 1;
}

.loading-bar {
  height: 2px;
  border-radius: 1px;
  overflow: hidden;
  position: relative;
  margin-top: -6px;
}

.loading-bar.active {
  background: var(--bg-elevated);
}

.loading-bar.active::after {
  content: '';
  position: absolute;
  inset: 0;
  width: 40%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: slide 1s linear infinite;
}

@keyframes slide {
  from {
    transform: translateX(-100%);
  }

  to {
    transform: translateX(350%);
  }
}

/* ===== 连接列表 ===== */
.conn-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.empty-hint {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  border: 1px dashed var(--border-strong);
  border-radius: 12px;
}

.proc-group {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}

.proc-group-header {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 14px;
  background: var(--bg-elevated);
  cursor: pointer;
  user-select: none;
  flex-wrap: wrap;
}

.proc-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* 刷新按钮：纯图标、无底色，与主题背景融为一体；加载时图标自旋，尺寸不变 */
.refresh-btn {
  flex-shrink: 0;
  color: var(--text-secondary);
}

.refresh-icon {
  width: 15px;
  height: 15px;
}

.refresh-icon.spinning {
  animation: refresh-spin 0.9s linear infinite;
}

@keyframes refresh-spin {
  to {
    transform: rotate(360deg);
  }
}

.collapse-icon {
  width: 10px;
  height: 10px;
  color: var(--text-tertiary);
  transition: transform 0.2s;
  flex-shrink: 0;
}

.proc-group.collapsed .collapse-icon {
  transform: rotate(-90deg);
}

.proc-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.proc-count {
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  padding: 1px 8px;
  border-radius: 8px;
  font-family: var(--font-mono);
}

.proc-group.collapsed .proc-group-body {
  display: none;
}

.conn-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 14px;
  border-top: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.15s;
}

.conn-row:hover {
  background: var(--bg-hover);
}

.conn-row.selected {
  background: var(--accent-dim);
}

.conn-host {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.conn-hostname {
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conn-ip {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conn-tag {
  font-size: 11px;
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  padding: 2px 8px;
  white-space: nowrap;
  flex-shrink: 0;
}

.conn-row.selected .conn-tag {
  border-color: var(--accent);
  color: var(--text-primary);
}

/* ===== 选中操作条（悬浮固定在窗口底部） ===== */
.action-bar {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translate(-50%, 14px);
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 9px 14px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.22);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease, transform 0.2s ease;
  z-index: 100;
}

.action-bar.show {
  opacity: 1;
  transform: translate(-50%, 0);
  pointer-events: auto;
}

.selected-count {
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 600;
  margin-right: 4px;
}

/* ===== 窄容器：概览纵向堆叠 ===== */
@container (max-width: 820px) {
  .flow-schematic {
    flex-direction: column;
  }

  .flow-endpoint {
    justify-content: center;
  }

  .flow-node {
    width: 76px;
    height: 76px;
  }

  .flow-lines {
    flex-direction: row;
    justify-content: space-evenly;
    align-self: auto;
    min-width: 0;
    height: 18px;
  }

  .flow-lines i {
    width: 1px;
    height: 100%;
  }

  .flow-direct {
    max-width: none;
  }
}
</style>
