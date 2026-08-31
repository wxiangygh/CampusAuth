// dev-only：浏览器直开 `vite dev` 时模拟 pywebview ApiBridge，
// 无 Python 后端也能预览/调试界面（仅 serve 模式由 vite 插件注入，构建产物不含）。
const delay = (value, ms = 40) => new Promise((resolve) => setTimeout(() => resolve(value), ms))
const clone = (value) => JSON.parse(JSON.stringify(value))

const CATALOG = [
  { id: 'ensure_wifi', name: '检查并连接目标 WiFi', description: '确认目标 SSID，必要时发起 WiFi 连接。', group: '基础网络', default_timeout: 15, default_retries: 1 },
  { id: 'detect_warp_state', name: '检测 WARP 当前状态', description: '已连接时跳过重复认证准备步骤。', group: '基础网络', default_timeout: 8, default_retries: 0 },
  { id: 'refresh_status', name: '刷新网络状态', description: '重新探测校园网认证状态。', group: '基础网络', default_timeout: 10, default_retries: 0 },
  { id: 'prepare_network', name: '[复合] 准备认证网络', description: '断开 WARP 并整理 IPv4/IPv6 环境。', group: '认证', default_timeout: 20, default_retries: 1 },
  { id: 'portal_auth', name: 'Portal 网页认证', description: '向认证门户提交账号密码。', group: '认证', default_timeout: 15, default_retries: 2 },
  { id: 'verify_auth', name: '验证认证结果', description: '探测公网可达性确认认证成功。', group: '认证', default_timeout: 15, default_retries: 1 },
  { id: 'disconnect_warp', name: '断开 Cloudflare WARP', description: '只断开 WARP 连接，不停止或禁用系统服务。', group: 'Cloudflare WARP', default_timeout: 10, default_retries: 0 },
  { id: 'connect_warp', name: '连接 Cloudflare WARP', description: '启动并连接 WARP，等待隧道建立。', group: 'Cloudflare WARP', default_timeout: 30, default_retries: 2 },
]

let workflows = [
  {
    id: 'default_auth',
    name: '默认认证',
    built_in: true,
    customized: false,
    tray_menu: true,
    steps: [
      { id: 'ensure_wifi', enabled: true, retries: 1, timeout: 15, retry_delay: 1, continue_on_error: false },
      { id: 'prepare_network', enabled: true, retries: 1, timeout: 20, retry_delay: 1, continue_on_error: false },
      { id: 'portal_auth', enabled: true, retries: 2, timeout: 15, retry_delay: 1.5, continue_on_error: false },
      { id: 'verify_auth', enabled: true, retries: 1, timeout: 15, retry_delay: 1, continue_on_error: false },
      { id: 'connect_warp', enabled: false, retries: 2, timeout: 30, retry_delay: 2, continue_on_error: true },
    ],
  },
  {
    id: 'warp_only',
    name: '仅重连 WARP',
    built_in: false,
    tray_menu: true,
    steps: [
      { id: 'disconnect_warp', enabled: true, retries: 0, timeout: 10, retry_delay: 1, continue_on_error: false },
      { id: 'connect_warp', enabled: true, retries: 2, timeout: 30, retry_delay: 2, continue_on_error: false },
    ],
  },
]

let activeId = 'default_auth'
let revision = 1
let autoTune = false

// 模拟各节点的累计运行统计：覆盖绿/橙/红三档与"无数据"状态
const STATS = {
  default_auth: {
    ensure_wifi: { runs: 42, avg_elapsed: 1.85, avg_retries: 0.05, score: 93, suggested_timeout: null, suggested_retries: null },
    prepare_network: { runs: 42, avg_elapsed: 6.4, avg_retries: 0.31, score: 72, suggested_timeout: null, suggested_retries: null },
    portal_auth: { runs: 42, avg_elapsed: 9.8, avg_retries: 0.64, score: 55, suggested_timeout: 22.5, suggested_retries: null },
    verify_auth: { runs: 41, avg_elapsed: 11.2, avg_retries: 1.37, score: 34, suggested_timeout: 28, suggested_retries: 2 },
    connect_warp: { runs: 6, avg_elapsed: 18.6, avg_retries: 1.83, score: 21, suggested_timeout: 45, suggested_retries: 3 },
  },
  warp_only: {
    disconnect_warp: { runs: 11, avg_elapsed: 2.3, avg_retries: 0, score: 97, suggested_timeout: null, suggested_retries: null },
  },
}

