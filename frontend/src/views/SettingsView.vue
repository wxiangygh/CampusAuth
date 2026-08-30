<script setup>
import { ref, computed, watch, onBeforeUnmount, nextTick } from 'vue'
import { NButton, NInput, NAutoComplete, NSwitch, NInputGroup, NSelect } from 'naive-ui'
import { store, doAutoSave } from '../store'
import { api } from '../bridge'
import { ui } from '../ui'

// ===== 按钮绑定工作流 =====
const workflows = ref([])

const workflowOptions = computed(() =>
  workflows.value.map((wf) => ({
    label: wf.name + (wf.built_in ? '（内置）' : ''),
    value: wf.id,
  }))
)

// 恢复按钮额外提供"内置恢复流程"选项（value 为空串）
const restoreOptions = computed(() => [
  { label: '内置恢复流程（断开 WARP 并启用 IPv4）', value: '' },
  ...workflowOptions.value,
])

// ===== WiFi 扫描 =====
const wifiOptions = ref([])
const scanning = ref(false)

async function refreshWifi() {
  if (scanning.value) return
  scanning.value = true
  wifiOptions.value = []
  try {
    const networks = await api().scan_wifi()
    wifiOptions.value = (networks || []).map((s) => ({ label: s, value: s }))
  } catch (e) {
    console.error('Failed to start wifi scan:', e)
  }
  scanning.value = false
}

async function browseWarpPath() {
  try {
    const path = await api().browse_folder('选择 warp-cli.exe')
    if (path) {
      store.form.warp_cli_path = path
      doAutoSave()
    }
  } catch (e) {
    console.error('browseWarpPath failed:', e)
  }
}

// ===== 表单自动保存 =====
let autoSaveTimer = null

// 文本输入：防抖 500ms
watch(
  () => [store.form.wifi_name, store.form.username, store.form.password, store.form.warp_cli_path, store.form.portal_ip, store.form.portal_port],
  () => {
    if (!store.configLoaded) return
    clearTimeout(autoSaveTimer)
    autoSaveTimer = setTimeout(() => doAutoSave(), 500)
  }
)

// 开关：立即保存
watch(
  () => [store.form.auto_auth, store.form.auto_restore],
  () => {
    if (!store.configLoaded) return
    clearTimeout(autoSaveTimer)
    doAutoSave()
  }
)

// 按钮绑定工作流：立即保存
watch(
  () => [store.form.auth_button_workflow, store.form.restore_button_workflow],
  () => {
    if (!store.configLoaded) return
    clearTimeout(autoSaveTimer)
    doAutoSave()
  }
)

watch(
  () => store.form.silent_startup,
  async () => {
    if (!store.configLoaded) return
    clearTimeout(autoSaveTimer)
    doAutoSave()
    try {
      await api().refresh_startup_task()
    } catch (e) {
      console.error('refresh_startup_task failed:', e)
    }
  }
)

// 开机自启走独立 API（失败回滚）；初始化回填时不触发
let suppressStartupWatch = false

watch(
  () => store.form.auto_startup,
  async (enabled) => {
    if (!store.configLoaded || suppressStartupWatch) return
    try {
      const result = await api().set_startup(enabled)
      if (!result.success) {
        store.form.auto_startup = !enabled
        ui.alert(result.message, '提示', 'warning')
      }
    } catch (e) {
      store.form.auto_startup = !enabled
      ui.alert('设置失败: ' + e.message, '错误', 'error')
    }
  }
)

// ===== 初始化：加载配置与开机自启状态 =====
async function initSettings() {
  try {
    const config = await api().load_config()
    const f = store.form
    f.wifi_name = config.wifi_name || ''
    f.username = config.username || ''
    f.password = config.password || ''
    f.auto_auth = config.auto_auth || false
    f.auto_restore = config.auto_restore || false
    f.warp_cli_path = config.warp_cli_path || ''
    f.portal_ip = config.portal_ip || ''
    f.portal_port = config.portal_port || ''
    f.silent_startup = config.silent_startup || false
    f.auth_total_timeout = config.auth_total_timeout || 90
    f.auth_button_workflow = config.auth_button_workflow || 'default_auth'
    f.restore_button_workflow = config.restore_button_workflow || ''
    store.configRevision = config._revision || 0
    await nextTick()
    store.configLoaded = true
  } catch (e) {
    console.error('Failed to load config:', e)
    store.configLoaded = true
  }
  try {
    const startup = await api().get_startup_status()
    suppressStartupWatch = true
    store.form.auto_startup = startup.enabled || false
    await nextTick()
    suppressStartupWatch = false
  } catch (e) {
    suppressStartupWatch = false
    console.error('Failed to get startup status:', e)
  }
  // 加载工作流列表供按钮绑定下拉使用
  try {
    const data = await api().list_workflows()
    workflows.value = data.workflows || []
    // 绑定的 id 已被删除时回退到默认，避免按钮指向不存在的工作流
    const ids = new Set(workflows.value.map((w) => w.id))
    if (!ids.has(store.form.auth_button_workflow)) store.form.auth_button_workflow = 'default_auth'
    if (store.form.restore_button_workflow && !ids.has(store.form.restore_button_workflow)) {
      store.form.restore_button_workflow = ''
    }
  } catch (e) {
    console.error('Failed to load workflows:', e)
  }
}

