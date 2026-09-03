<script setup>
// "当前分流配置"悬浮窗（独立窗口，hash 路由 #viewer 挂载，不经过 App.vue）
// 三列分栏：WARP 域名排除规则 / IPv6 白名单 CIDR（含旧版残留）/ WARP DNS fallback 域名
// - pywebview 生产环境：frameless 独立窗口，标题栏带 pywebview-drag-region 可拖动，
//   可拖出主窗口边界；关闭走后端 close_traffic_config_window（主窗口关闭时后端联动销毁）
// - 浏览器 dev 预览：window.open 弹窗（原生标题栏可拖动），关闭按钮回退 window.close()
import { ref, reactive, computed, onMounted } from 'vue'
import { NConfigProvider, NButton, NTag } from 'naive-ui'
import { waitForApi, api } from '../bridge'
import { naiveTheme, themeOverrides, isDark } from '../theme'
import { ui } from '../ui'
import { sortBy, compareDomain } from '../utils/sortlists'
import AppIcon from '../components/AppIcon.vue'
import SortToggle from '../components/SortToggle.vue'

const loading = ref(false)
const loadedAt = ref('')

const state = reactive({
  domains: [],   // WARP tunnel host 域名排除规则
  ipv6: [],      // IPv6 白名单 CIDR（使用中）
  legacy: [],    // 旧版 IP/CIDR 排除规则残留
  dns: [],       // WARP 中当前的 DNS fallback 域名
})

// 每列独立的排序方向：1 = A→Z，-1 = Z→A
const domainSortDir = ref(1)
const cidrSortDir = ref(1)
const dnsSortDir = ref(1)

// 域名列：*.x.com 与 x.com 同组，组内裸域名在前（降序只翻转分组之间）
const sortedDomains = computed(() => sortBy(state.domains, (x) => x, domainSortDir.value, compareDomain))
// IPv6 白名单与旧版残留同属一列，共用一个排序方向
const sortedIpv6 = computed(() => sortBy(state.ipv6, (x) => x, cidrSortDir.value))
const sortedLegacy = computed(() => sortBy(state.legacy, (x) => x, cidrSortDir.value))
const sortedDns = computed(() => sortBy(state.dns, (x) => x, dnsSortDir.value, compareDomain))

const hasAny = computed(() =>
  state.domains.length + state.ipv6.length + state.legacy.length + state.dns.length > 0)

async function refresh() {
  loading.value = true
  try {
    const a = api()
    const [domains, ipInfo, dns] = await Promise.all([
      a.get_warp_ranges().catch(() => []),
      a.get_cli_ip_ranges().catch(() => ({ active_ipv6: [], legacy: [] })),
      a.get_dns_fallback_list().catch(() => []),
    ])
    state.domains = domains || []
    state.ipv6 = (ipInfo && ipInfo.active_ipv6) || []
    state.legacy = (ipInfo && ipInfo.legacy) || []
    state.dns = dns || []
    loadedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } finally {
    loading.value = false
  }
}

function closeWindow() {
  const a = api()
  if (a && typeof a.close_traffic_config_window === 'function') {
    a.close_traffic_config_window().catch(() => {})
  }
  // 浏览器 dev 回退：window.open 打开的弹窗允许脚本关闭自身
  try {
    window.close()
  } catch (e) {
    /* ignore */
  }
}

async function cleanupLegacyRules() {
  const ok = await ui.confirm(
    '确定清理旧版IP/CIDR排除规则残留？\n\n将执行：\n1. 删除WARP中所有CLI添加的IP排除规则\n2. 清理配置文件中的ips/cidrs/mode等旧字段\n3. 将配置中的域名以域名排除方式重新应用到WARP'
  )
  if (!ok) return
  loading.value = true
  try {
    const r = await api().cleanup_legacy_config()
    ui.toast(r.message, r.success ? 'success' : 'error')
    await refresh()
  } catch (e) {
    ui.toast('清理失败: ' + e, 'error')
  }
  loading.value = false
}

onMounted(async () => {
  document.title = '当前分流配置 - CampusAuth'
  try {
    await waitForApi(15000)
  } catch (e) {
    ui.toast('API 加载超时: ' + e.message, 'error')
    return
  }
  await refresh()
})
</script>