function snapshot() {
  return { workflows: clone(workflows), active_workflow_id: activeId, revision: ++revision }
}

const api = {
  get_ui_prefs: () => delay({}),
  save_ui_prefs: () => delay({}),
  get_workflow_catalog: () =>
    delay({ steps: clone(CATALOG), workflows: clone(workflows), active_workflow_id: activeId }),
  select_workflow: (id) => {
    activeId = id
    return delay({ success: true, ...snapshot() })
  },
  save_workflow: (steps, id, name) => {
    const wf = workflows.find((item) => item.id === id)
    if (wf) {
      wf.steps = clone(steps || [])
      if (name) wf.name = name
      if (wf.built_in) wf.customized = true
    }
    return delay({ success: true, workflow: clone(wf), ...snapshot() })
  },
  save_workflow_as: (name, steps, trayMenu) => {
    const wf = { id: 'wf_' + Date.now(), name, built_in: false, tray_menu: trayMenu !== false, steps: clone(steps || []) }
    workflows.push(wf)
    activeId = wf.id
    return delay({ success: true, workflow: clone(wf), ...snapshot() })
  },
  update_workflow_meta: (id, name, trayMenu) => {
    const wf = workflows.find((item) => item.id === id)
    if (wf) {
      if (name) wf.name = name
      wf.tray_menu = !!trayMenu
    }
    return delay({ success: true, ...snapshot() })
  },
  delete_workflow: (id) => {
    workflows = workflows.filter((item) => item.id !== id)
    if (activeId === id) activeId = workflows[0]?.id || 'default_auth'
    return delay({ success: true, message: '已删除', ...snapshot() })
  },
  reset_workflow: (id) => {
    const wf = workflows.find((item) => item.id === id)
    if (wf) wf.customized = false
    return delay({ success: true, workflow: clone(wf), ...snapshot() })
  },
  get_workflow_stats: (id) =>
    delay({ workflow_id: id, auto_tune: autoTune, steps: clone(STATS[id] || {}) }, 80),
  set_workflow_auto_tune: (enabled) => {
    autoTune = !!enabled
    return delay({ success: true, auto_tune: autoTune, revision: ++revision })
  },
  apply_workflow_tuning: (id) => {
    const wfId = id || activeId
    const wf = workflows.find((item) => item.id === wfId)
    const stats = STATS[wfId] || {}
    const changes = []
    for (const step of wf?.steps || []) {
      const st = stats[step.id]
      if (!st) continue
      const t = st.suggested_timeout != null && st.suggested_timeout !== step.timeout
      const r = st.suggested_retries != null && st.suggested_retries !== step.retries
      if (!t && !r) continue
      changes.push({
        id: step.id,
        timeout_from: step.timeout ?? 15, timeout: t ? st.suggested_timeout : (step.timeout ?? 15),
        retries_from: step.retries ?? 0, retries: r ? st.suggested_retries : (step.retries ?? 0),
      })
      if (t) step.timeout = st.suggested_timeout
      if (r) step.retries = st.suggested_retries
    }
    return delay({ success: true, workflow_id: wfId, changed: changes.length > 0, changes, revision: ++revision })
  },
  auto_save_form: () => delay({ success: true, revision: ++revision }),
  check_network_status: () => delay({ status: 'idle' }),
  get_network_detail: () => delay(null),
  get_traffic_status_fast: () =>
    delay({
      warp_underlay: 'ipv4',
      stats: { ipv4: 5, ipv6: 2, ipv4_warp: 4, ipv4_warp_ipv6: 1, ipv6_warp: 3, ipv6_warp_ipv4: 0 },
      connections: [
        { process: 'chrome.exe', hostname: 'www.google.com', remote_ip: '142.250.190.36', remote_port: 443, route_type: 'ipv4_warp', is_warp: true },
        { process: 'chrome.exe', hostname: 'github.com', remote_ip: '140.82.114.4', remote_port: 443, route_type: 'ipv4_warp', is_warp: true },
        { process: 'chrome.exe', hostname: 'v2ex.com', remote_ip: '104.21.6.52', remote_port: 443, route_type: 'ipv4', is_warp: false },
        { process: 'Code.exe', hostname: 'update.code.visualstudio.com', remote_ip: '13.107.42.14', remote_port: 443, route_type: 'ipv4', is_warp: false },
        { process: 'Code.exe', hostname: '', remote_ip: '2620:1ec:21::14', remote_port: 443, route_type: 'ipv6', is_warp: false },
        { process: 'python.exe', hostname: 'pypi.org', remote_ip: '151.101.0.223', remote_port: 443, route_type: 'ipv4_warp_ipv6', is_warp: true },
        { process: 'python.exe', hostname: 'mirrors.aliyun.com', remote_ip: '2408:4002:10c0::16', remote_port: 443, route_type: 'ipv6', is_warp: false },
        { process: 'WeChat.exe', hostname: 'szshort.weixin.qq.com', remote_ip: '101.91.34.117', remote_port: 80, route_type: 'ipv4', is_warp: false },
        { process: 'WeChat.exe', hostname: 'dns.weixin.qq.com', remote_ip: '203.205.255.17', remote_port: 53, route_type: 'ipv6_warp', is_warp: true },
        { process: 'cloudflared.exe', hostname: 'argotunnel.com', remote_ip: '162.159.137.17', remote_port: 7844, route_type: 'ipv6_warp', is_warp: true },
        { process: 'cloudflared.exe', hostname: '', remote_ip: '2606:4700::6810:cf9', remote_port: 7844, route_type: 'ipv6_warp', is_warp: true },
        { process: 'steam.exe', hostname: 'api.steampowered.com', remote_ip: '23.203.216.54', remote_port: 443, route_type: 'ipv4', is_warp: false },
        { process: 'steam.exe', hostname: 'media.steampowered.com', remote_ip: '23.203.216.88', remote_port: 443, route_type: 'ipv4_warp', is_warp: true },
        { process: 'steam.exe', hostname: 'cdn.cloudflare.steamstatic.com', remote_ip: '104.18.30.182', remote_port: 443, route_type: 'ipv4_warp', is_warp: true },
        { process: 'QQ.exe', hostname: 'qzs.qq.com', remote_ip: '119.147.235.122', remote_port: 443, route_type: 'ipv4', is_warp: false },
      ],
    }, 90),
  get_traffic_status_slow: (ips) =>
    delay(Object.fromEntries((ips || []).map((ip) => [ip, ip.startsWith('2') ? 'ipv6.example.net' : 'resolved.example.net'])), 300),
  set_connections_route: (items) =>
    delay({ results: (items || []).map((it) => ({ hostname: it.hostname, remote_ip: it.remote_ip, success: true })) }),
}

// 其他视图（设置 / WARP / 流量）不是预览重点，统一空实现保持控制台干净
const NOOP_STUBS = [
  'get_exclusion_config', 'save_exclusion_config', 'get_warp_ranges', 'save_warp_ranges',
  'load_config', 'save_config', 'get_startup_status', 'set_auto_startup',
  'get_app_info', 'list_workflows', 'check_for_update', 'get_traffic_stats',
  'get_traffic_history', 'reset_traffic_stats', 'get_warp_status', 'warp_action',
]
for (const name of NOOP_STUBS) {
  if (!api[name]) api[name] = () => delay(null)
}

window.pywebview = { api }
console.info('[dev-mock] pywebview bridge mocked')