watch(
  () => store.apiReady,
  async (ready) => {
    if (!ready) return
    await initSettings()
  },
  { immediate: true }
)

function onBeforeUnload() {
  doAutoSave()
}

window.addEventListener('beforeunload', onBeforeUnload)

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  clearTimeout(autoSaveTimer)
})
</script>

<template>
  <div class="settings-view">
    <section class="card settings-card">
      <div class="section-title">基本设置</div>

      <div class="form-row">
        <div class="form-group">
          <label class="form-label">WiFi 网络</label>
          <n-input-group>
            <n-auto-complete v-model:value="store.form.wifi_name" :options="wifiOptions"
              placeholder="选择或输入WiFi名称" style="flex: 1" />
            <n-button :loading="scanning" @click="refreshWifi">扫描</n-button>
          </n-input-group>
        </div>
        <div class="form-group">
          <label class="form-label">认证账号</label>
          <n-input v-model:value="store.form.username" placeholder="输入上网账号" />
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label class="form-label">认证密码</label>
          <n-input v-model:value="store.form.password" type="password" show-password-on="click"
            placeholder="输入账号密码" />
        </div>
        <div class="form-group">
          <label class="form-label">校园网认证服务器</label>
          <n-input-group>
            <n-input v-model:value="store.form.portal_ip" placeholder="服务器 IP" style="flex: 1" />
            <n-input v-model:value="store.form.portal_port" placeholder="端口" style="max-width: 110px" />
          </n-input-group>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label class="form-label">WARP-CLI 路径</label>
          <n-input-group>
            <n-input v-model:value="store.form.warp_cli_path" placeholder="留空则自动检测" style="flex: 1" />
            <n-button @click="browseWarpPath">浏览</n-button>
          </n-input-group>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label class="form-label">「开始认证」按钮执行的工作流</label>
          <n-select v-model:value="store.form.auth_button_workflow" :options="workflowOptions" />
        </div>
        <div class="form-group">
          <label class="form-label">「恢复网络」按钮执行的工作流</label>
          <n-select v-model:value="store.form.restore_button_workflow" :options="restoreOptions" />
        </div>
      </div>

      <div class="toggle-grid">
        <div class="toggle-card">
          <div class="toggle-head">
            <span class="toggle-title">自动认证</span>
            <n-switch v-model:value="store.form.auto_auth" size="small" />
          </div>
          <div class="toggle-tip">连接 WiFi 后自动认证</div>
        </div>
        <div class="toggle-card">
          <div class="toggle-head">
            <span class="toggle-title">自动恢复</span>
            <n-switch v-model:value="store.form.auto_restore" size="small" />
          </div>
          <div class="toggle-tip">切换 WiFi 后自动恢复网络</div>
        </div>
        <div class="toggle-card">
          <div class="toggle-head">
            <span class="toggle-title">开机自启</span>
            <n-switch v-model:value="store.form.auto_startup" size="small" />
          </div>
          <div class="toggle-tip">登录后自动启动</div>
        </div>
        <div class="toggle-card">
          <div class="toggle-head">
            <span class="toggle-title">静默启动</span>
            <n-switch v-model:value="store.form.silent_startup" size="small" />
          </div>
          <div class="toggle-tip">开机自启时不显示主窗口</div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.settings-view {
  padding: 20px 22px 30px;
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.settings-card {
  padding: 18px 20px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-top: 16px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
}

.form-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.toggle-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 16px;
}

.toggle-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 11px 14px;
}

.toggle-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.toggle-title {
  font-size: 13px;
  font-weight: 500;
}

.toggle-tip {
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
