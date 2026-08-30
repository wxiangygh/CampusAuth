<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { NButton, NInput, NInputNumber, NSelect, NCheckbox } from 'naive-ui'
import { api } from '../bridge'
import { store, doAutoSave } from '../store'
import { ui } from '../ui'

// ===== 状态 =====
const catalog = ref(new Map())
const workflows = ref([])
const currentId = ref(null)
const workflow = ref({ id: '', name: '', steps: [], tray_menu: true, built_in: false })
const message = ref('')
const messageError = ref(false)
const selectedStep = ref('')
const inited = ref(false)

const stepOptions = computed(() => {
  const groups = new Map()
  for (const step of catalog.value.values()) {
    const group = step.group || '其他'
    if (!groups.has(group)) groups.set(group, [])
    groups.get(group).push({ label: step.name, value: step.id })
  }
  return Array.from(groups.entries()).map(([label, children], i) => ({
    type: 'group',
    label,
    key: 'g_' + i,
    children,
  }))
})

const workflowOptions = computed(() => {
  const draft = currentId.value ? [] : [{ label: '未保存的新工作流', value: '' }]
  return draft.concat(
    workflows.value.map((wf) => ({
      label: wf.name + (wf.built_in ? '（内置）' : ''),
      value: wf.id,
    }))
  )
})

function setMessage(msg, isError = false) {
  message.value = msg || ''
  messageError.value = isError
}

function defaultStep(stepId) {
  const meta = catalog.value.get(stepId)
  return {
    id: stepId,
    enabled: true,
    retries: Number(meta?.default_retries || 0),
    timeout: Number(meta?.default_timeout || 15),
    retry_delay: 1,
    continue_on_error: false,
  }
}

// ===== 初始化 / 加载 =====
async function init(activeId) {
  try {
    const data = await api().get_workflow_catalog()
    catalog.value = new Map((data.steps || []).map((step) => [step.id, step]))
    workflows.value = data.workflows || []
    if (stepOptions.value.length && stepOptions.value[0].children?.length) {
      selectedStep.value = stepOptions.value[0].children[0].value
    }
    await load(activeId || data.active_workflow_id || 'default_auth', false)
  } catch (e) {
    setMessage('工作流加载失败：' + e.message, true)
  }
}

async function load(id, makeActive = true) {
  if (!id) return
  if (makeActive) {
    const result = await api().select_workflow(id)
    if (result.success === false) {
      setMessage(result.message, true)
      return
    }
    workflows.value = result.workflows || workflows.value
    if (result.revision) store.configRevision = result.revision
  }
  const wf = workflows.value.find((item) => item.id === id)
  if (!wf) {
    setMessage('工作流不存在', true)
    return
  }
  currentId.value = id
  workflow.value = JSON.parse(JSON.stringify(wf))
  workflow.value.steps = workflow.value.steps || []
  setMessage(makeActive ? `已切换到：${workflow.value.name}` : '')
}

async function onWorkflowChange(id) {
  await load(id, true)
}

// ===== 步骤编辑 =====
function updateStep(index, key, value) {
  workflow.value.steps[index][key] = value
  setMessage('有未保存的工作流更改')
}

function onNameUpdate(value) {
  workflow.value.name = String(value || '')
  setMessage('名称已修改，点击“保存”后生效')
}

function moveStep(index, offset) {
  const target = index + offset
  if (target < 0 || target >= workflow.value.steps.length) return
  const steps = workflow.value.steps
  ;[steps[index], steps[target]] = [steps[target], steps[index]]
  setMessage('有未保存的工作流更改')
}

function removeStep(index) {
  workflow.value.steps.splice(index, 1)
  setMessage('有未保存的工作流更改')
}

function addSelectedStep() {
  if (!selectedStep.value) return
  workflow.value.steps.push(defaultStep(selectedStep.value))
  setMessage(`已添加节点：${catalog.value.get(selectedStep.value)?.name || selectedStep.value}`)
}

async function toggleTray(enabled) {
  workflow.value.tray_menu = !!enabled
  if (!currentId.value) return
  const result = await api().update_workflow_meta(currentId.value, null, enabled)
  if (result.success === false) return setMessage(result.message, true)
  workflows.value = result.workflows || workflows.value
  if (result.revision) store.configRevision = result.revision
  setMessage(enabled ? '该工作流将显示在托盘菜单' : '已从托盘菜单隐藏')
}

// ===== 工作流操作 =====
function newWorkflow() {
  currentId.value = null
  workflow.value = {
    id: null,
    name: '新工作流',
    built_in: false,
    tray_menu: true,
    steps: [defaultStep('refresh_status')],
  }
  setMessage('正在编辑新工作流，点击“另存为独立功能”保存')
}

