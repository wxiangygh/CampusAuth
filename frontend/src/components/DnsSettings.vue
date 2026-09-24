<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { NButton, NInput, NSelect } from 'naive-ui'
import { api } from '../bridge'
import { store } from '../store'
import { ui } from '../ui'

const presets = ref([])
const loaded = ref(false)
const saving = ref(false)
const form = reactive({ preset: 'legacy', servers: '', ipv6_servers: '' })
const options = computed(() => [
  ...presets.value.map(p => ({ label: p.label, value: p.id })),
  { label: '自定义 DNS', value: 'custom' },
])

function selectPreset(id) {
  form.preset = id
  const preset = presets.value.find(p => p.id === id)
  if (preset) {
    form.servers = preset.servers.join('\n')
    form.ipv6_servers = preset.ipv6_servers.join('\n')
  }
}

async function load() {
  loaded.value = false
  try {
    const result = await api().get_dns_settings()
    presets.value = result.presets
    form.preset = result.preset
    form.servers = result.servers.join('\n')
    form.ipv6_servers = result.ipv6_servers.join('\n')
    loaded.value = true
  } catch (e) {
    ui.toast('读取 DNS 设置失败：' + e.message, 'error')
  }
}

async function save() {
  saving.value = true
  try {
    const result = await api().save_dns_settings({ ...form })
    ui.toast(result.message, result.success ? 'success' : 'error')
    if (result.success) store.configRevision = result.revision
  } catch (e) {
    ui.toast('保存 DNS 设置失败：' + e.message, 'error')
  } finally {
    saving.value = false
  }
}

watch(() => store.apiReady, ready => { if (ready) load() }, { immediate: true })
</script>

<template>
  <section class="card dns-settings">
    <div class="section-title">DNS 服务器</div>
    <template v-if="loaded">
      <label>服务商预选</label>
      <n-select :value="form.preset" :options="options" @update:value="selectPreset" />
      <div class="dns-fields">
        <div>
          <label>分流规则解析 DNS（IPv4 / IPv6）</label>
          <n-input v-model:value="form.servers" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="每行一个 IP 地址，也可用逗号分隔" @update:value="form.preset = 'custom'" />
        </div>
        <div>
          <label>认证流程 IPv6 DNS</label>
          <n-input v-model:value="form.ipv6_servers" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="每行一个 IPv6 地址；留空表示自动获取" @update:value="form.preset = 'custom'" />
        </div>
      </div>
      <p>按填写顺序使用服务器。保存后，新的分流域名解析立即使用所选 DNS；认证 IPv6 DNS 在下次执行“设置 IPv6 DNS”节点时生效，重置节点仍恢复自动获取。</p>
      <p>DNS Fallback 继续使用本地网络的 DNS。联通预选适用于北京地区，其他地区可自定义；国外 DNS 的可用性取决于当前网络。</p>
      <n-button type="primary" :loading="saving" @click="save">保存 DNS 设置</n-button>
    </template>
    <n-button v-else :disabled="!store.apiReady" @click="load">加载 DNS 设置</n-button>
  </section>
</template>

<style scoped>
.dns-settings { padding: 20px; }
.dns-settings label { display: block; margin: 12px 0 8px; font-size: 13px; }
.dns-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.dns-settings p { font-size: 12px; opacity: .7; line-height: 1.7; }
@media (max-width: 700px) { .dns-fields { grid-template-columns: 1fr; } }
</style>
