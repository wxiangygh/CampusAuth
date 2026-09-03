// dev-only：浏览器直开 `vite dev` 时模拟 pywebview ApiBridge，
// 无 Python 后端也能预览/调试界面（仅 serve 模式由 vite 插件注入，构建产物不含）。
const delay = (value, ms = 40) => new Promise((resolve) => setTimeout(() => resolve(value), ms))
const clone = (value) => JSON.parse(JSON.stringify(value))

// 进程图标 mock：真实环境由后端从 exe 提取 32x32 PNG 转成 data URL，
// dev 预览无 Python 后端，这里用彩色字母方块代替，仅用于验证布局。
const procIcon = (letter, color) =>
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" rx="7" fill="${color}"/><text x="16" y="23" font-family="Segoe UI,sans-serif" font-size="18" font-weight="700" fill="#fff" text-anchor="middle">${letter}</text></svg>`
  )

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
      icons: {
        'chrome.exe': procIcon('C', '#4285f4'),
        'Code.exe': procIcon('V', '#007acc'),
        'python.exe': procIcon('P', '#ffd43b'),
        'WeChat.exe': procIcon('W', '#07c160'),
        'cloudflared.exe': procIcon('F', '#f6821f'),
        'steam.exe': procIcon('S', '#1b2838'),
        'QQ.exe': procIcon('Q', '#12b7f5'),
      },
    }, 90),
  get_traffic_status_slow: (ips) =>
    delay(Object.fromEntries((ips || []).map((ip) => [ip, ip.startsWith('2') ? 'ipv6.example.net' : 'resolved.example.net'])), 300),
  set_connections_route: (items) =>
    delay({ results: (items || []).map((it) => ({ hostname: it.hostname, remote_ip: it.remote_ip, success: true })) }),

  // ===== 应用更新（设置页"检查更新"按钮验证用）=====
  get_app_info: () => delay({
    version: '1.2.5',
    install_dir: 'D:\\Apps\\CampusAuth',
    exe: 'D:\\Apps\\CampusAuth\\CampusAuth.exe',
  }),
  check_for_update: () => delay({
    available: true,
    current: '1.2.5',
    reason: 'ok',
    latest: {
      version: '1.2.6',
      tag: 'v1.2.6',
      name: 'CampusAuth v1.2.6',
      notes: '- 更新检测改为运行期间每 6 小时复查，新版本发布后无需重启即可发现\n- 设置页新增「检查更新」按钮，可随时手动检测',
      published_at: new Date().toISOString(),
      html_url: 'https://github.com/wxiangygh/CampusAuth/releases/tag/v1.2.6',
      download_url: 'https://example.invalid/CampusAuth.exe',
      size: 30000000,
    },
  }),

  // ===== WiFi 扫描（模拟真实扫描耗时，便于验证加载态与候选列表弹出）=====
  scan_wifi: () => delay([
    'CMCC_BJUT_SUSHE_H0910',
    'BJUT_SUSHE_2.4G',
    'BJUT_SUSHE_5G',
    'CMCC-kuandai',
    'CMCC_BJUT_SUSHE_H1009-5G',
  ], 2500),
}

// ===== WARP 分流规则（WarpView 子tab + "当前分流配置"悬浮窗预览）=====
let learnedDomains = ['www.bjut.edu.cn', 'jwgl.bjut.edu.cn', 'lib.bjut.edu.cn', 'mail.bjut.edu.cn', 'ehall.bjut.edu.cn']

let exclusion = {
  domains: [
    { domain: 'www.bjut.edu.cn', route: 'ipv6', enabled: true, added_at: '2026-08-30 10:12' },
    { domain: 'jwgl.bjut.edu.cn', route: 'ipv6', enabled: true, added_at: '2026-08-30 10:14' },
    { domain: 'lib.bjut.edu.cn', route: 'ipv6', enabled: true, added_at: '2026-08-30 10:15' },
    { domain: 'v.qq.com', route: 'ipv4', enabled: true, added_at: '2026-08-31 09:02' },
    { domain: 'music.163.com', route: 'ipv4', enabled: false, added_at: '2026-09-01 21:40' },
  ],
  ip_ranges: [
    { cidr: '10.0.0.0/8', route: 'ipv4', enabled: true },
    { cidr: '166.111.0.0/16', route: 'ipv4', enabled: false },
    { cidr: '2402:4e00:1430::/48', route: 'ipv6', enabled: true },
  ],
  dns_fallback: [
    { domain: 'bytedns3.com', enabled: true, added_at: '2026-08-29 08:30' },
    { domain: 'queniuyk.com', enabled: true, added_at: '2026-08-29 08:31' },
    { domain: 'queniuak.com', enabled: false, added_at: '2026-09-01 12:05' },
  ],
}

