import { reactive } from 'vue'
import { api } from './bridge'
import { accentRGBA } from './theme'
import { ui } from './ui'

// ===== 全局响应式状态（认证状态机 + 表单 + 网络详情）=====
export const store = reactive({
  apiReady: false,
  initDone: false,
  initError: '',
  activeTab: 'home', // home | warp | traffic

  // 认证操作状态
  authRunning: false,
  currentAction: null, // 'auth' | 'restore'
  status: { state: 'idle', title: '一键认证', subtitle: '点击下方按钮开始认证', icon: 'wifi' },
  progress: { visible: false, pct: 0, label: '' },
  authDisabled: false,
  restoreDisabled: false,

  // 设置表单（auto_save_form 契约字段；auto_startup 走独立 API）
  form: {
    wifi_name: '',
    username: '',
    password: '',
    auto_auth: false,
    auto_restore: false,
    warp_cli_path: '',
    portal_ip: '',
    portal_port: '',
    silent_startup: false,
    auto_startup: false,
    auto_check_update: true,
    auth_total_timeout: 90,
    // 主页按钮绑定的工作流：'' = 恢复按钮使用内置恢复逻辑
    auth_button_workflow: 'default_auth',
    restore_button_workflow: '',
  },
  configLoaded: false,
  configRevision: 0,

  // ===== 应用更新（GitHub Releases）=====
  update: {
    visible: false,
    version: '',
    name: '',
    notes: '',
    status: '', // downloading | downloading… | installing | error
    pct: 0,
    message: '',
    busy: false,
  },

  // 网络详情
  detail: null,
  detailCollapsed: true,
  detailUserCollapsed: false,

  // UI 偏好
  pageSize: 20,
})

let _lastStateRevision = 0

// ===== 状态指示 =====
export function setStatus(state, title, subtitle, iconKey) {
  store.status = { state, title, subtitle, icon: iconKey }
}

function showProgress(pct, label) {
  store.progress = { visible: true, pct, label }
}

function hideProgress() {
  store.progress = { visible: false, pct: 0, label: '' }
}

export function updateStatusFromCheck(status) {
  if (!status || store.authRunning) return
  hideProgress()
  if (status.status === 'connected') {
    setStatus('success', 'WARP已连接', status.message || '', 'check')
    store.authDisabled = true
    store.restoreDisabled = false
  } else if (status.status === 'partial') {
    setStatus('running', '部分连接', status.message || '', 'warn')
    store.authDisabled = false
    store.restoreDisabled = false
  } else if (status.status === 'broken') {
    setStatus('error', '网络异常', status.message || '', 'cross')
    store.authDisabled = false
    store.restoreDisabled = false
  } else if (status.status === 'normal') {
    setStatus('normal', '正常模式', status.message || '', 'check')
    store.authDisabled = false
    store.restoreDisabled = true
  } else {
    setStatus('idle', '校园网助手', '点击下方按钮开始认证', 'wifi')
    store.authDisabled = false
    store.restoreDisabled = true
  }
}

// ===== 认证操作（对应原 onAuthProgress / finishAuth 等）=====
// 显式取消后的短暂窗口：忽略后端重复推送的 cancelled 事件，避免打断紧随其后的新操作
let _ignoreCancelledUntil = 0
// 操作纪元：后端每次启动新操作分配递增 operationId，
// 旧操作滞后发出的进度/终态事件在此整体丢弃，彻底避免进度条回退
let _operationId = 0
// 同一纪元内已显示过的最大进度（单调递增守卫）
let _lastPct = 0

