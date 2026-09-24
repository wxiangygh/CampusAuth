<script setup>
import { ref, reactive, watch } from 'vue'
import { NButton, NInput, NSwitch } from 'naive-ui'
import { api } from '../bridge'
import { store } from '../store'
import { ui } from '../ui'

const loaded = ref(false)
const saving = ref(false)
const rules = ref([])
const draft = reactive({ target: '', servers: '' })
const nrpt = ref({ enabled_bindings: 0, applied: 0, system_rules: 0, updated_at: '' })
const nrptBusy = ref(false)

async function load() {
  loaded.value = false
  try {
    const result = await api().get_dns_bindings()
    // 服务器列表按行展示，编辑后原样回传（后端兼容换行与逗号分隔）
    rules.value = (result.bindings || []).map(r => ({ ...r, servers: r.servers.join('\n') }))
    loaded.value = true
    loadNrpt()
  } catch (e) {
    ui.toast('读取定向解析规则失败：' + e.message, 'error')
  }
}

async function loadNrpt() {
  try {
    nrpt.value = await api().get_dns_nrpt_status()
  } catch (e) {
    console.warn('get_dns_nrpt_status failed:', e)
  }
}

async function pushNrpt() {
  nrptBusy.value = true
  try {
    const result = await api().apply_dns_bindings_to_system()
    ui.toast(result.message, result.success ? 'success' : 'error')
    await loadNrpt()
  } catch (e) {
    ui.toast('下发系统解析策略失败：' + e.message, 'error')
  } finally {
    nrptBusy.value = false
  }
}

async function clearNrpt() {
  nrptBusy.value = true
  try {
    const result = await api().clear_dns_bindings_from_system()
    ui.toast(result.message, result.success ? 'success' : 'error')
    await loadNrpt()
  } catch (e) {
    ui.toast('清除系统解析策略失败：' + e.message, 'error')
  } finally {
    nrptBusy.value = false
  }
}

function addRule() {
  const target = draft.target.trim()
  const servers = draft.servers.trim()
  if (!target || !servers) {
    ui.toast('请填写解析目标与 DNS 服务器', 'warning')
    return
  }
  if (rules.value.some(r => r.target.toLowerCase() === target.toLowerCase())) {
    ui.toast('该目标已有绑定规则，请直接修改', 'warning')
    return
  }
  rules.value.push({ target, servers, enabled: true })
  draft.target = ''
  draft.servers = ''
}

function removeRule(index) {
  rules.value.splice(index, 1)
}

async function save() {
  saving.value = true
  try {
    const result = await api().save_dns_bindings(
      rules.value.map(r => ({ target: r.target, servers: r.servers, enabled: r.enabled }))
    )
    ui.toast(result.message, result.success ? 'success' : 'error')
    if (result.success) {
      store.configRevision = result.revision
      // 重载以显示后端规范化后的目标（小写、去通配符与末尾点、网段补全）
      await load()
    }
  } catch (e) {
    ui.toast('保存定向解析规则失败：' + e.message, 'error')
  } finally {
    saving.value = false
  }
}

watch(() => store.apiReady, ready => { if (ready) load() }, { immediate: true })
</script>

<template>
  <section class="card dns-bindings">
    <div class="section-title">定向解析</div>
    <div class="section-desc">
      为特定的域名、IP 或网段绑定解析所用的 DNS 服务器。域名规则覆盖其全部子域名，多个规则命中时取最具体的一条；
      未命中的解析仍使用上方的全局 DNS 服务器。
    </div>
    <template v-if="loaded">
      <div class="binding-list">
        <div v-if="!rules.length" class="empty-hint">暂无定向解析规则</div>
        <div v-for="(r, i) in rules" :key="r.target" class="binding-item">
          <div class="binding-head">
            <span class="mono binding-target">{{ r.target }}</span>
            <div class="binding-actions">
              <n-switch v-model:value="r.enabled" size="small" />
              <span class="binding-state">{{ r.enabled ? '已启用' : '已禁用' }}</span>
              <n-button size="tiny" quaternary type="error" @click="removeRule(i)">删除</n-button>
            </div>
          </div>
          <n-input v-model:value="r.servers" type="textarea" size="small" :autosize="{ minRows: 1, maxRows: 4 }"
            placeholder="每行一个 DNS 服务器地址，也可用逗号分隔" />
        </div>
      </div>

      <div class="add-title">新增绑定</div>
      <div class="add-form">
        <n-input v-model:value="draft.target" size="small" placeholder="域名 example.com / IP 1.2.3.4 / 网段 2400:3200::/32"
          style="flex: 1" @keydown.enter="addRule" />
        <n-input v-model:value="draft.servers" size="small" placeholder="解析服务器，如 8.8.8.8, 1.1.1.1" style="flex: 1"
          @keydown.enter="addRule" />
        <n-button size="small" @click="addRule">添加</n-button>
      </div>

      <div class="save-row">
        <n-button type="primary" :loading="saving" @click="save">保存定向解析规则</n-button>
        <n-button :disabled="saving" @click="load">放弃修改并重载</n-button>
      </div>
      <p class="binding-tip">
        保存后只影响之后的解析；需要按新服务器重新解析已有域名，可在分流规则页删除后重新添加该域名。
      </p>

      <div class="nrpt-box">
        <div class="nrpt-head">
          <span class="nrpt-title">系统级生效（Windows 名称解析策略表 NRPT）</span>
          <n-button size="tiny" :disabled="nrptBusy" @click="loadNrpt">刷新状态</n-button>
        </div>
        <div class="section-desc">
          下发后浏览器、命令行等所有程序的匹配域名都会改用绑定的服务器（WARP 接管系统 DNS 时同样生效）；
          未下发时绑定只作用于本程序自己的解析。写入系统策略需要管理员权限。
        </div>
        <div class="nrpt-meta">
          启用规则 {{ nrpt.enabled_bindings }} 条 · 已在系统生效 {{ nrpt.applied }} 条 · 本机策略共
          {{ nrpt.system_rules }} 条<span v-if="nrpt.updated_at"> · 上次下发 {{ nrpt.updated_at }}</span>
        </div>
        <div class="nrpt-actions">
          <n-button size="small" type="primary" secondary :loading="nrptBusy" @click="pushNrpt">
            下发已保存的规则
          </n-button>
          <n-button size="small" :disabled="nrptBusy || !nrpt.applied" @click="clearNrpt">
            从系统清除
          </n-button>
        </div>
      </div>
    </template>
    <n-button v-else :disabled="!store.apiReady" @click="load">加载定向解析规则</n-button>
  </section>
</template>

<style scoped>
.dns-bindings {
  padding: 20px;
}

.dns-bindings .section-desc {
  margin-top: 8px;
}

.binding-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 14px;
}

.binding-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 13px;
}

.binding-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.binding-target {
  font-size: 13px;
  font-weight: 500;
  word-break: break-all;
}

.binding-actions {
  display: flex;
  align-items: center;
  gap: 7px;
}

.binding-state {
  font-size: 11px;
  color: var(--text-tertiary);
}

.add-title {
  margin-top: 16px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.add-form {
  display: flex;
  align-items: center;
  gap: 8px;
}

.save-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
}

.binding-tip {
  margin-top: 10px;
  font-size: 12px;
  opacity: .7;
  line-height: 1.7;
}

.nrpt-box {
  margin-top: 16px;
  padding: 12px 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
}

.nrpt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.nrpt-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.nrpt-meta {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.nrpt-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
</style>