function applyResult(result, successMessage) {
  if (result.success === false) {
    setMessage(result.message, true)
    return false
  }
  workflows.value = result.workflows || workflows.value
  if (result.workflow) {
    currentId.value = result.workflow.id
    workflow.value = JSON.parse(JSON.stringify(result.workflow))
    workflow.value.steps = workflow.value.steps || []
  }
  if (result.active_workflow_id) currentId.value = result.active_workflow_id
  if (result.revision) store.configRevision = result.revision
  setMessage(successMessage || result.message || '已保存')
  return true
}

async function save() {
  if (!currentId.value) return saveAs()
  setMessage('正在保存...')
  try {
    // 名称与步骤一并提交：修复"改名后点保存，名称未持久化"的问题
    const result = await api().save_workflow(workflow.value.steps, currentId.value, workflow.value.name)
    applyResult(result)
  } catch (e) {
    setMessage('保存失败：' + e.message, true)
  }
}

async function saveAs() {
  const name = String(workflow.value.name || '').trim()
  if (!name) return setMessage('请输入工作流名称', true)
  const tray = workflow.value.tray_menu !== false
  setMessage('正在保存为独立工作流...')
  try {
    const result = await api().save_workflow_as(name, workflow.value.steps, tray)
    applyResult(result)
  } catch (e) {
    setMessage('保存失败：' + e.message, true)
  }
}

// ===== 复制工作流 =====
// 参考 Figma/Notion 的 Duplicate 模式：一键创建副本并自动选中，
// 聚焦名称输入框并全选，用户改个名字即可保存使用。
// 复制的是编辑器中的当前内容（所见即所得，含未保存的修改）。
const nameInputRef = ref(null)

async function duplicateWorkflow() {
  const name = String(workflow.value.name || '').trim()
  if (!name) return setMessage('请先输入工作流名称', true)
  if (!workflow.value.steps.length) return setMessage('暂无节点可复制', true)
  const copyName = `${name} 副本`
  const tray = workflow.value.tray_menu !== false
  setMessage('正在复制工作流...')
  try {
    const result = await api().save_workflow_as(copyName, workflow.value.steps, tray)
    if (applyResult(result, `已复制为「${copyName}」，可继续修改`)) {
      ui.toast(`已复制为「${copyName}」`, 'success')
      // 副本已选为当前编辑对象，聚焦名称框并全选，便于立即改名
      await nextTick()
      try {
        nameInputRef.value?.focus()
        nameInputRef.value?.select()
      } catch (e) {
        /* ignore */
      }
    }
  } catch (e) {
    setMessage('复制失败：' + e.message, true)
  }
}

async function removeWorkflow() {
  if (!currentId.value) return newWorkflow()
  const target = workflows.value.find((item) => item.id === currentId.value)
  if (target?.built_in) return setMessage('内置工作流不能删除', true)
  const ok = await ui.confirm(`确定删除工作流“${target?.name || currentId.value}”吗？`)
  if (!ok) return
  try {
    const result = await api().delete_workflow(currentId.value)
    if (result.success === false) return setMessage(result.message, true)
    workflows.value = result.workflows || []
    if (result.revision) store.configRevision = result.revision
    await load(result.active_workflow_id || 'default_auth', false)
    setMessage(result.message)
  } catch (e) {
    setMessage('删除失败：' + e.message, true)
  }
}

async function reset() {
  if (!currentId.value) return
  try {
    const result = await api().reset_workflow(currentId.value)
    applyResult(result)
  } catch (e) {
    setMessage('恢复失败：' + e.message, true)
  }
}

// 工作流总时限改动即保存
watch(
  () => store.form.auth_total_timeout,
  () => {
    if (!store.configLoaded) return
    doAutoSave()
  }
)

// 自初始化
watch(
  () => store.apiReady,
  async (ready) => {
    if (!ready || inited.value) return
    inited.value = true
    await init()
  },
  { immediate: true }
)
</script>

