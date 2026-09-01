<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { NButton, NInput, NInputNumber, NSelect, NCheckbox, NSwitch, NTooltip } from 'naive-ui'
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
// 编辑器草稿是否有未保存改动（自动调优回写配置后只在"干净"时同步回显）
const dirty = ref(false)
// 保存/复制进行中：防止双击连发创建出重复副本
const saving = ref(false)

// ===== 计时统计 / 自动调优 =====
// {step_id: {runs, avg_elapsed, avg_retries, score, suggested_timeout, suggested_retries}}
const stepStats = ref({})
const autoTune = ref(false)

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

// 一键应用调优建议的加载态
const applyingTune = ref(false)

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
  dirty.value = false
  setMessage(makeActive ? `已切换到：${workflow.value.name}` : '')
  await refreshStats()
}

async function onWorkflowChange(id) {
  await load(id, true)
}

// ===== 计时统计 =====
async function refreshStats() {
  if (!currentId.value) {
    stepStats.value = {}
    return
  }
  try {
    const data = await api().get_workflow_stats(currentId.value)
    stepStats.value = data.steps || {}
    autoTune.value = !!data.auto_tune
  } catch (e) {
    console.warn('get_workflow_stats failed:', e)
  }
}

function statsOf(stepId) {
  const st = stepStats.value[stepId]
  return st && st.runs ? st : null
}

// 稳定度 → 绿/橙/红渐变：得分高（耗时短、重试少）偏绿，反之为红。
// 评分权重（重试 70% / 耗时 30%）由后端 stability_score 决定，这里只负责着色。
function stabilityColor(score) {
  const t = Math.max(0, Math.min(100, Number(score) || 0))
  // 绿(145,63%,42%) → 橙(38,90%,50%) → 红(4,76%,55%)，两段线性插值
  const lerp = (a, b, k) => a + (b - a) * k
  let h, s, l
  if (t >= 50) {
    const k = (t - 50) / 50
    h = lerp(38, 145, k)
    s = lerp(90, 63, k)
    l = lerp(50, 42, k)
  } else {
    const k = t / 50
    h = lerp(4, 38, k)
    s = lerp(76, 90, k)
    l = lerp(55, 50, k)
  }
  return `hsl(${Math.round(h)}, ${Math.round(s)}%, ${Math.round(l)}%)`
}

function stabilityLabel(score) {
  if (score >= 80) return '稳定'
  if (score >= 60) return '一般'
  if (score >= 40) return '偏慢'
  return '不稳定'
}

function railColor(stepId) {
  const st = statsOf(stepId)
  return st ? stabilityColor(st.score) : ''
}

function scoreChipStyle(stepId) {
  const st = statsOf(stepId)
  if (!st) return {}
  const c = stabilityColor(st.score)
  return {
    color: c,
    borderColor: `color-mix(in srgb, ${c} 45%, transparent)`,
    background: `color-mix(in srgb, ${c} 13%, transparent)`,
  }
}

function fmtSec(x) {
  return `${Math.round(Number(x || 0) * 100) / 100}s`
}

function fmtNum(x) {
  return String(Math.round(Number(x || 0) * 100) / 100)
}

async function onAutoTuneUpdate(enabled) {
  try {
    const result = await api().set_workflow_auto_tune(enabled)
    if (result.success === false) {
      setMessage(result.message, true)
      return
    }
    autoTune.value = !!enabled
    if (result.revision) store.configRevision = result.revision
    setMessage(
      enabled
        ? '已开启自动调优：每次运行后按实测数据调整各节点超时与重试'
        : '已关闭自动调优：超时与重试保持手动设置'
    )
  } catch (e) {
    setMessage('自动调优开关设置失败：' + e.message, true)
  }
}

// 一键应用：把当前已有的调优建议立即写回该工作流配置（无需再手动保存）
async function applyTuning() {
  if (!currentId.value || applyingTune.value) return
  applyingTune.value = true
  try {
    const result = await api().apply_workflow_tuning(currentId.value)
    if (result.success === false) return setMessage(result.message || '应用失败', true)
    if (result.revision) store.configRevision = result.revision
    const changes = result.changes || []
    if (!changes.length) {
      setMessage(
        '暂无可应用的调优建议：每个节点需累计运行 ≥3 次（超时建议）/ ≥5 次（重试建议）才会产生建议，或与当前值差异过小无需调整'
      )
      await refreshStats()
      return
    }
    await refreshStats()
    if (!dirty.value) await syncWorkflowDefinitions()
    const stepName = (id) => catalog.value.get(id)?.name || id
    const desc = changes
      .map((c) => `${stepName(c.id)}：超时 ${c.timeout_from}→${c.timeout}s、重试 ${c.retries_from}→${c.retries} 次`)
      .join('；')
    setMessage(`已应用 ${changes.length} 项调优：${desc}`)
  } catch (e) {
    setMessage('应用调优失败：' + e.message, true)
  } finally {
    applyingTune.value = false
  }
}

