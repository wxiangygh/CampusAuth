<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { NButton, NInput, NSwitch, NCheckbox, NTag } from 'naive-ui'
import { api } from '../bridge'
import { store } from '../store'
import { ui } from '../ui'
import { accentHex, accentRGBA } from '../theme'

// ===== 6 类路由配置 =====
const ROUTE_CONFIG = {
  ipv4: { color: '#3b82f6', label: 'IPv4 直连' },
  ipv6: { color: '#22C55E', label: 'IPv6 直连' },
  ipv4_warp: { color: '#F59E0B', label: 'WARP[v4]→v4' },
  ipv4_warp_ipv6: { color: '#EAB308', label: 'WARP[v4]→v6' },
  ipv6_warp: { color: '#EF4444', label: 'WARP[v6]→v6' },
  ipv6_warp_ipv4: { color: '#A855F7', label: 'WARP[v6]→v4' },
}

const traffic = reactive({
  subview: 'list',
  conns: [],
  stats: {},
  warpUnderlay: 'ipv4',
  cumulativeMode: false,
  autoRefresh: true,
  searchList: '',
  searchCanvas: '',
  loadingFast: false,
})

const cumulativeConns = reactive(new Map())
const selectedConns = reactive(new Set())
const collapsedGroups = reactive(new Set())

let loadingSlow = false
let autoTimer = null
let refreshTimer = null

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
    drawCanvas()
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
    if (updated) drawCanvas()
  } catch (e) {
    console.warn('refreshSlow failed:', e)
  } finally {
    loadingSlow = false
  }
}