// WARP 实时状态（悬浮窗三列数据源）
let warpTunnelHosts = ['www.bjut.edu.cn', 'jwgl.bjut.edu.cn', 'lib.bjut.edu.cn', 'v.qq.com', 'music.163.com']
let warpCliRanges = {
  active_ipv6: ['2402:4e00:1430::/48', '2408:4002:10c0::/48'],
  legacy: ['203.205.255.17/32'],
}
let warpDnsFallback = ['bytedns3.com', 'queniuyk.com']

function findBy(list, key, value) {
  return list.find((it) => it[key] === value)
}

Object.assign(api, {
  get_exclusion_config: () => delay(clone(exclusion)),
  add_domain: (domain, route) => {
    exclusion.domains.push({ domain, route: route || 'ipv6', enabled: true, added_at: '2026-09-03 21:00' })
    if (route !== 'ipv4' && !warpTunnelHosts.includes(domain)) warpTunnelHosts.push(domain)
    return delay({ success: true, message: `已添加 ${domain}` })
  },
  remove_domain: (domain) => {
    exclusion.domains = exclusion.domains.filter((d) => d.domain !== domain)
    warpTunnelHosts = warpTunnelHosts.filter((d) => d !== domain)
    return delay({ success: true, message: `已删除 ${domain}` })
  },
  toggle_domain: (domain, enabled) => {
    const d = findBy(exclusion.domains, 'domain', domain)
    if (d) d.enabled = !!enabled
    return delay({ success: true, message: enabled ? '已启用' : '已禁用' })
  },
  set_domain_route: (domain, route) => {
    const d = findBy(exclusion.domains, 'domain', domain)
    if (d) d.route = route
    return delay({ success: true, message: '路由已切换' })
  },
  add_ip_range: (cidr, route) => {
    exclusion.ip_ranges.push({ cidr, route: route || 'ipv4', enabled: true })
    return delay({ success: true, message: `已添加 ${cidr}` })
  },
  remove_ip_range: (cidr) => {
    exclusion.ip_ranges = exclusion.ip_ranges.filter((r) => r.cidr !== cidr)
    return delay({ success: true, message: `已删除 ${cidr}` })
  },
  toggle_ip_range: (cidr, enabled) => {
    const r = findBy(exclusion.ip_ranges, 'cidr', cidr)
    if (r) r.enabled = !!enabled
    return delay({ success: true, message: enabled ? '已启用' : '已禁用' })
  },
  set_ip_range_route: (cidr, route) => {
    const r = findBy(exclusion.ip_ranges, 'cidr', cidr)
    if (r) r.route = route
    return delay({ success: true, message: '路由已切换' })
  },
  check_ipv6_support: () => delay({ success: true, message: '检测完成', details: [] }),
  apply_to_warp: () => delay({
    success: true,
    message: '已同步到 WARP',
    details: exclusion.domains.map((d) => ({ domain: d.domain, success: true })),
  }),
  sync_from_warp: (kind) => {
    // 按子tab分类同步：把 WARP 侧独有的条目合并进本地配置（不覆盖已有项）
    let added = 0
    if (!kind || kind === 'domain') {
      for (const host of warpTunnelHosts) {
        if (!exclusion.domains.some((d) => d.domain === host)) {
          exclusion.domains.push({ domain: host, route: 'ipv6', enabled: true, added_at: '2026-09-03 21:00' })
          added++
        }
      }
    }
    if (!kind || kind === 'dns') {
      for (const d of warpDnsFallback) {
        if (!exclusion.dns_fallback.some((x) => x.domain === d)) {
          exclusion.dns_fallback.push({ domain: d, enabled: true, added_at: '2026-09-03 21:00' })
          added++
        }
      }
    }
    if (!kind || kind === 'ip') {
      for (const cidr of warpCliRanges.active_ipv6.concat(warpCliRanges.legacy)) {
        if (!exclusion.ip_ranges.some((r) => r.cidr === cidr)) {
          exclusion.ip_ranges.push({ cidr, route: cidr.includes(':') ? 'ipv6' : 'ipv4', enabled: true })
          added++
        }
      }
    }
    return delay({
      success: true,
      message: added ? `从 WARP 同步了 ${added} 条规则` : 'WARP 与本地配置已同步，无需更新',
      details: { hosts_added: [], dns_added: [], ip_ranges_added: [] },
    })
  },
  apply_ip_ranges_to_warp: () => {
    for (const r of exclusion.ip_ranges.filter((x) => x.enabled)) {
      if (!warpCliRanges.active_ipv6.includes(r.cidr) && !warpCliRanges.legacy.includes(r.cidr)) {
        ;(r.cidr.includes(':') ? warpCliRanges.active_ipv6 : warpCliRanges.legacy).push(r.cidr)
      }
    }
    return delay({
      success: true,
      message: 'IP 范围已同步到 WARP',
      details: exclusion.ip_ranges.map((r) => ({ cidr: r.cidr, success: true })),
    })
  },
  get_warp_ranges: () => delay(clone(warpTunnelHosts)),
  get_cli_ip_ranges: () => delay(clone(warpCliRanges)),
  get_dns_fallback_list: () => delay(clone(warpDnsFallback)),
  cleanup_legacy_config: () => {
    warpCliRanges.legacy = []
    return delay({ success: true, message: '旧版规则残留已清理', details: [] })
  },
  add_dns_fallback: (domain) => {
    exclusion.dns_fallback.push({ domain, enabled: true, added_at: '2026-09-03 21:00' })
    if (!warpDnsFallback.includes(domain)) warpDnsFallback.push(domain)
    return delay({ success: true, message: `已添加 ${domain}` })
  },
  remove_dns_fallback: (domain) => {
    exclusion.dns_fallback = exclusion.dns_fallback.filter((d) => d.domain !== domain)
    warpDnsFallback = warpDnsFallback.filter((d) => d !== domain)
    return delay({ success: true, message: `已移除 ${domain}` })
  },
  toggle_dns_fallback: (domain, enabled) => {
    const d = findBy(exclusion.dns_fallback, 'domain', domain)
    if (d) d.enabled = !!enabled
    return delay({ success: true, message: enabled ? '已启用' : '已禁用' })
  },
  apply_dns_fallback_to_warp: () => delay({ success: true, message: 'DNS fallback 已同步到 WARP', details: [] }),
  is_ipv4_enabled: () => delay(false),
  set_ipv4_enabled: (enabled) => delay({ success: true, message: enabled ? 'IPv4 已启用' : 'IPv4 已禁用' }),
  get_auto_enable_ipv4: () => delay(false),
  set_auto_enable_ipv4: (enabled) => delay({ success: true }),
  start_learning: () => delay({ success: true, message: '学习模式已启动' }),
  stop_learning: () => delay({ success: true, message: '学习模式已停止' }),
  get_learned_domains: () => delay(clone(learnedDomains)),
  // "从当前连接推荐"数据（未走 WARP 的直连）
  get_traffic_status: () => delay({
    warp_underlay: 'ipv6',
    stats: { ipv4: 5, ipv6: 2, ipv4_warp: 4, ipv4_warp_ipv6: 1, ipv6_warp: 3, ipv6_warp_ipv4: 0 },
    connections: [
      { process: 'chrome.exe', hostname: '', remote_ip: '104.21.6.52', remote_port: 443, route_type: 'ipv4', is_warp: false },
      { process: 'WeChat.exe', hostname: 'szshort.weixin.qq.com', remote_ip: '101.91.34.117', remote_port: 80, route_type: 'ipv4', is_warp: false },
      { process: 'python.exe', hostname: 'mirrors.aliyun.com', remote_ip: '2408:4002:10c0::16', remote_port: 443, route_type: 'ipv6', is_warp: false },
      { process: 'steam.exe', hostname: 'api.steampowered.com', remote_ip: '23.203.216.54', remote_port: 443, route_type: 'ipv4', is_warp: false },
    ],
  }, 90),
  // 注意：刻意不提供 open_traffic_config_window / close_traffic_config_window，
  // 使浏览器 dev 预览走 window.open 弹窗回退路径，与真实环境行为可分别验证
})

// 其他视图（设置 / 流量）不是预览重点，统一空实现保持控制台干净
const NOOP_STUBS = [
  'save_exclusion_config', 'save_warp_ranges',
  'load_config', 'save_config', 'get_startup_status', 'set_auto_startup',
  'list_workflows', 'get_traffic_stats',
  'get_traffic_history', 'reset_traffic_stats', 'get_warp_status', 'warp_action',
]
for (const name of NOOP_STUBS) {
  if (!api[name]) api[name] = () => delay(null)
}

window.pywebview = { api }
console.info('[dev-mock] pywebview bridge mocked')