// ===== 步骤编辑 =====
function updateStep(index, key, value) {
  workflow.value.steps[index][key] = value
  dirty.value = true
  setMessage('有未保存的工作流更改')
}

function onNameUpdate(value) {
  workflow.value.name = String(value || '')
  dirty.value = true
  setMessage('名称已修改，点击“保存”后生效')
}

function moveStep(index, offset) {
  const target = index + offset
  if (target < 0 || target >= workflow.value.steps.length) return
  const steps = workflow.value.steps
  ;[steps[index], steps[target]] = [steps[target], steps[index]]
  dirty.value = true
  setMessage('有未保存的工作流更改')
}

function removeStep(index) {
  workflow.value.steps.splice(index, 1)
  dirty.value = true
  setMessage('有未保存的工作流更改')
}

function addSelectedStep() {
  if (!selectedStep.value) return
  workflow.value.steps.push(defaultStep(selectedStep.value))
  dirty.value = true
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
  dirty.value = true
  stepStats.value = {}
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
  dirty.value = false
  setMessage(successMessage || result.message || '已保存')
  refreshStats()
  return true
}

async function save() {
  if (!currentId.value) return saveAs()
  if (saving.value) return
  saving.value = true
  setMessage('正在保存...')
  try {
    // 名称与步骤一并提交：修复"改名后点保存，名称未持久化"的问题
    const result = await api().save_workflow(workflow.value.steps, currentId.value, workflow.value.name)
    applyResult(result)
  } catch (e) {
    setMessage('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

async function saveAs() {
  if (saving.value) return
  const name = String(workflow.value.name || '').trim()
  if (!name) return setMessage('请输入工作流名称', true)
  const tray = workflow.value.tray_menu !== false
  saving.value = true
  setMessage('正在保存为独立工作流...')
  try {
    const result = await api().save_workflow_as(name, workflow.value.steps, tray)
    applyResult(result)
  } catch (e) {
    setMessage('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

// ===== 复制工作流 =====
// 参考 Figma/Notion 的 Duplicate 模式：一键创建副本并自动选中，
// 聚焦名称输入框并全选，用户改个名字即可保存使用。
// 复制的是编辑器中的当前内容（所见即所得，含未保存的修改）。
// saving 防抖：双击"复制"会连发两次 save_workflow_as，凭空多出
// 一个同名副本（副本 id 会加后缀，配置里出现两条一模一样的条目）。
const nameInputRef = ref(null)

async function duplicateWorkflow() {
  if (saving.value) return
  const name = String(workflow.value.name || '').trim()
  if (!name) return setMessage('请先输入工作流名称', true)
  if (!workflow.value.steps.length) return setMessage('暂无节点可复制', true)
  const copyName = `${name} 副本`
  const tray = workflow.value.tray_menu !== false
  saving.value = true
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
  } finally {
    saving.value = false
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

// ===== 运行结束后的数据同步 =====
// 认证/恢复结束后：统计库里多了新样本 → 刷新耗时/重试/稳定度；
// 若开启了自动调优，后端已把新参数写回配置，编辑器在"干净"时同步回显。
watch(
  () => store.authRunning,
  (running, wasRunning) => {
    if (running || !wasRunning || !inited.value) return
    refreshStats()
    if (autoTune.value && !dirty.value && currentId.value) syncWorkflowDefinitions()
  }
)

async function syncWorkflowDefinitions() {
  try {
    const data = await api().get_workflow_catalog()
    workflows.value = data.workflows || workflows.value
    const wf = workflows.value.find((item) => item.id === currentId.value)
    if (!wf) return
    workflow.value = JSON.parse(JSON.stringify(wf))
    workflow.value.steps = workflow.value.steps || []
  } catch (e) {
    console.warn('syncWorkflowDefinitions failed:', e)
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
          <div class="section-desc">
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
          class="we-step-picker" />
        <n-button @click="addSelectedStep">添加节点</n-button>
        <n-button @click="duplicateWorkflow" :disabled="saving" title="复制当前工作流为副本，可在此基础上修改">复制</n-button>
        <n-button @click="newWorkflow">新建</n-button>
        <n-button @click="saveAs" :disabled="saving">另存为独立功能</n-button>
        <n-button type="primary" @click="save" :disabled="saving">保存</n-button>
        <n-button @click="reset">恢复内置</n-button>
        <n-button type="error" secondary @click="removeWorkflow">删除</n-button>
      </div>

      <!-- 自动调优：按每次运行的节点耗时/重试数据自动整定超时与重试 -->
      <div class="wf-tune">
        <div class="wf-tune-left">
          <div class="wf-tune-switch">
            <span class="wf-tune-title">自动调优</span>
            <n-switch :value="autoTune" size="small" @update:value="onAutoTuneUpdate" />
          </div>
          <span class="wf-tune-desc">
            记录每个节点的耗时与重试，按
            <n-tooltip trigger="hover">
              <template #trigger>
                <span class="wf-tune-link">RFC 6298 超时估计</span>
              </template>
              超时 = 平滑平均耗时 + 4 × 平均偏差，另加 25% 余量；<br />
              重试按几何分布模型取「达到 95% 累计成功率」所需的最少次数。
            </n-tooltip>
            自动整定。每个节点累计运行 ≥3 次产生超时建议、≥5 次产生重试建议；开启后每次运行结束自动应用，也可随时点右侧按钮一键应用。
          </span>
        </div>
        <div class="wf-tune-right">
          <n-tooltip trigger="hover">
            <template #trigger>
              <div class="wf-legend">
                <span class="wf-legend-label">节点稳定度</span>
                <span class="wf-legend-end">稳定</span>
                <div class="wf-legend-bar"></div>
                <span class="wf-legend-end">不稳定</span>
              </div>
            </template>
            稳定度 = 100 −（70% × 平均重试因子 + 30% × 耗时因子）。<br />
            重试意味着节点本身不稳定，权重高于耗时；<br />
            颜色越绿表示耗时越短、重试越少。
          </n-tooltip>
          <n-tooltip v-if="currentId" trigger="hover">
            <template #trigger>
              <n-button size="small" secondary :loading="applyingTune" @click="applyTuning">
                一键应用调优
              </n-button>
            </template>
            把当前已有的调优建议立即写回该工作流（即时生效，无需保存）。<br />
            样本不足的节点（运行 &lt;3 次 / &lt;5 次）不会产生建议。
          </n-tooltip>
        </div>
      </div>

      <div class="we-steps">
        <div v-if="!workflow.steps.length" class="empty-hint">暂无节点，请添加</div>
        <div v-for="(step, index) in workflow.steps" :key="index" class="we-step"
          :style="railColor(step.id) ? { borderLeftColor: railColor(step.id) } : {}">
          <div class="we-step-main">
            <n-checkbox :checked="step.enabled !== false"
              @update:checked="(v) => updateStep(index, 'enabled', v)" title="启用步骤" />
            <div class="we-step-info">
              <div class="we-step-name">{{ catalog.get(step.id)?.name || step.id }}</div>
              <div class="we-step-desc">{{ catalog.get(step.id)?.description || '' }}</div>
            </div>
          </div>
          <div class="we-step-params">
            <div class="we-param">
              <label>超时（秒）</label>
              <n-input-number :value="Number(step.timeout || 15)" :min="1" :max="180" size="small"
                @update:value="(v) => updateStep(index, 'timeout', Number(v))" />
            </div>
            <div class="we-param">
              <label>重试</label>
              <n-input-number :value="Number(step.retries || 0)" :min="0" :max="5" size="small"
                @update:value="(v) => updateStep(index, 'retries', Number(v))" />
            </div>
            <div class="we-param">
              <label>重试间隔（秒）</label>
              <n-input-number :value="Number(step.retry_delay ?? 1)" :min="0" :max="30" :step="0.5" size="small"
                @update:value="(v) => updateStep(index, 'retry_delay', Number(v))" />
            </div>
            <div class="we-param-check" title="该节点失败时继续执行后续节点">
              <n-checkbox :checked="!!step.continue_on_error"
                @update:checked="(v) => updateStep(index, 'continue_on_error', v)">允许跳过</n-checkbox>
            </div>
          </div>
          <div class="we-step-ops">
            <n-button size="tiny" quaternary :disabled="index === 0" @click="moveStep(index, -1)">↑</n-button>
            <n-button size="tiny" quaternary :disabled="index === workflow.steps.length - 1"
              @click="moveStep(index, 1)">↓</n-button>
            <n-button size="tiny" quaternary type="error" @click="removeStep(index)">×</n-button>
          </div>

          <!-- 计时统计：累计平均耗时 / 平均重试 / 稳定度着色 -->
          <div class="we-step-stats">
            <template v-if="statsOf(step.id)">
              <span class="wf-chip wf-chip-score" :style="scoreChipStyle(step.id)">
                <i class="wf-chip-dot" :style="{ background: stabilityColor(statsOf(step.id).score) }"></i>
                {{ stabilityLabel(statsOf(step.id).score) }} · {{ statsOf(step.id).score }} 分
              </span>
              <span class="wf-chip">平均耗时 <b class="mono">{{ fmtSec(statsOf(step.id).avg_elapsed) }}</b></span>
              <span class="wf-chip">平均重试 <b class="mono">{{ fmtNum(statsOf(step.id).avg_retries) }}</b> 次</span>
              <span class="wf-chip wf-chip-dim">运行 {{ statsOf(step.id).runs }} 次</span>
              <template v-if="!autoTune">
                <span v-if="statsOf(step.id).suggested_timeout != null" class="wf-chip wf-chip-hint">
                  建议超时 {{ statsOf(step.id).suggested_timeout }}s
                </span>
                <span v-if="statsOf(step.id).suggested_retries != null" class="wf-chip wf-chip-hint">
                  建议重试 {{ statsOf(step.id).suggested_retries }} 次
                </span>
              </template>
            </template>
            <span v-else class="wf-stats-empty">
              {{ currentId ? '暂无运行数据 · 运行一次后开始累计统计' : '保存工作流后开始累计运行统计' }}
            </span>
          </div>
        </div>
      </div>

      <div class="we-message" :class="{ error: messageError }">{{ message }}</div>
    </section>
  </div>
</template>

<style scoped>
.workflow-view {
  container-type: inline-size;
  padding: 20px 22px 30px;
  padding: clamp(14px, 2cqi, 26px) clamp(14px, 2.4cqi, 30px) 30px;
  max-width: 1080px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.wf-card {
  padding: 20px 22px;
}

.we-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.we-meta {
  display: grid;
  grid-template-columns: 1.25fr 1.25fr auto 130px;
  gap: 12px;
  align-items: end;
  margin-top: 16px;
}

.we-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.we-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.we-tray {
  padding-bottom: 6px;
  white-space: nowrap;
  font-size: 12px;
}

.we-timeout {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.we-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
}

.we-step-picker {
  flex: 1;
  min-width: 220px;
}

/* ===== 自动调优栏 ===== */
.wf-tune {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 16px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-elevated);
}

.wf-tune-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  min-width: 0;
}

.wf-tune-switch {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wf-tune-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.wf-tune-desc {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.wf-tune-link {
  color: var(--text-primary);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  cursor: help;
}

.wf-tune-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.wf-legend {
  display: flex;
  align-items: center;
  gap: 7px;
  cursor: help;
}

.wf-legend-label {
  font-size: 12px;
  color: var(--text-secondary);
}

.wf-legend-bar {
  width: 86px;
  height: 7px;
  border-radius: 4px;
  background: linear-gradient(90deg,
      hsl(145, 63%, 42%),
      hsl(38, 90%, 50%),
      hsl(4, 76%, 55%));
}

.wf-legend-end {
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ===== 节点列表 ===== */
.we-steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.we-step {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  grid-template-areas:
    "main params ops"
    "stats stats stats";
  gap: 6px 12px;
  align-items: center;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: 10px;
  background: var(--bg-elevated);
  transition: border-color 0.15s ease;
}

.we-step:hover {
  border-color: var(--border-strong);
}

.we-step-main {
  grid-area: main;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.we-step-info {
  min-width: 0;
}

.we-step-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.we-step-desc {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.we-step-params {
  grid-area: params;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.we-param {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 96px;
}

.we-param label {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-secondary);
}

.we-param-check {
  padding-bottom: 5px;
  font-size: 12px;
  white-space: nowrap;
}

.we-step-ops {
  grid-area: ops;
  display: flex;
  gap: 2px;
}

.we-step-stats {
  grid-area: stats;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  padding-top: 9px;
  border-top: 1px dashed var(--border);
}

/* ===== 统计 chips ===== */
.wf-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 9px;
  font-size: 11px;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-panel);
  white-space: nowrap;
}

.wf-chip b {
  font-weight: 600;
  font-size: 11px;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.wf-chip-dim {
  color: var(--text-tertiary);
}

.wf-chip-score {
  font-weight: 600;
}

.wf-chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.wf-chip-hint {
  border-style: dashed;
  color: var(--text-primary);
}

.wf-stats-empty {
  font-size: 11px;
  color: var(--text-tertiary);
}

.we-message {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 12px;
  min-height: 16px;
  line-height: 1.6;
}

.we-message.error {
  color: var(--error);
}

/* 窄容器：参数区换行到信息区下方，避免挤压 */
@container (max-width: 860px) {
  .we-step {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "main ops"
      "params params"
      "stats stats";
  }
}
</style>
