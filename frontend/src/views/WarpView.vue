<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { NButton, NInput, NSelect, NCheckbox, NTag, NPagination } from 'naive-ui'
import { api } from '../bridge'
import { store } from '../store'
import { ui } from '../ui'
import { fuzzyMatch, paginate } from '../utils'

// ===== 状态 =====
const subtab = ref('domain')

// 学习模式
const learning = reactive({
  active: false,
  badge: '未启动',
  status: '开启学习模式后，请正常访问需要排除的网站。系统会监控DNS缓存，记录访问的域名，供您选择排除。',
  domains: [],
  keyword: '',
})
const checkedDomains = reactive(new Set())
const learnedPage = reactive({ page: 1, pageSize: store.pageSize || 20 })

// 排除规则
const rules = reactive({
  list: [],
  keyword: '',
  page: 1,
  pageSize: store.pageSize || 20,
})
const domainInput = ref('')
const domainRoute = ref('ipv6')

// IP 排除
const ipRanges = ref([])
const ipInput = ref('')
const ipPrefix = ref('auto')
const ipRoute = ref('ipv4')
const recommend = ref(null)
const recommendLoading = ref(false)

// 网络方案（启用/禁用 IPv4、认证后自动启用 IPv4）已移除：
// 工作流提供 enable_ipv4 / disable_ipv4 等节点可实现相同功能

// WARP 当前规则
const warpDomainRanges = ref([])
const warpIpv6Ranges = ref([])
const warpLegacyRanges = ref([])

// DNS Fallback
const dnsList = ref([])
const warpDnsList = ref([])
const dnsInput = ref('')

// ===== 计算 =====
const filteredLearned = computed(() => {
  const kw = learning.keyword.trim()
  return kw ? learning.domains.filter((d) => fuzzyMatch(d, kw)) : learning.domains
})

const pagedLearned = computed(() => paginate(filteredLearned.value, learnedPage.page, learnedPage.pageSize))

const filteredRules = computed(() => {
  const kw = rules.keyword.trim()
  return kw ? rules.list.filter((d) => fuzzyMatch(d.domain || '', kw)) : rules.list
})

const pagedRules = computed(() => paginate(filteredRules.value, rules.page, rules.pageSize))

const routeOptions = [
  { label: '走 IPv6 校园网', value: 'ipv6' },
  { label: '走 IPv4 校园网', value: 'ipv4' },
]

const ipRouteOptions = [
  { label: '走 IPv4 校园网', value: 'ipv4' },
  { label: '走 IPv6 校园网', value: 'ipv6' },
]

// IP 前缀选项根据输入内容动态变化
const prefixOptions = computed(() => {
  const ipPart = String(ipInput.value || '').trim().split('/')[0]
  const isIpv6 = ipPart.includes(':')
  const opts = [{ label: '自动前缀', value: 'auto' }]
  if (isIpv6) {
    ;[128, 64, 48, 32].forEach((p) => opts.push({ label: `/${p}`, value: String(p) }))
  } else if (ipPart && /^\d+\.\d+\.\d+\.\d+$/.test(ipPart)) {
    ;[32, 24, 16, 8].forEach((p) => opts.push({ label: `/${p}`, value: String(p) }))
  }
  return opts
})

function buildCidr(ipOrCidr, prefixLen) {
  if (prefixLen === 'auto' || !prefixLen) {
    if (ipOrCidr.includes('/')) return ipOrCidr
    return ipOrCidr.includes(':') ? ipOrCidr + '/128' : ipOrCidr + '/32'
  }
  const ipPart = ipOrCidr.split('/')[0]
  return `${ipPart}/${prefixLen}`
}

// ===== 学习模式 =====
let learnTimer = null
let wasAutoBeforeHide = false