export function finishAuth(success, message, action, opId) {
  // 纪元守卫：只处理最新操作的终态
  if (opId !== undefined && opId !== null && opId < _operationId) return
  // 事件未带 action 时回退到纪元内记录的操作类型
  const kind = action || store.currentAction
  store.authRunning = false
  store.currentAction = null
  _lastPct = 0
  stopParticles()
  // 操作结束后立即可交互（按钮不再长时间禁用），随后由状态检查细化
  store.authDisabled = false
  store.restoreDisabled = false

  const isCancelled = message === '已取消'
  if (success) {
    setStatus(kind === 'restore' ? 'normal' : 'success', kind === 'restore' ? '恢复成功' : '认证成功', message || '', 'check')
    showProgress(100, '完成')
  } else if (isCancelled) {
    setStatus('idle', '已取消', '操作已取消', 'wifi')
    showProgress(100, '已取消')
  } else {
    setStatus('error', kind === 'restore' ? '恢复失败' : '认证失败', message || '', 'cross')
    showProgress(100, '失败')
  }

  setTimeout(() => {
    if (store.authRunning) return
    const a = api()
    if (!a) return
    a.check_network_status()
      .then((s) => {
        if (!store.authRunning) updateStatusFromCheck(s)
      })
      .catch(() => {})
  }, 1500)

  // 认证/恢复完成后刷新网络详情（WARP/IPv4 状态可能变化）
  setTimeout(() => refreshNetworkDetail(), 1800)
}

export function handleAuthProgress(data) {
  const { step, total, message, status, action, operationId } = data || {}
  // 纪元守卫：新纪元开启（后端 start_operation）时重置单调进度
  if (operationId !== undefined && operationId !== null) {
    if (operationId < _operationId) return // 旧操作滞后事件，整体丢弃
    if (operationId > _operationId) {
      _operationId = operationId
      _lastPct = 0
    }
  }
  if (status === 'success') return finishAuth(true, message, action, operationId)
  if (status === 'error') return finishAuth(false, message, action, operationId)
  if (status === 'cancelled') {
    if (!store.authRunning) return
    if (Date.now() < _ignoreCancelledUntil) return
    return finishAuth(false, message || '已取消', action, operationId)
  }

  // 单调递增守卫：同一纪元内进度只升不降（后端 total 变化等造成的回退被钳制）
  let pct = Math.round((step / total) * 100)
  if (isNaN(pct) || pct < 0) pct = 0
  if (pct < _lastPct) pct = _lastPct
  _lastPct = pct

  const label = action === 'restore' ? '恢复中' : '认证中'
  setStatus('running', label + '...', message || '', 'loader')
  showProgress(pct, label)
  startParticles()

  // 后端自动认证时前端未感知，此处进入运行态；运行期间保持按钮可点击（点击即抢占）
  if (!store.authRunning) {
    store.authRunning = true
    store.currentAction = action || 'auth'
    store.authDisabled = false
    store.restoreDisabled = false
  }
}

export function handleAppState(state) {
  if (!state || Number(state.revision || 0) <= _lastStateRevision) return
  _lastStateRevision = Number(state.revision || 0)
  if (state.config_revision) {
    store.configRevision = Math.max(store.configRevision || 0, state.config_revision)
  }
  const operation = state.operation || {}
  const opId = operation.operation_id
  // 纪元守卫：state 通道里旧纪元的操作数据整体忽略
  if (opId !== undefined && opId !== null && opId < _operationId) {
    if (!store.authRunning && state.network) updateStatusFromCheck(state.network)
    return
  }
  if (operation.status === 'running') {
    handleAuthProgress({
      step: operation.step || 0,
      total: operation.total || 1,
      message: operation.message || '正在处理...',
      status: 'running',
      action: operation.kind || 'auth',
      operationId: opId,
    })
    return
  }
  if (store.authRunning && ['success', 'error', 'cancelled'].includes(operation.status)) {
    finishAuth(operation.status === 'success', operation.message || '', operation.kind || store.currentAction, opId)
    return
  }
  if (!store.authRunning && state.network) updateStatusFromCheck(state.network)
}