<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
    <div class="viewer-shell" :data-theme="isDark ? 'dark' : 'light'">
      <!-- 标题栏：pywebview-drag-region 支持拖动整个悬浮窗 -->
      <div class="viewer-titlebar pywebview-drag-region">
        <div class="viewer-title-left">
          <AppIcon name="globe" :size="14" />
          <span class="viewer-title-text">当前分流配置</span>
          <span class="viewer-updated mono" v-if="loadedAt">更新于 {{ loadedAt }}</span>
        </div>
        <div class="viewer-title-right">
          <button class="title-btn" title="刷新" @click="refresh" :disabled="loading">
            <AppIcon name="refresh" :size="14" />
          </button>
          <button class="title-btn close" title="关闭" @click="closeWindow">
            <AppIcon name="x" :size="14" />
          </button>
        </div>
      </div>

      <!-- 三列分栏 -->
      <div class="viewer-body">
        <section class="viewer-col">
          <div class="col-head">
            <span class="col-title">WARP 当前排除规则</span>
            <div class="col-head-right">
              <n-tag size="small">{{ state.domains.length }}</n-tag>
              <SortToggle v-model:dir="domainSortDir" compact subject="域名" />
            </div>
          </div>
          <div class="col-sub">域名排除规则（tunnel host）</div>
          <div class="col-list">
            <div v-if="!state.domains.length" class="empty-hint">WARP 中暂无域名排除规则</div>
            <div v-for="r in sortedDomains" :key="r" class="col-item mono">{{ r }}</div>
          </div>
        </section>

        <section class="viewer-col">
          <div class="col-head">
            <span class="col-title">IPv6 白名单 CIDR</span>
            <div class="col-head-right">
              <n-tag size="small" type="success">{{ state.ipv6.length }}</n-tag>
              <SortToggle v-model:dir="cidrSortDir" compact subject="CIDR" />
            </div>
          </div>
          <div class="col-sub">
            域名规则自动生成的 IPv6 路由 CIDR
            <template v-if="state.legacy.length">
              <br />
              <span class="legacy-count">另有 {{ state.legacy.length }} 条旧版 IP 规则残留</span>
            </template>
          </div>
          <div class="col-list">
            <div v-if="!state.ipv6.length && !state.legacy.length" class="empty-hint">暂无 IPv6 白名单 CIDR</div>
            <div v-for="r in sortedIpv6" :key="r" class="col-item mono">{{ r }}</div>
            <template v-if="state.legacy.length">
              <div class="col-item mono legacy" v-for="r in sortedLegacy" :key="r">{{ r }}</div>
            </template>
          </div>
          <div class="col-foot" v-if="state.legacy.length">
            <n-button size="tiny" type="error" secondary :loading="loading" @click="cleanupLegacyRules">
              清理旧版IP规则残留
            </n-button>
          </div>
        </section>

        <section class="viewer-col">
          <div class="col-head">
            <span class="col-title">WARP DNS Fallback 域名</span>
            <div class="col-head-right">
              <n-tag size="small">{{ state.dns.length }}</n-tag>
              <SortToggle v-model:dir="dnsSortDir" compact subject="域名" />
            </div>
          </div>
          <div class="col-sub">WARP 中当前的 DNS fallback（本地解析）域名</div>
          <div class="col-list">
            <div v-if="!state.dns.length" class="empty-hint">WARP 中暂无 DNS fallback 域名</div>
            <div v-for="d in sortedDns" :key="d" class="col-item mono">{{ d }}</div>
          </div>
        </section>
      </div>

      <div class="viewer-statusbar">
        <span v-if="loading">加载中...</span>
        <span v-else-if="hasAny">数据为 WARP 实时状态，可拖动窗口标题栏移动位置</span>
        <span v-else>暂无数据，请点击右上角刷新</span>
      </div>
    </div>
  </n-config-provider>
</template>

<style scoped>
.viewer-shell {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
  color: var(--text-primary);
  overflow: hidden;
}

.viewer-titlebar {
  height: 40px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px 0 16px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
}

.viewer-title-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.viewer-title-left svg {
  color: var(--text-tertiary);
}

.viewer-title-text {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}

.viewer-updated {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.viewer-title-right {
  display: flex;
  gap: 4px;
}

.title-btn {
  width: 30px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-tertiary);
  transition: all 0.15s;
}

.title-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.title-btn.close:hover {
  background: rgba(239, 68, 68, 0.22);
  color: #ef4444;
}

.title-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

/* 三列分栏：body 不滚动，各列拉伸等高，列表区域独立垂直滚动 */
.viewer-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
  padding: 12px;
}

.viewer-col {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}

.col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.col-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 计数标签与排序切换靠右成组，避免 space-between 把标签甩到中间 */
.col-head-right {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.col-sub {
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-tertiary);
  line-height: 1.5;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}

.legacy-count {
  color: var(--error);
}

.col-list {
  margin-top: 8px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.col-item {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 4px 8px;
  background: var(--bg-elevated);
  border-radius: 5px;
  word-break: break-all;
}

.col-item.legacy {
  color: var(--error);
  background: rgba(239, 68, 68, 0.08);
}

.col-foot {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.viewer-statusbar {
  flex-shrink: 0;
  height: 26px;
  display: flex;
  align-items: center;
  padding: 0 14px;
  background: var(--bg-panel);
  border-top: 1px solid var(--border);
  font-size: 10px;
  color: var(--text-tertiary);
}
</style>