<template>
  <div class="workflow-view">
    <section class="card wf-card">
      <div class="we-header">
        <div>
          <div class="section-title">工作流编排</div>
          <div class="section-desc" style="margin-top: 4px">
            支持多个独立工作流、注销节点、WARP 服务重启节点；勾选托盘显示后会加入托盘菜单。可在现有工作流基础上「复制」后修改，快速创建变体。
          </div>
        </div>
      </div>

      <div class="we-meta">
        <div class="we-field">
          <label class="we-label">工作流</label>
          <n-select :value="currentId || ''" :options="workflowOptions" @update:value="onWorkflowChange" />
        </div>
        <div class="we-field">
          <label class="we-label">名称</label>
          <n-input ref="nameInputRef" :value="workflow.name" maxlength="60" :disabled="!!workflow.built_in"
            @update:value="onNameUpdate"
            :title="workflow.built_in ? '内置工作流名称不可修改，可复制为自定义工作流' : ''" />
        </div>
        <div class="we-tray">
          <n-checkbox :checked="workflow.tray_menu !== false" @update:checked="toggleTray">托盘菜单</n-checkbox>
        </div>
        <div class="we-timeout">
          <label class="we-label">总时限（秒）</label>
          <n-input-number v-model:value="store.form.auth_total_timeout" :min="30" :max="300" style="width: 100%" />
        </div>
      </div>

      <div class="we-actions">
        <n-select v-model:value="selectedStep" :options="stepOptions" filterable placeholder="选择节点类型"
          style="flex: 1; min-width: 200px" />
        <n-button @click="addSelectedStep">添加节点</n-button>
        <n-button @click="duplicateWorkflow" title="复制当前工作流为副本，可在此基础上修改">复制</n-button>
        <n-button @click="newWorkflow">新建</n-button>
        <n-button @click="saveAs">另存为独立功能</n-button>
        <n-button type="primary" @click="save">保存</n-button>
        <n-button @click="reset">恢复内置</n-button>
        <n-button type="error" secondary @click="removeWorkflow">删除</n-button>
      </div>

      <div class="we-steps">
        <div v-if="!workflow.steps.length" class="empty-hint">暂无节点，请添加</div>
        <div v-for="(step, index) in workflow.steps" :key="index" class="we-step">
          <n-checkbox :checked="step.enabled !== false"
            @update:checked="(v) => updateStep(index, 'enabled', v)" title="启用步骤" />
          <div class="we-step-info">
            <div class="we-step-name">{{ catalog.get(step.id)?.name || step.id }}</div>
            <div class="we-step-desc">{{ catalog.get(step.id)?.description || '' }}</div>
          </div>
          <div class="we-step-param">
            <label>超时</label>
            <n-input-number :value="Number(step.timeout || 15)" :min="1" :max="180" size="small"
              @update:value="(v) => updateStep(index, 'timeout', Number(v))" />
          </div>
          <div class="we-step-param">
            <label>重试</label>
            <n-input-number :value="Number(step.retries || 0)" :min="0" :max="5" size="small"
              @update:value="(v) => updateStep(index, 'retries', Number(v))" />
          </div>
          <div class="we-step-param">
            <label>延迟</label>
            <n-input-number :value="Number(step.retry_delay ?? 1)" :min="0" :max="30" :step="0.5" size="small"
              @update:value="(v) => updateStep(index, 'retry_delay', Number(v))" />
          </div>
          <div class="we-step-param check">
            <label>跳过错误</label>
            <n-checkbox :checked="!!step.continue_on_error"
              @update:checked="(v) => updateStep(index, 'continue_on_error', v)" />
          </div>
          <div class="we-step-ops">
            <n-button size="tiny" quaternary :disabled="index === 0" @click="moveStep(index, -1)">↑</n-button>
            <n-button size="tiny" quaternary :disabled="index === workflow.steps.length - 1"
              @click="moveStep(index, 1)">↓</n-button>
            <n-button size="tiny" quaternary type="error" @click="removeStep(index)">×</n-button>
          </div>
        </div>
      </div>

      <div class="we-message" :class="{ error: messageError }">{{ message }}</div>
    </section>
  </div>
</template>

<style scoped>
.workflow-view {
  padding: 20px 22px 30px;
  max-width: 1080px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.wf-card {
  padding: 18px 20px;
}

.we-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.we-meta {
  display: grid;
  grid-template-columns: 1fr 1fr auto auto;
  gap: 10px;
  align-items: end;
  margin-top: 14px;
}

.we-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.we-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.we-tray {
  padding-bottom: 4px;
  white-space: nowrap;
}

.we-timeout {
  display: flex;
  flex-direction: column;
  gap: 5px;
  width: 110px;
}

.we-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.we-steps {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 12px;
}

.we-step {
  display: grid;
  grid-template-columns: 24px minmax(150px, 1fr) 92px 92px 92px 76px auto;
  gap: 8px;
  align-items: center;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elevated);
}

.we-step-info {
  min-width: 0;
}

.we-step-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.we-step-desc {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.we-step-param {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.we-step-param.check {
  align-items: center;
}

.we-step-param label {
  font-size: 10px;
  color: var(--text-tertiary);
}

.we-step-ops {
  display: flex;
  gap: 2px;
}

.we-message {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 9px;
  min-height: 14px;
}

.we-message.error {
  color: var(--error);
}
</style>