// 操作抢占：有操作进行时点击新按钮 = 停止旧操作并立即执行最新操作
export async function startAuth() {
  if (store.authRunning) {
    ui.toast('已停止当前操作，开始认证')
    await cancelOperation()
  }
  store.authRunning = true
  store.currentAction = 'auth'
  store.authDisabled = false
  store.restoreDisabled = false
  _operationId += 1 // 前端预占纪元：后端 start_operation 会再分配并覆盖
  _lastPct = 0
  setStatus('running', '认证中...', '正在准备...', 'loader')
  showProgress(0, '认证中')
  startParticles()
  try {
    await api().test_auth()
  } catch (e) {
    finishAuth(false, e.message, 'auth')
  }
}

export async function startRestore() {
  if (store.authRunning) {
    ui.toast('已停止当前操作，开始恢复网络')
    await cancelOperation()
  }
  store.authRunning = true
  store.currentAction = 'restore'
  store.authDisabled = false
  store.restoreDisabled = false
  _operationId += 1 // 前端预占纪元：后端 start_operation 会再分配并覆盖
  _lastPct = 0
  setStatus('running', '恢复中...', '正在恢复正常网络模式...', 'loader')
  showProgress(0, '恢复中')
  startParticles()
  try {
    await api().restore_network()
  } catch (e) {
    finishAuth(false, e.message, 'restore')
  }
}

export async function cancelOperation() {
  _ignoreCancelledUntil = Date.now() + 2500
  try {
    await api().cancel_operation()
  } catch (e) {
    console.error('cancel failed:', e)
  }
  finishAuth(false, '已取消', store.currentAction)
}

// ===== 主页粒子动画 =====
let animRunning = false
let particles = []
let particleCtx = null

class Particle {
  constructor() {
    this.reset()
  }
  reset() {
    this.angle = Math.random() * Math.PI * 2
    this.radius = 60 + Math.random() * 50
    this.speed = 0.005 + Math.random() * 0.01
    this.size = 0.5 + Math.random() * 1.5
    this.opacity = 0.1 + Math.random() * 0.4
    this.cx = 150
    this.cy = 150
  }
  update() {
    this.angle += this.speed
  }
  draw(ctx) {
    const x = this.cx + Math.cos(this.angle) * this.radius
    const y = this.cy + Math.sin(this.angle) * this.radius
    ctx.beginPath()
    ctx.arc(x, y, this.size, 0, Math.PI * 2)
    ctx.fillStyle = accentRGBA(this.opacity)
    ctx.fill()
  }
}

function animateParticles() {
  if (!animRunning) return
  if (particleCtx) {
    particleCtx.clearRect(0, 0, 300, 300)
    particles.forEach((p) => {
      p.update()
      p.draw(particleCtx)
    })
  }
  requestAnimationFrame(animateParticles)
}

export function setParticleCanvas(canvas) {
  particleCtx = canvas ? canvas.getContext('2d') : null
}

export function startParticles() {
  if (animRunning) return
  animRunning = true
  particles = []
  for (let i = 0; i < 30; i++) particles.push(new Particle())
  animateParticles()
}

export function stopParticles() {
  animRunning = false
  if (particleCtx) particleCtx.clearRect(0, 0, 300, 300)
}

// ===== 表单自动保存（auto_save_form 契约）=====
let _saveSerial = Promise.resolve()

export function collectFormConfig() {
  const f = store.form
  return {
    wifi_name: String(f.wifi_name || '').trim(),
    username: String(f.username || '').trim(),
    password: f.password || '',
    auto_auth: !!f.auto_auth,
    auto_restore: !!f.auto_restore,
    warp_cli_path: String(f.warp_cli_path || '').trim(),
    portal_ip: String(f.portal_ip || '').trim(),
    portal_port: String(f.portal_port || '').trim(),
    silent_startup: !!f.silent_startup,
    auto_check_update: f.auto_check_update !== false,
    auth_total_timeout: Number(f.auth_total_timeout || 90),
    auth_button_workflow: String(f.auth_button_workflow || 'default_auth'),
    restore_button_workflow: String(f.restore_button_workflow || ''),
  }
}