async function startLearning() {
  ui.showLoading('启动学习模式...')
  try {
    const r = await api().start_learning()
    if (r.success) {
      learning.active = true
      checkedDomains.clear()
      learning.badge = '学习中'
      learning.status = r.message
      ui.toast(r.message, 'success')
      startAutoRefreshLearned()
    } else {
      ui.toast(r.message, 'error')
    }
  } catch (e) {
    ui.toast('启动失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function stopLearning() {
  ui.showLoading('停止学习...')
  try {
    stopAutoRefreshLearned()
    const r = await api().stop_learning()
    learning.active = false
    learning.badge = '已完成'
    learning.status = r.message
    await refreshLearnedDomains()
    ui.toast(r.message, 'success')
  } catch (e) {
    ui.toast('停止失败: ' + e, 'error')
  }
  ui.hideLoading()
}

function startAutoRefreshLearned() {
  stopAutoRefreshLearned()
  learnTimer = setInterval(() => refreshLearnedDomains(), 3000)
}

function stopAutoRefreshLearned() {
  if (learnTimer) {
    clearInterval(learnTimer)
    learnTimer = null
  }
}

async function refreshLearnedDomains() {
  try {
    learning.domains = (await api().get_learned_domains()) || []
    if (!learning.keyword.trim()) learning.badge = learning.active ? '学习中' : `${learning.domains.length} 个域名`
  } catch (e) {
    console.error('refreshLearned error:', e)
  }
}

function onDomainCheck(domain, checked) {
  if (checked) checkedDomains.add(domain)
  else checkedDomains.delete(domain)
}

const pageAllChecked = computed(() => pagedLearned.value.length > 0 && pagedLearned.value.every((d) => checkedDomains.has(d)))

function toggleSelectAll() {
  const items = pagedLearned.value
  if (!items.length) return
  const allChecked = items.every((d) => checkedDomains.has(d))
  items.forEach((d) => {
    if (allChecked) checkedDomains.delete(d)
    else checkedDomains.add(d)
  })
}

async function addSelectedDomains() {
  const selected = pagedLearned.value.filter((d) => checkedDomains.has(d))
  if (!selected.length) {
    ui.toast('请至少选择一个域名', 'error')
    return
  }
  const route = domainRoute.value
  ui.showLoading(`正在添加 ${selected.length} 个域名...`)
  let okCount = 0
  let failCount = 0
  for (const domain of selected) {
    try {
      const r = await api().add_domain(domain, route)
      if (r.success) okCount++
      else failCount++
    } catch (e) {
      failCount++
    }
  }
  ui.hideLoading()
  ui.toast(`添加完成: ${okCount} 成功, ${failCount} 失败`, failCount ? 'error' : 'success')
  checkedDomains.clear()
  await loadRules()
}

// ===== 域名排除规则 =====
async function loadRules() {
  try {
    const cfg = await api().get_exclusion_config()
    rules.list = cfg.domains || []
    ipRanges.value = cfg.ip_ranges || []
  } catch (e) {
    console.error('loadRules error:', e)
  }
}

async function addDomainManually() {
  const domain = domainInput.value.trim()
  if (!domain) {
    ui.toast('请输入域名', 'error')
    return
  }
  ui.showLoading('正在添加域名...')
  try {
    const r = await api().add_domain(domain, domainRoute.value)
    if (r.success) {
      domainInput.value = ''
      ui.toast(r.message, 'success')
      await loadRules()
    } else {
      ui.toast(r.message, 'error')
    }
  } catch (e) {
    ui.toast('添加失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function setDomainRoute(domain, route) {
  ui.showLoading('切换路由中...')
  try {
    const r = await api().set_domain_route(domain, route)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadRules()
    await loadWarpRanges()
  } catch (e) {
    ui.toast('切换失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function removeDomain(domain) {
  const ok = await ui.confirm(`确定删除 ${domain} 的排除规则？`)
  if (!ok) return
  ui.showLoading('删除中...')
  try {
    const r = await api().remove_domain(domain)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadRules()
    await loadWarpRanges()
  } catch (e) {
    ui.toast('删除失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function toggleDomain(domain, enabled) {
  try {
    const r = await api().toggle_domain(domain, enabled)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadRules()
    await loadWarpRanges()
  } catch (e) {
    ui.toast('操作失败: ' + e, 'error')
  }
}

async function applyAllToWarp() {
  ui.showLoading('同步规则到WARP...')
  try {
    const r = await api().apply_to_warp()
    const details = r.details || []
    const okCount = details.filter((d) => d.success).length
    const failCount = details.length - okCount
    ui.toast(`${r.message} (${okCount}成功/${failCount}失败)`, failCount ? 'error' : 'success')
    await loadWarpRanges()
  } catch (e) {
    ui.toast('同步失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function syncFromWarp() {
  ui.showLoading('从WARP同步规则...')
  try {
    const r = await api().sync_from_warp()
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadRules()
    await loadDnsFallbackRules()
  } catch (e) {
    ui.toast('同步失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function checkIpv6Support() {
  ui.showLoading('正在检测 IPv6 支持...')
  try {
    const r = await api().check_ipv6_support()
    if (r.success) {
      let detailMsg = r.message
      if (r.details && r.details.length) {
        const downgraded = r.details.filter((d) => d.action === 'downgraded')
        const kept = r.details.filter((d) => d.action === 'kept')
        if (downgraded.length) {
          detailMsg += '\n\n已降级（无 IPv6）:\n' + downgraded.map((d) => `  • ${d.domain}`).join('\n')
        }
        if (kept.length) {
          detailMsg += `\n\n支持 IPv6（保持）:\n` + kept.map((d) => `  • ${d.domain}`).join('\n')
        }
      }
      ui.toast(r.message, 'success')
      ui.alert(detailMsg, 'IPv6 支持检测')
      await loadRules()
    } else {
      ui.toast(r.message, 'error')
    }
  } catch (e) {
    ui.toast('检测失败: ' + e, 'error')
  }
  ui.hideLoading()
}

// ===== IP 范围管理 =====
async function loadIpRanges() {
  try {
    const cfg = await api().get_exclusion_config()
    ipRanges.value = cfg.ip_ranges || []
  } catch (e) {
    console.error('loadIpRanges error:', e)
  }
}

async function addIpRangeManually() {
  const rawValue = ipInput.value.trim()
  if (!rawValue) {
    ui.toast('请输入 IP 或 CIDR', 'error')
    return
  }
  const cidr = buildCidr(rawValue, ipPrefix.value)
  ui.showLoading('正在添加 IP 范围...')
  try {
    const r = await api().add_ip_range(cidr, ipRoute.value)
    if (r.success) {
      ipInput.value = ''
      ui.toast(r.message, 'success')
      await loadIpRanges()
    } else {
      ui.toast(r.message, 'error')
    }
  } catch (e) {
    ui.toast('添加失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function removeIpRange(cidr) {
  const ok = await ui.confirm(`确定删除 ${cidr} 的 IP 排除规则？`)
  if (!ok) return
  ui.showLoading('删除中...')
  try {
    const r = await api().remove_ip_range(cidr)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadIpRanges()
  } catch (e) {
    ui.toast('删除失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function toggleIpRange(cidr, enabled) {
  try {
    const r = await api().toggle_ip_range(cidr, enabled)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadIpRanges()
  } catch (e) {
    ui.toast('操作失败: ' + e, 'error')
  }
}

async function setIpRangeRoute(cidr, route) {
  ui.showLoading('切换路由中...')
  try {
    const r = await api().set_ip_range_route(cidr, route)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadIpRanges()
  } catch (e) {
    ui.toast('切换失败: ' + e, 'error')
  }
  ui.hideLoading()
}

// ===== 从当前连接推荐 =====
const recommendItems = computed(() => {
  const data = recommend.value
  if (!data || !data.connections || !data.connections.length) return []
  const directConns = data.connections.filter((c) => !c.is_warp)
  const ipMap = new Map()
  for (const c of directConns) {
    if (!ipMap.has(c.remote_ip)) {
      ipMap.set(c.remote_ip, { ip: c.remote_ip, process: c.process, hostname: c.hostname || '', route_type: c.route_type })
    }
  }
  return Array.from(ipMap.values())
})

async function loadTrafficForExclude() {
  recommendLoading.value = true
  try {
    recommend.value = await api().get_traffic_status()
  } catch (e) {
    recommend.value = null
  }
  recommendLoading.value = false
}

async function addRecommendedCidr(cidr, route) {
  ui.showLoading('正在添加排除规则...')
  try {
    const r = await api().add_ip_range(cidr, route)
    ui.toast(r.message, r.success ? 'success' : 'error')
    if (r.success) await loadIpRanges()
  } catch (e) {
    ui.toast('添加失败: ' + e, 'error')
  }
  ui.hideLoading()
}

// ===== IPv4 启用/禁用 =====
async function loadIpv4Status() {
  try {
    ipv4Enabled.value = await api().is_ipv4_enabled()
    ipv4Status.value = ipv4Enabled.value ? '已启用' : '已禁用'
  } catch (e) {
    ipv4Status.value = '检测失败'
  }
}

async function toggleIpv4(enabled) {
  ipv4Status.value = enabled ? '启用中...' : '禁用中...'
  try {
    const r = await api().set_ipv4_enabled(enabled)
    if (r.success) {
      ui.toast(r.message, 'success')
    } else {
      ui.toast(r.message, 'error')
      ipv4Enabled.value = !enabled
    }
  } catch (e) {
    ui.toast('操作失败: ' + e, 'error')
    ipv4Enabled.value = !enabled
  }
  await loadIpv4Status()
}

async function loadAutoEnableIpv4() {
  try {
    autoEnableIpv4.value = await api().get_auto_enable_ipv4()
    autoEnableIpv4Status.value = autoEnableIpv4.value ? '已开启' : '已关闭'
  } catch (e) {
    autoEnableIpv4Status.value = '检测失败'
  }
}

async function toggleAutoEnableIpv4(enabled) {
  try {
    const r = await api().set_auto_enable_ipv4(enabled)
    if (r.success) {
      ui.toast(enabled ? '已开启认证后自动启用 IPv4' : '已关闭认证后自动启用 IPv4', 'success')
    } else {
      ui.toast(r.message, 'error')
      autoEnableIpv4.value = !enabled
    }
  } catch (e) {
    ui.toast('操作失败: ' + e, 'error')
    autoEnableIpv4.value = !enabled
  }
  await loadAutoEnableIpv4()
}

// ===== WARP 当前规则 =====
async function loadWarpRanges() {
  try {
    const ranges = await api().get_warp_ranges()
    warpDomainRanges.value = ranges || []
    try {
      const ipInfo = await api().get_cli_ip_ranges()
      warpIpv6Ranges.value = ipInfo.active_ipv6 || []
      warpLegacyRanges.value = ipInfo.legacy || []
    } catch (e) {
      warpIpv6Ranges.value = []
      warpLegacyRanges.value = []
    }
  } catch (e) {
    console.error('loadWarpRanges error:', e)
  }
}

async function cleanupLegacyRules() {
  const ok = await ui.confirm(
    '确定清理旧版IP/CIDR排除规则残留？\n\n将执行：\n1. 删除WARP中所有CLI添加的IP排除规则\n2. 清理配置文件中的ips/cidrs/mode等旧字段\n3. 将配置中的域名以域名排除方式重新应用到WARP'
  )
  if (!ok) return
  ui.showLoading('清理旧版规则残留中...')
  try {
    const r = await api().cleanup_legacy_config()
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadRules()
    await loadWarpRanges()
  } catch (e) {
    ui.toast('清理失败: ' + e, 'error')
  }
  ui.hideLoading()
}

// ===== DNS Fallback =====
async function loadDnsFallbackRules() {
  try {
    const cfg = await api().get_exclusion_config()
    dnsList.value = cfg.dns_fallback || []
    await loadWarpDnsFallback()
  } catch (e) {
    console.error('loadDnsFallbackRules error:', e)
  }
}

async function loadWarpDnsFallback() {
  try {
    warpDnsList.value = (await api().get_dns_fallback_list()) || []
  } catch (e) {
    console.error('loadWarpDnsFallback error:', e)
  }
}

async function addDnsFallbackManually() {
  const domain = dnsInput.value.trim().toLowerCase()
  if (!domain) {
    ui.toast('请输入域名', 'error')
    return
  }
  if (!domain.includes('.')) {
    ui.toast('域名格式无效', 'error')
    return
  }
  ui.showLoading('添加 DNS fallback 域名中...')
  try {
    const r = await api().add_dns_fallback(domain)
    ui.toast(r.message, r.success ? 'success' : 'error')
    if (r.success) {
      dnsInput.value = ''
      await loadDnsFallbackRules()
    }
  } catch (e) {
    ui.toast('添加失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function removeDnsFallback(domain) {
  const ok = await ui.confirm(`确定从 DNS fallback 移除 ${domain}？`)
  if (!ok) return
  ui.showLoading('移除 DNS fallback 域名中...')
  try {
    const r = await api().remove_dns_fallback(domain)
    ui.toast(r.message, r.success ? 'success' : 'error')
    if (r.success) await loadDnsFallbackRules()
  } catch (e) {
    ui.toast('移除失败: ' + e, 'error')
  }
  ui.hideLoading()
}

async function toggleDnsFallback(domain, enabled) {
  try {
    const r = await api().toggle_dns_fallback(domain, enabled)
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadDnsFallbackRules()
  } catch (e) {
    ui.toast('操作失败: ' + e, 'error')
  }
}

async function applyDnsFallbackToWarp() {
  ui.showLoading('同步 DNS fallback 到 WARP 中...')
  try {
    const r = await api().apply_dns_fallback_to_warp()
    ui.toast(r.message, r.success ? 'success' : 'error')
    await loadDnsFallbackRules()
  } catch (e) {
    ui.toast('同步失败: ' + e, 'error')
  }
  ui.hideLoading()
}

// ===== 分页大小持久化 =====
function persistPageSize(size) {
  try {
    api()?.save_ui_prefs({ page_size: size })?.catch(() => {})
  } catch (e) {
    console.warn('save_ui_prefs failed:', e)
  }
}

function onLearnedPageSize(size) {
  learnedPage.pageSize = size
  learnedPage.page = 1
  rules.pageSize = size
  persistPageSize(size)
}

function onRulesPageSize(size) {
  rules.pageSize = size
  rules.page = 1
  learnedPage.pageSize = size
  persistPageSize(size)
}

// UI 偏好中的分页大小可能在组件挂载后才到达，同步到本地分页状态
watch(
  () => store.pageSize,
  (sz) => {
    learnedPage.pageSize = sz
    learnedPage.page = 1
    rules.pageSize = sz
    rules.page = 1
  }
)

// ===== 生命周期 =====
function onVisibilityChange() {
  if (document.hidden) {
    wasAutoBeforeHide = !!learnTimer
    stopAutoRefreshLearned()
  } else {
    if (wasAutoBeforeHide) startAutoRefreshLearned()
  }
}

watch(
  () => store.apiReady,
  async (ready) => {
    if (!ready) return
    await loadRules()
    await loadWarpRanges()
    await loadDnsFallbackRules()
  },
  { immediate: true }
)

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange)
  stopAutoRefreshLearned()
})
</script>

<template>
  <div class="warp-view">
    <!-- 学习模式 -->
    <section class="card section">
      <div class="section-title">
        学习模式
        <n-tag size="small" :type="learning.active ? 'info' : 'default'">{{ learning.badge }}</n-tag>
      </div>
      <div class="learn-status" :class="{ active: learning.active }">{{ learning.status }}</div>
      <div class="learn-actions">
        <n-button type="primary" size="small" v-if="!learning.active" @click="startLearning">开始学习</n-button>
        <n-button type="error" size="small" v-else @click="stopLearning">停止学习</n-button>
        <n-button size="small" :disabled="!learning.domains.length" @click="refreshLearnedDomains">刷新列表</n-button>
        <n-button size="small" type="success" :disabled="!filteredLearned.length" @click="addSelectedDomains">添加选中到排除列表</n-button>
      </div>
      <div class="search-row">
        <n-input v-model:value="learning.keyword" size="small" placeholder="模糊搜索域名（不区分大小写）" clearable />
        <span class="search-count" v-if="learning.keyword.trim() && learning.domains.length">
          {{ filteredLearned.length }}/{{ learning.domains.length }}
        </span>
        <n-button size="small" :disabled="!pagedLearned.length" @click="toggleSelectAll">
          {{ pageAllChecked ? '取消全选' : '全选' }}
        </n-button>
      </div>
      <div class="domain-list">
        <div v-if="!learning.domains.length" class="empty-hint">暂未学习到域名</div>
        <div v-else-if="!filteredLearned.length" class="empty-hint">无匹配的域名</div>
        <div v-for="d in pagedLearned" :key="d" class="domain-item" :class="{ checked: checkedDomains.has(d) }">
          <n-checkbox :checked="checkedDomains.has(d)" @update:checked="(v) => onDomainCheck(d, v)" />
          <span class="domain-name mono">{{ d }}</span>
        </div>
      </div>
      <n-pagination v-if="filteredLearned.length" size="small" :page="learnedPage.page"
        :page-size="learnedPage.pageSize" :item-count="filteredLearned.length" :page-sizes="[10, 20, 50, 100]"
        show-size-picker @update:page="(p) => (learnedPage.page = p)" @update:page-size="onLearnedPageSize" />
    </section>

    <!-- 已配置的排除规则 -->
    <section class="card section">
      <div class="section-title">
        已配置的排除规则
        <n-tag size="small">{{ rules.list.length }}</n-tag>
      </div>

      <div class="subtab-bar">
        <button class="subtab-btn" :class="{ active: subtab === 'domain' }" @click="subtab = 'domain'">域名排除</button>
        <button class="subtab-btn" :class="{ active: subtab === 'ip' }" @click="subtab = 'ip'">IP 排除</button>
      </div>

      <!-- 域名排除子页 -->
      <div v-show="subtab === 'domain'">
        <div class="add-row">
          <div class="add-title">手动添加域名</div>
          <div class="add-form">
            <n-input v-model:value="domainInput" size="small" placeholder="输入域名，如 example.com" style="flex: 2"
              @keydown.enter="addDomainManually" />
            <n-select v-model:value="domainRoute" size="small" :options="routeOptions" style="width: 150px" />
            <n-button type="primary" size="small" @click="addDomainManually">添加</n-button>
          </div>
          <div class="section-desc">
            <strong>走 IPv6 校园网：</strong>域名排除 WARP + DNS fallback + IPv6 CIDR + 防火墙阻止 IPv4，强制走校园
            IPv6（适合有 AAAA 记录的域名）<br />
            <strong>走 IPv4 校园网：</strong>域名排除 WARP，IPv4/IPv6 都走校园网直连（适合只有 A 记录的域名）
          </div>
        </div>

        <div class="bulk-actions">
          <n-button size="tiny" type="primary" secondary @click="applyAllToWarp">同步到WARP</n-button>
          <n-button size="tiny" @click="syncFromWarp">从WARP同步</n-button>
          <n-button size="tiny" @click="checkIpv6Support" title="检测所有 IPv6 路由域名是否真的支持 IPv6，不支持则降级为 IPv4">检测 IPv6 支持</n-button>
        </div>

        <div class="search-row">
          <n-input v-model:value="rules.keyword" size="small" placeholder="模糊搜索规则域名（不区分大小写）" clearable />
          <span class="search-count" v-if="rules.keyword.trim() && rules.list.length">
            {{ filteredRules.length }}/{{ rules.list.length }}
          </span>
        </div>

        <div class="rule-list">
          <div v-if="!rules.list.length" class="empty-hint">暂无排除规则，请通过学习模式或手动添加</div>
          <div v-else-if="!filteredRules.length" class="empty-hint">无匹配的规则</div>
          <div v-for="d in pagedRules" :key="d.domain" class="rule-item">
            <div class="rule-header">
              <span class="rule-domain mono">
                {{ d.domain }}
                <n-tag size="tiny" :bordered="false" type="info" v-if="(d.route || 'ipv6') === 'ipv4'">IPv4</n-tag>
                <n-tag size="tiny" :bordered="false" type="success" v-else>IPv6</n-tag>
              </span>
              <div class="rule-actions">
                <n-tag size="small" :type="d.enabled ? 'success' : 'default'">{{ d.enabled ? '已启用' : '已禁用' }}</n-tag>
                <n-button size="tiny" quaternary @click="setDomainRoute(d.domain, (d.route || 'ipv6') === 'ipv4' ? 'ipv6' : 'ipv4')">
                  {{ (d.route || 'ipv6') === 'ipv4' ? '切IPv6' : '切IPv4' }}
                </n-button>
                <n-button size="tiny" quaternary @click="toggleDomain(d.domain, !d.enabled)">
                  {{ d.enabled ? '禁用' : '启用' }}
                </n-button>
                <n-button size="tiny" quaternary type="error" @click="removeDomain(d.domain)">删除</n-button>
              </div>
            </div>
            <div class="rule-meta">
              添加时间: {{ d.added_at || '未知' }} | 路由: {{ (d.route || 'ipv6') === 'ipv4' ? 'IPv4 校园网直连' : 'IPv6 校园网直连' }}
            </div>
          </div>
        </div>

        <n-pagination v-if="filteredRules.length" size="small" :page="rules.page" :page-size="rules.pageSize"
          :item-count="filteredRules.length" :page-sizes="[10, 20, 50, 100]" show-size-picker
          @update:page="(p) => (rules.page = p)" @update:page-size="onRulesPageSize" />
      </div>

      <!-- IP 排除子页 -->
      <div v-show="subtab === 'ip'">
        <div class="add-row">
          <div class="add-title">手动添加 IP 范围</div>
          <div class="add-form">
            <n-input v-model:value="ipInput" size="small" placeholder="输入 IP 或 CIDR，如 10.0.0.0/8 或 2402:4e00::"
              style="flex: 2" @keydown.enter="addIpRangeManually" />
            <n-select v-model:value="ipPrefix" size="small" :options="prefixOptions" style="width: 120px" />
            <n-select v-model:value="ipRoute" size="small" :options="ipRouteOptions" style="width: 150px" />
            <n-button type="primary" size="small" @click="addIpRangeManually">添加</n-button>
          </div>
          <div class="section-desc">
            <strong>走 IPv4 校园网：</strong>CIDR 排除，流量走校园网 IPv4<br />
            <strong>走 IPv6 校园网：</strong>CIDR 排除，流量走校园网 IPv6<br />
            <strong>前缀长度：</strong>/32(单IP)、/24(IPv4子网)、/32或/48(IPv6网段)、/64(IPv6子网)
          </div>
        </div>

        <div class="recommend-box">
          <div class="recommend-head">
            <span class="add-title">从当前连接推荐</span>
            <n-button size="tiny" :loading="recommendLoading" @click="loadTrafficForExclude">刷新连接</n-button>
          </div>
          <div class="section-desc">显示当前未走 WARP 的连接（直连），点击可一键将其 IP 网段加入排除列表。</div>
          <div class="recommend-list">
            <div v-if="recommend === null" class="empty-hint">点击“刷新连接”加载</div>
            <div v-else-if="!recommendItems.length" class="empty-hint">暂无直连连接（所有流量都走 WARP）</div>
            <div v-for="c in recommendItems" :key="c.ip" class="recommend-item">
              <div class="recommend-info">
                <div class="recommend-line1">
                  <span class="recommend-process">{{ c.process }}</span>
                  <n-tag size="tiny" :bordered="false">{{ c.route_type === 'ipv6' ? 'IPv6直连' : 'IPv4直连' }}</n-tag>
                </div>
                <div class="recommend-host">{{ c.hostname || '(无域名)' }}</div>
                <div class="recommend-ip mono">{{ c.ip }}</div>
              </div>
              <div class="recommend-ops">
                <n-button size="tiny"
                  @click="addRecommendedCidr(buildCidr(c.ip, String(c.ip.includes(':') ? 32 : 24)), c.ip.includes(':') ? 'ipv6' : 'ipv4')"
                  :title="`添加 /${c.ip.includes(':') ? 32 : 24} 网段`">
                  /{{ c.ip.includes(':') ? 32 : 24 }}
                </n-button>
                <n-button size="tiny" @click="addRecommendedCidr(c.ip + (c.ip.includes(':') ? '/128' : '/32'), c.ip.includes(':') ? 'ipv6' : 'ipv4')"
                  title="添加单 IP">单IP</n-button>
              </div>
            </div>
          </div>
        </div>

        <div class="rule-list">
          <div v-if="!ipRanges.length" class="empty-hint">暂无 IP 排除规则</div>
          <div v-for="r in ipRanges" :key="r.cidr" class="rule-item">
            <div class="rule-header">
              <span class="rule-domain mono">
                {{ r.cidr }}
                <n-tag size="tiny" :bordered="false" type="info" v-if="(r.route || 'ipv4') === 'ipv4'">IPv4</n-tag>
                <n-tag size="tiny" :bordered="false" type="success" v-else>IPv6</n-tag>
              </span>
              <div class="rule-actions">
                <n-tag size="small" :type="r.enabled ? 'success' : 'default'">{{ r.enabled ? '已启用' : '已禁用' }}</n-tag>
                <n-button size="tiny" quaternary @click="setIpRangeRoute(r.cidr, (r.route || 'ipv4') === 'ipv4' ? 'ipv6' : 'ipv4')">
                  {{ (r.route || 'ipv4') === 'ipv4' ? '切IPv6' : '切IPv4' }}
                </n-button>
                <n-button size="tiny" quaternary @click="toggleIpRange(r.cidr, !r.enabled)">
                  {{ r.enabled ? '禁用' : '启用' }}
                </n-button>
                <n-button size="tiny" quaternary type="error" @click="removeIpRange(r.cidr)">删除</n-button>
              </div>
            </div>
            <div class="rule-meta">
              路由: {{ (r.route || 'ipv4') === 'ipv4' ? 'IPv4 校园网直连' : 'IPv6 校园网直连' }}
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- WARP 当前排除规则 -->
    <section class="card section">
      <div class="section-title">
        WARP 当前排除规则
        <n-button size="tiny" style="margin-left: auto" @click="loadWarpRanges">刷新</n-button>
      </div>
      <div class="bulk-actions" v-if="warpLegacyRanges.length">
        <n-button size="tiny" type="error" secondary @click="cleanupLegacyRules">清理旧版IP规则残留</n-button>
      </div>
      <div class="warp-ranges">
        <div v-if="!warpDomainRanges.length" class="empty-hint">WARP中暂无域名排除规则</div>
        <template v-else>
          <div class="range-group-title">域名排除规则（tunnel host）:</div>
          <div class="range-line mono" v-for="r in warpDomainRanges" :key="r">{{ r }}</div>
        </template>
        <template v-if="warpIpv6Ranges.length">
          <div class="range-group-title">IPv6 白名单使用的 CIDR（{{ warpIpv6Ranges.length }}条，正常）:</div>
          <div class="range-line mono" v-for="r in warpIpv6Ranges" :key="r">{{ r }}</div>
        </template>
        <template v-if="warpLegacyRanges.length">
          <div class="range-group-title legacy">旧版IP排除规则残留（{{ warpLegacyRanges.length }}条，建议清理）:</div>
          <div class="range-line mono legacy" v-for="r in warpLegacyRanges" :key="r">{{ r }}</div>
        </template>
      </div>
    </section>

    <!-- DNS Fallback -->
    <section class="card section">
      <div class="section-title">
        DNS Fallback 域名（本地解析）
        <n-tag size="small">{{ dnsList.length }}</n-tag>
        <n-button size="tiny" style="margin-left: auto" @click="loadDnsFallbackRules">刷新</n-button>
      </div>
      <div class="dns-desc">
        <strong>用途：</strong>让指定域名的 DNS 查询走本地运营商 DNS（而非 WARP DNS），避免 CDN 调度域名返回海外节点导致访问被阻止。<br />
        <strong>典型场景：</strong>bytedns3.com、queniuyk.com 等字节系 CDN 调度域名。<br />
        <strong>与流量排除的区别：</strong>流量排除（tunnel host）让连接不走 WARP；DNS fallback 让解析走本地 DNS，流量仍可走 WARP。
      </div>
      <div class="add-form">
        <n-input v-model:value="dnsInput" size="small" placeholder="输入域名，如 bytedns3.com" style="flex: 1"
          @keydown.enter="addDnsFallbackManually" />
        <n-button type="primary" size="small" @click="addDnsFallbackManually">添加</n-button>
      </div>
      <div class="bulk-actions">
        <n-button size="tiny" type="primary" secondary @click="applyDnsFallbackToWarp">同步到WARP</n-button>
      </div>
      <div class="rule-list">
        <div v-if="!dnsList.length" class="empty-hint">暂无 DNS fallback 域名</div>
        <div v-for="d in dnsList" :key="d.domain" class="rule-item">
          <div class="rule-header">
            <span class="rule-domain mono">{{ d.domain }}</span>
            <div class="rule-actions">
              <n-tag size="small" :type="d.enabled !== false ? 'success' : 'default'">{{ d.enabled !== false ? '已启用' : '已禁用' }}</n-tag>
              <n-button size="tiny" quaternary @click="toggleDnsFallback(d.domain, d.enabled === false)">
                {{ d.enabled !== false ? '禁用' : '启用' }}
              </n-button>
              <n-button size="tiny" quaternary type="error" @click="removeDnsFallback(d.domain)">删除</n-button>
            </div>
          </div>
          <div class="rule-meta">添加时间: {{ d.added_at || '未知' }}</div>
        </div>
      </div>
      <div class="warp-dns-block">
        <div class="range-group-title">WARP 中当前的 DNS fallback 域名：</div>
        <div v-if="!warpDnsList.length" class="empty-hint">WARP中暂无 DNS fallback 域名</div>
        <div class="range-line mono" v-for="d in warpDnsList" :key="d">{{ d }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.warp-view {
  padding: 20px 22px 30px;
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section {
  padding: 16px 18px;
}

/* 学习模式 */
.learn-status {
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.7;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 13px;
}

.learn-status.active {
  border-color: var(--border-strong);
  background: var(--accent-dim);
  color: var(--text-primary);
}

.learn-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.search-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.search-row .n-input {
  max-width: 300px;
}

.search-count {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  white-space: nowrap;
}

.domain-list {
  margin-top: 10px;
  max-height: 280px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 2px;
}

.domain-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--bg-elevated);
  border: 1px solid transparent;
  transition: all 0.15s;
}

.domain-item.checked {
  border-color: var(--border-strong);
  background: var(--accent-dim);
}

.domain-name {
  font-size: 12px;
  word-break: break-all;
}

.domain-list .n-pagination {
  margin-top: 10px;
}

/* 子tab */
.subtab-bar {
  display: flex;
  gap: 2px;
  margin: 12px 0;
  border-bottom: 1px solid var(--border);
}

.subtab-btn {
  padding: 7px 14px;
  font-size: 12px;
  color: var(--text-tertiary);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
}

.subtab-btn:hover {
  color: var(--text-primary);
}

.subtab-btn.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

/* 添加行 */
.add-row {
  margin-bottom: 14px;
}

.add-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.add-form {
  display: flex;
  gap: 8px;
  align-items: center;
}

.add-form+.section-desc {
  margin-top: 8px;
}

.bulk-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 12px 0;
}

/* 规则列表 */
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}

.rule-item {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 13px;
}

.rule-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.rule-domain {
  font-size: 13px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  word-break: break-all;
}

.rule-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.rule-meta {
  margin-top: 7px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.rule-list .n-pagination {
  margin-top: 12px;
}

/* 推荐连接 */
.recommend-box {
  margin-bottom: 14px;
  padding: 12px 14px;
  background: var(--bg-elevated);
  border-radius: 8px;
  border: 1px solid var(--border);
}

.recommend-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.recommend-box .section-desc {
  margin: 8px 0;
}

.recommend-list {
  max-height: 220px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.recommend-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-radius: 6px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
}

.recommend-info {
  flex: 1;
  min-width: 0;
}

.recommend-line1 {
  display: flex;
  align-items: center;
  gap: 8px;
}

.recommend-process {
  font-size: 12px;
  font-weight: 500;
}

.recommend-host {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.recommend-ip {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.recommend-ops {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

/* WARP ranges */
.warp-ranges {
  margin-top: 12px;
}

.range-group-title {
  font-size: 12px;
  color: var(--text-tertiary);
  margin: 10px 0 6px;
}

.range-group-title:first-child {
  margin-top: 0;
}

.range-group-title.legacy {
  color: var(--error);
}

.range-line {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 3px 10px;
  background: var(--bg-elevated);
  border-radius: 5px;
  margin-bottom: 3px;
  word-break: break-all;
}

.range-line.legacy {
  color: var(--error);
}

/* DNS */
.dns-desc {
  margin: 12px 0;
  padding: 10px 13px;
  background: var(--bg-elevated);
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.7;
}

.warp-dns-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
</style>