function pruneSelection() {
  const visible = new Set(filteredCanvasConns.value.map((c) => connId(c)))
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

// ===== 列表视图 =====
const groupedListView = computed(() => {
  let conns = traffic.conns
  const kw = traffic.searchList.trim().toLowerCase()
  if (kw) {
    conns = conns.filter(
      (c) =>
        (c.process || '').toLowerCase().includes(kw) ||
        (c.remote_ip || '').toLowerCase().includes(kw) ||
        (c.hostname || '').toLowerCase().includes(kw)
    )
  }
  const groups = {}
  for (const c of conns) {
    const key = c.process || 'unknown'
    ;(groups[key] ||= []).push(c)
  }
  return Object.keys(groups)
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
    .map((name) => ({ name, items: groups[name] }))
})

const statCards = computed(() =>
  Object.entries(ROUTE_CONFIG).map(([key, cfg]) => ({
    key,
    color: cfg.color,
    label: cfg.label,
    count: traffic.stats[key] || 0,
  }))
)

// ===== 画布视图 =====
const filteredCanvasConns = computed(() => {
  let conns
  if (traffic.cumulativeMode) {
    conns = Array.from(cumulativeConns.values())
  } else {
    conns = traffic.conns
  }
  const kw = traffic.searchCanvas.trim().toLowerCase()
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

const groupedCanvasList = computed(() => {
  const groups = {}
  for (const c of filteredCanvasConns.value) {
    const proc = c.process || 'unknown'
    ;(groups[proc] ||= []).push(c)
  }
  return Object.entries(groups).map(([name, items]) => ({ name, items }))
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
  const items = filteredCanvasConns.value.filter((c) => (c.process || 'unknown') === proc)
  for (const c of items) {
    if (checked) selectedConns.add(connId(c))
    else selectedConns.delete(connId(c))
  }
}

const allCanvasSelected = computed(
  () => filteredCanvasConns.value.length > 0 && filteredCanvasConns.value.every((c) => selectedConns.has(connId(c)))
)

function toggleSelectAll(checked) {
  if (checked) {
    for (const c of filteredCanvasConns.value) selectedConns.add(connId(c))
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
  const isIpv6 = ':' in (conn.remote_ip || '') || conn.is_ipv6
  if (route === 'ipv4') return 'ipv4'
  if (route === 'ipv6') return 'ipv6'
  if (traffic.warpUnderlay === 'ipv6') return isIpv6 ? 'ipv6_warp' : 'ipv6_warp_ipv4'
  return isIpv6 ? 'ipv4_warp_ipv6' : 'ipv4_warp'
}

async function setRoute(route) {
  if (selectedConns.size === 0 || !api()) return
  const items = []
  const connById = {}
  for (const id of selectedConns) {
    const c = filteredCanvasConns.value.find((cc) => connId(cc) === id)
    if (c) {
      items.push({ hostname: c.hostname || '', remote_ip: c.remote_ip })
      connById[id] = c
    }
  }
  if (!items.length) return
  const routeLabel = route === 'warp' ? '不直连(WARP)' : route === 'ipv6' ? 'IPv6 直连' : 'IPv4 直连'
  ui.toast(`正在设置 ${items.length} 个连接为 ${routeLabel}...`)
  try {
    const result = await api().set_connections_route(items, route)
    const results = result.results || []
    let successCount = 0
    let failCount = 0
    const failMessages = []
    for (const r of results) {
      const matchedId = Object.keys(connById).find((id) => {
        const c = connById[id]
        return (c.hostname || '') === r.hostname && c.remote_ip === r.remote_ip
      })
      if (r.success) {
        successCount++
        // 乐观更新：仅对成功的连接更新类型
        if (matchedId && traffic.cumulativeMode) {
          const c = cumulativeConns.get(matchedId)
          if (c) {
            c.route_type = inferRouteType(route, c)
            c.is_warp = route === 'warp'
            cumulativeConns.set(matchedId, c)
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
  }
}

// ===== Canvas 绘制 =====
const canvasRef = ref(null)
let ctx = null
let nodes = {}
let gridCanvas = null
let canvasAnimId = null

function resizeCanvas() {
  if (!canvasRef.value) return
  ctx = canvasRef.value.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const rect = canvasRef.value.parentElement.getBoundingClientRect()
  if (rect.width === 0 || rect.height === 0) return
  canvasRef.value.width = rect.width * dpr
  canvasRef.value.height = rect.height * dpr
  canvasRef.value.style.width = rect.width + 'px'
  canvasRef.value.style.height = rect.height + 'px'
  ctx.scale(dpr, dpr)
  computeNodes(rect.width, rect.height)
  buildGridCanvas(rect.width, rect.height)
}

function computeNodes(w, h) {
  const cx = w * 0.08
  const cy = h * 0.5
  const tunnelX = w * 0.35
  const tunnelW = 90
  const tunnelH = 230
  const tunnelRight = tunnelX + tunnelW / 2
  const tunnelTop = cy - tunnelH / 2
  const tunnelBottom = cy + tunnelH / 2
  const typeX = w * 0.82
  const typeSpacing = Math.min(h * 0.12, 55)
  const typeStartY = cy - typeSpacing * 2.5
  const directOffset = 18
  const warpCountSpacing = (tunnelH - 50) / 3
  const warpCountStartY = cy - warpCountSpacing * 1.5

  nodes = {
    local: { x: cx, y: cy, label: '本机', color: '#A3A3A3' },
    tunnel: { x: tunnelX, y: cy, w: tunnelW, h: tunnelH, label: 'WARP 隧道', color: accentHex() },
    type_ipv4: { x: typeX, y: typeStartY, label: 'IPv4 直连', color: '#3b82f6', isType: true, routeType: 'ipv4' },
    type_ipv6: { x: typeX, y: typeStartY + typeSpacing, label: 'IPv6 直连', color: '#22C55E', isType: true, routeType: 'ipv6' },
    type_ipv4_warp: { x: typeX, y: typeStartY + typeSpacing * 2, label: 'WARP[v4]→v4', color: '#F59E0B', isType: true, routeType: 'ipv4_warp' },
    type_ipv4_warp_ipv6: { x: typeX, y: typeStartY + typeSpacing * 3, label: 'WARP[v4]→v6', color: '#EAB308', isType: true, routeType: 'ipv4_warp_ipv6' },
    type_ipv6_warp: { x: typeX, y: typeStartY + typeSpacing * 4, label: 'WARP[v6]→v6', color: '#EF4444', isType: true, routeType: 'ipv6_warp' },
    type_ipv6_warp_ipv4: { x: typeX, y: typeStartY + typeSpacing * 5, label: 'WARP[v6]→v4', color: '#A855F7', isType: true, routeType: 'ipv6_warp_ipv4' },
    count_ipv4: { x: tunnelRight + directOffset, y: tunnelTop - directOffset, color: '#3b82f6', isCount: true, routeType: 'ipv4' },
    count_ipv6: { x: tunnelRight + directOffset, y: tunnelBottom + directOffset, color: '#22C55E', isCount: true, routeType: 'ipv6' },
    count_ipv4_warp: { x: tunnelX, y: warpCountStartY, color: '#F59E0B', isCount: true, inTunnel: true, routeType: 'ipv4_warp' },
    count_ipv4_warp_ipv6: { x: tunnelX, y: warpCountStartY + warpCountSpacing, color: '#EAB308', isCount: true, inTunnel: true, routeType: 'ipv4_warp_ipv6' },
    count_ipv6_warp: { x: tunnelX, y: warpCountStartY + warpCountSpacing * 2, color: '#EF4444', isCount: true, inTunnel: true, routeType: 'ipv6_warp' },
    count_ipv6_warp_ipv4: { x: tunnelX, y: warpCountStartY + warpCountSpacing * 3, color: '#A855F7', isCount: true, inTunnel: true, routeType: 'ipv6_warp_ipv4' },
  }
}

function getPathPoints(routeType) {
  const local = nodes.local
  const countNode = nodes['count_' + routeType]
  const typeNode = nodes['type_' + routeType]
  if (!local || !countNode || !typeNode) return null
  return [
    { x: local.x, y: local.y },
    { x: countNode.x, y: countNode.y },
    { x: typeNode.x, y: typeNode.y },
  ]
}

function pathPoint(points, t) {
  if (points.length < 2) return points[0]
  const segs = []
  let totalLen = 0
  for (let i = 0; i < points.length - 1; i++) {
    const dx = points[i + 1].x - points[i].x
    const dy = points[i + 1].y - points[i].y
    const len = Math.sqrt(dx * dx + dy * dy)
    segs.push(len)
    totalLen += len
  }
  const target = t * totalLen
  let acc = 0
  for (let i = 0; i < segs.length; i++) {
    if (acc + segs[i] >= target) {
      const localT = segs[i] > 0 ? (target - acc) / segs[i] : 0
      return {
        x: (1 - localT) * points[i].x + localT * points[i + 1].x,
        y: (1 - localT) * points[i].y + localT * points[i + 1].y,
      }
    }
    acc += segs[i]
  }
  return points[points.length - 1]
}

function roundRect(c, x, y, w, h, r) {
  c.beginPath()
  c.moveTo(x + r, y)
  c.lineTo(x + w - r, y)
  c.quadraticCurveTo(x + w, y, x + w, y + r)
  c.lineTo(x + w, y + h - r)
  c.quadraticCurveTo(x + w, y + h, x + w - r, y + h)
  c.lineTo(x + r, y + h)
  c.quadraticCurveTo(x, y + h, x, y + h - r)
  c.lineTo(x, y + r)
  c.quadraticCurveTo(x, y, x + r, y)
  c.closePath()
}

function drawTunnel(node) {
  const x = node.x - node.w / 2
  const y = node.y - node.h / 2
  ctx.fillStyle = accentRGBA(0.06)
  roundRect(ctx, x, y, node.w, node.h, 12)
  ctx.fill()
  ctx.strokeStyle = node.color
  ctx.lineWidth = 1.5
  ctx.setLineDash([4, 3])
  roundRect(ctx, x, y, node.w, node.h, 12)
  ctx.stroke()
  ctx.setLineDash([])
  ctx.fillStyle = node.color
  ctx.font = '600 10px sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(node.label, node.x, y - 10)
  ctx.fillStyle = '#A3A3A3'
  ctx.font = '500 8px sans-serif'
  ctx.fillText(traffic.warpUnderlay === 'ipv6' ? '(IPv6)' : '(IPv4)', node.x, y - 22)
}

function drawPortNode(node) {
  const w = 44
  const h = 30
  const x = node.x - w / 2
  const y = node.y - h / 2
  ctx.fillStyle = node.color + '22'
  roundRect(ctx, x, y, w, h, 5)
  ctx.fill()
  ctx.strokeStyle = node.color
  ctx.lineWidth = 1.5
  roundRect(ctx, x, y, w, h, 5)
  ctx.stroke()
  ctx.fillStyle = 'rgba(0,0,0,0.5)'
  ctx.beginPath()
  ctx.moveTo(x + 8, y + 7)
  ctx.lineTo(x + w - 8, y + 7)
  ctx.lineTo(x + w - 11, y + h - 7)
  ctx.lineTo(x + 11, y + h - 7)
  ctx.closePath()
  ctx.fill()
  ctx.strokeStyle = node.color
  ctx.lineWidth = 1
  const pinCount = 4
  const pinStart = x + 13
  const pinEnd = x + w - 13
  const pinSpacing = (pinEnd - pinStart) / (pinCount - 1)
  for (let i = 0; i < pinCount; i++) {
    const px = pinStart + i * pinSpacing
    ctx.beginPath()
    ctx.moveTo(px, y + 10)
    ctx.lineTo(px, y + h - 10)
    ctx.stroke()
  }
  ctx.fillStyle = node.color
  ctx.font = '600 9px sans-serif'
  ctx.textAlign = 'right'
  ctx.textBaseline = 'middle'
  ctx.fillText(node.label, x - 6, node.y)
}

function drawCountNode(node) {
  const count = traffic.stats[node.routeType] || 0
  const r = count > 0 ? 14 : 10
  ctx.fillStyle = node.color + (count > 0 ? '44' : '15')
  ctx.beginPath()
  ctx.arc(node.x, node.y, r, 0, Math.PI * 2)
  ctx.fill()
  ctx.strokeStyle = node.color
  ctx.lineWidth = count > 0 ? 1.5 : 1
  ctx.beginPath()
  ctx.arc(node.x, node.y, r, 0, Math.PI * 2)
  ctx.stroke()
  if (count > 0) {
    ctx.fillStyle = node.color
    ctx.font = '700 11px sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(count.toString(), node.x, node.y)
  }
}

function drawLocalNode() {
  const n = nodes.local
  const pulse = Math.sin(Date.now() / 1000) * 0.3 + 0.7
  ctx.fillStyle = accentRGBA(0.12)
  ctx.beginPath()
  ctx.arc(n.x, n.y, 26, 0, Math.PI * 2)
  ctx.fill()
  ctx.fillStyle = accentRGBA(0.25)
  ctx.beginPath()
  ctx.arc(n.x, n.y, 18, 0, Math.PI * 2)
  ctx.fill()
  ctx.strokeStyle = accentHex()
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.arc(n.x, n.y, 18, 0, Math.PI * 2)
  ctx.stroke()
  ctx.fillStyle = '#FFFFFF'
  ctx.font = '700 10px sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(n.label, n.x, n.y)
  const total =
    (traffic.stats.ipv4 || 0) +
    (traffic.stats.ipv6 || 0) +
    (traffic.stats.ipv4_warp || 0) +
    (traffic.stats.ipv4_warp_ipv6 || 0) +
    (traffic.stats.ipv6_warp || 0) +
    (traffic.stats.ipv6_warp_ipv4 || 0)
  ctx.fillStyle = '#A3A3A3'
  ctx.font = '500 8px sans-serif'
  ctx.fillText(total + ' 连接', n.x, n.y + 32)
}

function drawPath(routeType) {
  const points = getPathPoints(routeType)
  if (!points) return
  const color = ROUTE_CONFIG[routeType].color
  const count = traffic.stats[routeType] || 0
  ctx.strokeStyle = count > 0 ? color + '55' : color + '15'
  ctx.lineWidth = count > 0 ? 2 : 1
  ctx.beginPath()
  ctx.moveTo(points[0].x, points[0].y)
  for (let i = 1; i < points.length; i++) {
    ctx.lineTo(points[i].x, points[i].y)
  }
  ctx.stroke()
}

function drawParticles() {
  for (const rt of Object.keys(ROUTE_CONFIG)) {
    const count = traffic.stats[rt] || 0
    if (count === 0) continue
    const points = getPathPoints(rt)
    if (!points) continue
    const color = ROUTE_CONFIG[rt].color
    const particleCount = Math.min(count, 4)
    const speed = 0.002 + count * 0.0003
    for (let i = 0; i < particleCount; i++) {
      const t = (Date.now() * speed + i / particleCount) % 1
      const pos = pathPoint(points, t)
      const trailLen = 3
      for (let j = 0; j < trailLen; j++) {
        const tt = Math.max(0, t - j * 0.025)
        const tp = pathPoint(points, tt)
        const alpha = (1 - j / trailLen) * 0.5
        ctx.fillStyle = color + Math.floor(alpha * 255).toString(16).padStart(2, '0')
        ctx.beginPath()
        ctx.arc(tp.x, tp.y, 2 - j * 0.4, 0, Math.PI * 2)
        ctx.fill()
      }
      ctx.fillStyle = color
      ctx.beginPath()
      ctx.arc(pos.x, pos.y, 2.5, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

function buildGridCanvas(w, h) {
  gridCanvas = document.createElement('canvas')
  const dpr = window.devicePixelRatio || 1
  gridCanvas.width = w * dpr
  gridCanvas.height = h * dpr
  const gctx = gridCanvas.getContext('2d')
  gctx.scale(dpr, dpr)
  gctx.strokeStyle = 'rgba(255,255,255,0.02)'
  gctx.lineWidth = 1
  for (let x = 0; x < w; x += 40) {
    gctx.beginPath()
    gctx.moveTo(x, 0)
    gctx.lineTo(x, h)
    gctx.stroke()
  }
  for (let y = 0; y < h; y += 40) {
    gctx.beginPath()
    gctx.moveTo(0, y)
    gctx.lineTo(w, y)
    gctx.stroke()
  }
}

function drawCanvas() {
  if (!canvasRef.value || !ctx) return
  const rect = canvasRef.value.parentElement.getBoundingClientRect()
  if (rect.width === 0 || rect.height === 0) return
  // 节点布局未就绪（如首次进入画布视图前）时跳过绘制
  if (!nodes.local || !nodes.tunnel) return
  ctx.clearRect(0, 0, rect.width, rect.height)
  if (gridCanvas) {
    ctx.drawImage(gridCanvas, 0, 0, rect.width, rect.height)
  }
  for (const rt of Object.keys(ROUTE_CONFIG)) drawPath(rt)
  drawParticles()
  drawLocalNode()
  {
    drawTunnel(nodes.tunnel)
    drawCountNode(nodes.count_ipv4_warp)
    drawCountNode(nodes.count_ipv4_warp_ipv6)
    drawCountNode(nodes.count_ipv6_warp)
    drawCountNode(nodes.count_ipv6_warp_ipv4)
    drawCountNode(nodes.count_ipv4)
    drawCountNode(nodes.count_ipv6)
    drawPortNode(nodes.type_ipv4)
    drawPortNode(nodes.type_ipv6)
    drawPortNode(nodes.type_ipv4_warp)
    drawPortNode(nodes.type_ipv4_warp_ipv6)
    drawPortNode(nodes.type_ipv6_warp)
    drawPortNode(nodes.type_ipv6_warp_ipv4)
  }
}

function startCanvasAnimation() {
  const loop = () => {
    drawCanvas()
    canvasAnimId = requestAnimationFrame(loop)
  }
  canvasAnimId = requestAnimationFrame(loop)
}

function pauseCanvas() {
  if (canvasAnimId) {
    cancelAnimationFrame(canvasAnimId)
    canvasAnimId = null
  }
}

async function resumeCanvas() {
  if (traffic.subview === 'canvas' && store.activeTab === 'traffic' && !canvasAnimId) {
    // 等待 v-show 切换完成后 canvas 才有实际尺寸
    await nextTick()
    resizeCanvas()
    startCanvasAnimation()
  }
}

function showSubview(name) {
  traffic.subview = name
  if (name === 'list') {
    pauseCanvas()
  } else {
    resumeCanvas()
  }
  api()?.save_ui_prefs({ traffic_subview: name })?.catch(() => {})
}

// ===== 生命周期与联动 =====
watch(
  () => store.activeTab,
  (name) => {
    if (name === 'traffic') {
      if (traffic.autoRefresh) startAutoRefresh()
      resumeCanvas()
    } else {
      stopAutoRefresh()
      pauseCanvas()
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
    traffic.subview = store.trafficSubview || 'list'
    await nextTick()
    if (traffic.subview === 'canvas' && store.activeTab === 'traffic') resumeCanvas()
    await refreshFast()
    refreshSlow()
  },
  { immediate: true }
)

function onWindowResize() {
  resizeCanvas()
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  stopAutoRefresh()
  pauseCanvas()
  if (refreshTimer) clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="traffic-view">
    <!-- 子视图切换 -->
    <div class="subview-bar">
      <button class="subview-btn" :class="{ active: traffic.subview === 'list' }" @click="showSubview('list')">列表视图
      </button>
      <button class="subview-btn" :class="{ active: traffic.subview === 'canvas' }" @click="showSubview('canvas')">画布动画
      </button>
    </div>

    <!-- 列表视图 -->
    <div v-show="traffic.subview === 'list'" class="traffic-subview">
      <div class="tv-header">
        网络流量监控
        <span class="warp-info">WARP 底层: {{ traffic.warpUnderlay === 'ipv6' ? 'IPv6' : 'IPv4' }}</span>
      </div>
      <div class="loading-bar" :class="{ active: traffic.loadingFast }"></div>

      <div class="stats-grid">
        <div v-for="card in statCards" :key="card.key" class="stat-card">
          <div class="stat-count" :style="{ color: card.color }">{{ card.count }}</div>
          <div class="stat-label">
            <span class="stat-dot" :style="{ background: card.color }"></span>{{ card.label }}
          </div>
        </div>
      </div>

      <div class="toolbar">
        <n-input v-model:value="traffic.searchList" size="small" placeholder="搜索进程名/域名/IP..." style="max-width: 260px"
          clearable />
        <label class="auto-label">
          <n-switch v-model:value="traffic.autoRefresh" size="small" />
          自动刷新
        </label>
        <n-button size="small" :loading="traffic.loadingFast" @click="refreshFast()">刷新</n-button>
      </div>

      <div class="conn-list">
        <div v-if="!groupedListView.length" class="empty-hint">暂无活动 TCP 连接</div>
        <div v-for="group in groupedListView" :key="group.name" class="conn-group">
          <div class="conn-group-header">
            {{ group.name }}
            <span class="conn-group-count">{{ group.items.length }}</span>
          </div>
          <div v-for="(c, i) in group.items" :key="i" class="conn-item">
            <span class="conn-process">{{ c.process }}</span>
            <span class="conn-host">
              <span class="conn-hostname">{{ c.hostname || '(无域名)' }}</span>
              <span class="conn-ip mono">{{ c.remote_ip }}:{{ c.remote_port }}</span>
            </span>
            <span class="conn-badge" :style="{
              color: (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).color,
              background: (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).color + '1e',
            }">{{ (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).label }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 画布视图 -->
    <div v-show="traffic.subview === 'canvas'" class="traffic-subview">
      <div class="canvas-wrap">
        <canvas ref="canvasRef" class="flow-canvas"></canvas>
        <div class="top-stats">
          <div v-for="card in statCards" :key="card.key" class="stat-pill">
            <span class="stat-dot" :style="{ background: card.color }"></span>
            <span class="stat-num" :style="{ color: card.color }">{{ card.count }}</span>
            <span class="stat-label">{{ card.label }}</span>
          </div>
        </div>
      </div>

      <div class="bottom-panel">
        <div class="panel-header">
          <div class="panel-header-left">
            <n-checkbox :checked="allCanvasSelected" @update:checked="toggleSelectAll" title="全选" />
            <span class="panel-title">
              活动连接
              <span class="warp-info">WARP 底层: {{ traffic.warpUnderlay === 'ipv6' ? 'IPv6' : 'IPv4' }}</span>
            </span>
          </div>
          <div class="panel-controls">
            <n-input v-model:value="traffic.searchCanvas" size="small" placeholder="搜索域名/IP/进程..." style="width: 200px"
              clearable />
            <label class="auto-label">
              <n-switch :value="traffic.cumulativeMode" size="small" @update:value="toggleCumulative" />
              累计展示
            </label>
            <label class="auto-label">
              <n-switch v-model:value="traffic.autoRefresh" size="small" />
              自动刷新
            </label>
          </div>
        </div>

        <div class="action-bar" :class="{ show: selectedConns.size > 0 }">
          <span class="selected-count">已选 {{ selectedConns.size }} 项</span>
          <n-button size="tiny" @click="setRoute('ipv4')">IPv4 直连</n-button>
          <n-button size="tiny" @click="setRoute('ipv6')">IPv6 直连</n-button>
          <n-button size="tiny" type="warning" secondary @click="setRoute('warp')">不直连(WARP)</n-button>
          <n-button size="tiny" quaternary @click="clearSelection()">取消选择</n-button>
        </div>

        <div class="conn-list">
          <div v-if="!filteredCanvasConns.length" class="empty-hint">
            {{ traffic.cumulativeMode ? '暂无累计连接' : '暂无活动连接' }}
          </div>
          <div v-for="group in groupedCanvasList" :key="group.name" class="proc-group"
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
                :class="{ selected: selectedConns.has(connId(c)) }" @click="toggleConn(connId(c), !selectedConns.has(connId(c)))">
                <n-checkbox :checked="selectedConns.has(connId(c))" @click.stop
                  @update:checked="(v) => toggleConn(connId(c), v)" />
                <span class="conn-host">
                  <span class="conn-hostname">{{ c.hostname || '(无域名)' }}</span>
                  <span class="conn-ip mono">{{ c.remote_ip }}:{{ c.remote_port }}</span>
                </span>
                <span class="conn-badge" :style="{
                  color: (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).color,
                  background: (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).color + '1e',
                }">{{ (ROUTE_CONFIG[c.route_type] || ROUTE_CONFIG['ipv4']).label }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.traffic-view {
  padding: 16px 22px 24px;
  max-width: 1180px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.subview-bar {
  display: flex;
  gap: 2px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 3px;
  width: fit-content;
}

.subview-btn {
  padding: 6px 18px;
  font-size: 12px;
  color: var(--text-tertiary);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.subview-btn:hover {
  color: var(--text-primary);
}

.subview-btn.active {
  color: var(--accent);
  background: var(--accent-dim);
}

.tv-header {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.warp-info {
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-elevated);
  padding: 2px 8px;
  border-radius: 4px;
}

.loading-bar {
  height: 2px;
  border-radius: 1px;
  background: transparent;
  overflow: hidden;
  position: relative;
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

/* 统计卡片 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
}

.stat-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 13px 14px;
}

.stat-count {
  font-size: 22px;
  font-weight: 700;
  font-family: var(--font-mono);
}

.stat-label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.stat-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
}

.auto-label {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}

/* 连接列表 */
.conn-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.conn-group {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.conn-group-header {
  padding: 9px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  display: flex;
  align-items: center;
  gap: 8px;
}

.conn-group-count {
  font-size: 10px;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  padding: 1px 7px;
  border-radius: 8px;
}

.conn-item,
.conn-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  transition: background 0.15s;
}

.conn-item:hover,
.conn-row:hover {
  background: var(--bg-hover);
}

.conn-row.selected {
  background: var(--accent-dim);
}

.conn-process {
  font-size: 12px;
  font-weight: 500;
  min-width: 90px;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-host {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.conn-hostname {
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conn-ip {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conn-badge {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
  flex-shrink: 0;
}

/* 画布 */
.canvas-wrap {
  position: relative;
  height: 400px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}

.flow-canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.top-stats {
  position: absolute;
  top: 10px;
  left: 12px;
  right: 12px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  pointer-events: none;
}

.stat-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  background: rgba(19, 19, 22, 0.85);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 4px 12px;
  font-size: 11px;
}

.stat-num {
  font-weight: 700;
  font-family: var(--font-mono);
}

/* 底部面板 */
.bottom-panel {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  background: var(--bg-elevated);
}

.panel-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-controls {
  display: flex;
  align-items: center;
  gap: 14px;
}

.action-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: var(--accent-dim);
  border-top: 1px solid var(--border-strong);
  border-bottom: 1px solid var(--border-strong);
  max-height: 0;
  overflow: hidden;
  padding-top: 0;
  padding-bottom: 0;
  transition: all 0.25s ease;
}

.action-bar.show {
  max-height: 60px;
  padding-top: 8px;
  padding-bottom: 8px;
}

.selected-count {
  font-size: 12px;
  color: var(--accent);
  font-weight: 600;
  margin-right: 4px;
}

.bottom-panel .conn-list {
  padding: 8px 10px 10px;
}

.proc-group {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.proc-group-header {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 12px;
  background: var(--bg-elevated);
  cursor: pointer;
  user-select: none;
}

.collapse-icon {
  width: 10px;
  height: 10px;
  color: var(--text-tertiary);
  transition: transform 0.2s;
}

.proc-group.collapsed .collapse-icon {
  transform: rotate(-90deg);
}

.proc-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.proc-count {
  font-size: 10px;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  padding: 1px 7px;
  border-radius: 8px;
}

.proc-group-body {
  display: block;
}

.proc-group.collapsed .proc-group-body {
  display: none;
}

.conn-row {
  cursor: pointer;
}
</style>