export function doAutoSave() {
  if (!store.configLoaded) return
  const config = collectFormConfig()
  _saveSerial = _saveSerial
    .catch(() => null)
    .then(async () => {
      try {
        const result = await api().auto_save_form(config)
        if (result && result.revision) store.configRevision = result.revision
        if (result && result.success === false) {
          console.error('Auto-save failed:', result.message)
        }
      } catch (e) {
        console.error('Auto-save failed:', e)
      }
    })
  return _saveSerial
}

// ===== 应用更新 =====
// 更新弹窗由侧边栏底部弹出，展示 Release 说明；确认后下载并覆盖安装，
// 安装阶段后端会退出应用，由更新脚本完成替换（配置文件保留）。
let _updatePollTimer = null

export function openUpdate(release) {
  if (!release) return
  store.update = {
    visible: true,
    version: release.version || '',
    name: release.name || '',
    notes: release.notes || '',
    status: '',
    pct: 0,
    message: '',
    busy: false,
  }
}

export function closeUpdate() {
  store.update.visible = false
}

function stopUpdatePolling() {
  if (_updatePollTimer) {
    clearInterval(_updatePollTimer)
    _updatePollTimer = null
  }
}

// 这几种 reason 表示"请求没打通"，而不是"确实没有新版本"，需要重试
const UPDATE_CHECK_FAILURES = ['network', 'timeout', 'error']

/**
 * 检查更新。返回是否成功拿到检测结果：
 * - true ：确实拿到了 GitHub 的响应（可能是有新版本，也可能已是最新）
 * - false：请求没打通（无网络 / 超时 / 限流 / 接口异常），调用方可以据此安排重试
 */
export async function checkForUpdate() {
  const a = api()
  if (!a) return false
  try {
    const result = await a.check_for_update()
    if (result && result.available && result.latest) openUpdate(result.latest)
    if (!result || UPDATE_CHECK_FAILURES.includes(result.reason)) return false
    return true
  } catch (e) {
    // 检测失败静默处理，不影响正常使用
    console.debug('check_for_update failed:', e)
    return false
  }
}

export async function startUpdate() {
  const a = api()
  if (!a || store.update.busy) return
  store.update.busy = true
  store.update.status = 'downloading'
  store.update.pct = 0
  store.update.message = '正在准备下载…'
  try {
    const result = await a.install_update()
    if (result && result.success === false) {
      store.update.busy = false
      store.update.status = 'error'
      store.update.message = result.message || '更新失败'
      return
    }
  } catch (e) {
    store.update.busy = false
    store.update.status = 'error'
    store.update.message = '更新失败：' + e.message
    return
  }
  // 轮询进度：下载完成并由脚本接管后应用会退出，届时轮询自然失效
  stopUpdatePolling()
  _updatePollTimer = setInterval(async () => {
    try {
      const progress = await api().get_update_progress()
      if (!progress) return
      store.update.pct = progress.pct || 0
      store.update.status = progress.status || store.update.status
      if (progress.message) store.update.message = progress.message
      if (progress.status === 'error') {
        stopUpdatePolling()
        store.update.busy = false
      }
    } catch (e) {
      // 应用正在退出，忽略
    }
  }, 400)
}

// ===== 网络详情 =====
export async function refreshNetworkDetail() {
  const a = api()
  if (!a) return
  try {
    const data = await a.get_network_detail()
    const hasData = data && (data.ipv4 || data.ipv6 || data.mac || data.wifi_ssid)
    if (!hasData) {
      store.detail = null
      store.detailCollapsed = true
      return
    }
    store.detail = data
    store.detailCollapsed = !!store.detailUserCollapsed
  } catch (e) {
    console.error('refreshNetworkDetail failed:', e)
    store.detail = null
    store.detailCollapsed = true
  }
}
