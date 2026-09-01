<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
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

// ===== 节点选中 / 剪贴板 / 撤销 =====
// 选中集保存"节点对象 → 自增 uid"的映射键（WeakMap，不写入配置）：
// 用 uid 而非数组下标，增删移动后选中关系不会错位。
const selection = ref(new Set())
let uidSeq = 0
const uidMap = new WeakMap()
function uidOf(step) {
  let uid = uidMap.get(step)
  if (uid == null) {
    uid = ++uidSeq
    uidMap.set(step, uid)
  }
  return uid
}
const clipboard = ref([])
const undoStack = ref([])
let lastUndoTag = ''
let lastUndoAt = 0

// ===== 拖选 / 拖拽排序 / 放置指示线 =====
const stepsWrap = ref(null)
const marquee = ref(null) // {x0,y0,x1,y1}（相对 .wf-steps 的局部坐标）
const dropIndex = ref(null)
const dropTop = ref(0)
const hoverList = ref(false)
const dragMode = ref(null) // 'marquee' | 'move'
let session = null // 非响应式：mousedown→mouseup 之间的一次指针会话
let anchorIndex = null
let lastPointerY = 0

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

// 工作流整体被替换（切换/保存/同步）后，旧的 uid 与撤销栈已失效，一并重置
function resetEditorState() {
  selection.value.clear()
  undoStack.value = []
  lastUndoTag = ''
  anchorIndex = null
  dropIndex.value = null
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
  resetEditorState()
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

// ===== 撤销栈 =====
// tag 相同且间隔 <900ms 的连续编辑（如在数字框里连点箭头）合并为一步，
// 否则撤销栈会被单次参数微调刷满。
function pushUndo(tag) {
  const now = Date.now()
  if (tag && tag === lastUndoTag && now - lastUndoAt < 900) {
    lastUndoAt = now
    return
  }
  lastUndoTag = tag || ''
  lastUndoAt = now
  undoStack.value.push(JSON.parse(JSON.stringify(workflow.value.steps)))
  if (undoStack.value.length > 80) undoStack.value.shift()
}

function undo() {
  const prev = undoStack.value.pop()
  if (!prev) return setMessage('没有可撤销的操作')
  workflow.value.steps = prev
  selection.value.clear()
  lastUndoTag = ''
  dirty.value = true
  setMessage('已撤销上一步操作')
}

// ===== 节点选中 =====
const stepRows = computed(() =>
  workflow.value.steps.map((step, index) => {
    const st = statsOf(step.id)
    const meta = catalog.value.get(step.id)
    const rail = st ? stabilityColor(st.score) : ''
    return {
      step,
      index,
      uid: uidOf(step),
      name: meta?.name || step.id,
      desc: meta?.description || '',
      stats: st,
      rail,
      scoreStyle: scoreChipStyle(step.id),
      elapsed: st ? fmtSec(st.avg_elapsed) : '',
      retries: st ? fmtNum(st.avg_retries) : '',
      selected: selection.value.has(uidOf(step)),
    }
  })
)

const selIndices = computed(() => {
  const out = []
  for (const row of stepRows.value) if (row.selected) out.push(row.index)
  return out
})
const selCount = computed(() => selIndices.value.length)
const selSteps = computed(() => selIndices.value.map((i) => workflow.value.steps[i]))
const canMoveUp = computed(() => selCount.value > 0 && selIndices.value[0] > 0)
const canMoveDown = computed(
  () => selCount.value > 0 && selIndices.value[selCount.value - 1] < workflow.value.steps.length - 1
)
const allSkip = computed(
  () => selCount.value > 0 && selSteps.value.every((s) => !!s.continue_on_error)
)
const someSkip = computed(
  () => !allSkip.value && selSteps.value.some((s) => !!s.continue_on_error)
)

function clearSelection() {
  selection.value.clear()
  anchorIndex = null
}

function selectAll() {
  selection.value = new Set(workflow.value.steps.map(uidOf))
  setMessage(`已全选 ${workflow.value.steps.length} 个节点`)
}

// ===== 节点增删改 =====
function markDirty(msg) {
  dirty.value = true
  setMessage(msg || '有未保存的工作流更改')
}

function updateStep(index, key, value) {
  pushUndo(`p:${uidOf(workflow.value.steps[index])}:${key}`)
  workflow.value.steps[index][key] = value
  markDirty()
}

function onNameUpdate(value) {
  workflow.value.name = String(value || '')
  dirty.value = true
  setMessage('名称已修改，点击“保存”后生效')
}

function insertSteps(index, items, msg) {
  pushUndo()
  const at = Math.max(0, Math.min(index, workflow.value.steps.length))
  workflow.value.steps.splice(at, 0, ...items)
  selection.value = new Set(items.map(uidOf))
  markDirty(msg || `已添加 ${items.length} 个节点`)
}

function addAt(index) {
  if (!selectedStep.value) return setMessage('请先选择要添加的节点类型', true)
  const name = catalog.value.get(selectedStep.value)?.name || selectedStep.value
  insertSteps(index, [defaultStep(selectedStep.value)], `已在位置 ${index + 1} 添加节点：${name}`)
}

function appendStep() {
  addAt(workflow.value.steps.length)
}

function deleteSelected(msg) {
  const idxs = selIndices.value
  if (!idxs.length) return
  pushUndo()
  const drop = new Set(idxs)
  workflow.value.steps = workflow.value.steps.filter((_, i) => !drop.has(i))
  selection.value.clear()
  anchorIndex = null
  // 模板里 @click 裸绑函数名时，Vue 会把 PointerEvent 当作第一个参数传进来——
  // 只接受字符串消息，否则消息条就会出现 "[object PointerEvent]"
  const text = typeof msg === 'string' && msg ? msg : `已删除 ${idxs.length} 个节点`
  markDirty(text)
}

// 整体上下移动：多选时保持组内相对顺序，未被选中的节点依次让位
function moveSelected(offset) {
  const idxs = selIndices.value
  if (!idxs.length) return
  const total = workflow.value.steps.length
  if (offset < 0 && idxs[0] === 0) return
  if (offset > 0 && idxs[idxs.length - 1] === total - 1) return
  pushUndo()
  const arr = [...workflow.value.steps]
  if (offset < 0) {
    for (const i of idxs) {
      const t = arr[i]
      arr[i] = arr[i - 1]
      arr[i - 1] = t
    }
  } else {
    for (let k = idxs.length - 1; k >= 0; k--) {
      const i = idxs[k]
      const t = arr[i]
      arr[i] = arr[i + 1]
      arr[i + 1] = t
    }
  }
  workflow.value.steps = arr
  markDirty(`已移动 ${idxs.length} 个节点`)
}

// 拖拽排序：把选中块整体搬到目标插入点（gap 下标）
function moveIndicesTo(indices, target) {
  const steps = workflow.value.steps
  const sorted = [...new Set(indices)].sort((a, b) => a - b)
  const drop = new Set(sorted)
  const items = sorted.map((i) => steps[i])
  const rest = steps.filter((_, i) => !drop.has(i))
  // 目标 gap 下标是按原数组算的，剔除前方的被搬走节点后才是新数组的下标
  const adj = target - sorted.filter((i) => i < target).length
  const next = [...rest.slice(0, adj), ...items, ...rest.slice(adj)]
  if (next.length === steps.length && next.every((s, i) => s === steps[i])) return
  pushUndo()
  workflow.value.steps = next
  markDirty(`已移动 ${items.length} 个节点`)
}

function toggleSkipSelected() {
  const items = selSteps.value
  if (!items.length) return
  const target = !items.every((s) => !!s.continue_on_error)
  pushUndo()
  items.forEach((s) => {
    s.continue_on_error = target
  })
  markDirty(
    target ? `已设置 ${items.length} 个节点失败时跳过` : `已取消 ${items.length} 个节点的跳过设置`
  )
}

// ===== 复制 / 剪切 / 粘贴 =====
function copySelected() {
  const items = selSteps.value
  if (!items.length) return
  clipboard.value = items.map((s) => JSON.parse(JSON.stringify(s)))
  setMessage(`已复制 ${items.length} 个节点 · 移动鼠标到目标位置后按 Ctrl+V 粘贴`)
}

function cutSelected() {
  if (!selCount.value) return
  copySelected()
  deleteSelected(`已剪切 ${clipboard.value.length} 个节点`)
}

// at == null 时落在最后一个选中节点之后（列表为空则落到末尾）
function pasteSteps(at) {
  if (!clipboard.value.length) return
  let idx = at
  if (idx == null) {
    const last = selIndices.value[selIndices.value.length - 1]
    idx = last == null ? workflow.value.steps.length : last + 1
  }
  const items = clipboard.value.map((s) => JSON.parse(JSON.stringify(s)))
  insertSteps(idx, items, `已在位置 ${Math.max(0, Math.min(idx, workflow.value.steps.length)) + 1} 粘贴 ${items.length} 个节点`)
}

// ===== 指针会话：框选 / 拖拽排序 / 放置指示线 =====
function rowElements() {
  return stepsWrap.value ? Array.from(stepsWrap.value.querySelectorAll('.wf-row')) : []
}

function computeDropIndex(clientY) {
  const rows = rowElements()
  if (!rows.length) return 0
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i].getBoundingClientRect()
    if (clientY < r.top + r.height / 2) return i
  }
  return rows.length
}

function updateDropTop() {
  const idx = dropIndex.value
  const el = stepsWrap.value
  if (idx == null || !el) {
    dropTop.value = 0
    return
  }
  const rows = rowElements()
  const base = el.getBoundingClientRect()
  if (!rows.length) {
    dropTop.value = 0
    return
  }
  if (idx >= rows.length) {
    const r = rows[rows.length - 1].getBoundingClientRect()
    dropTop.value = r.bottom - base.top + 2
  } else {
    const r = rows[idx].getBoundingClientRect()
    dropTop.value = Math.max(0, r.top - base.top - 2)
  }
}

function setDropIndex(clientY) {
  dropIndex.value = computeDropIndex(clientY)
  updateDropTop()
}

function isInteractive(target) {
  if (!target || !target.closest) return false
  return !!target.closest(
    'input, textarea, button, a, select, .n-base-selection, .n-checkbox, [role="button"], .wf-grip, .wf-num'
  )
}

function scrollHost() {
  let el = stepsWrap.value
  while (el && el !== document.body) {
    const ov = getComputedStyle(el).overflowY
    if ((ov === 'auto' || ov === 'scroll') && el.scrollHeight > el.clientHeight + 2) return el
    el = el.parentElement
  }
  return null
}

// 拖到列表上下边缘时自动滚动，长列表跨屏搬运才可行
function autoScroll(clientY) {
  const host = scrollHost()
  if (!host) return
  const r = host.getBoundingClientRect()
  const EDGE = 52
  if (clientY < r.top + EDGE) host.scrollTop -= 16
  else if (clientY > r.bottom - EDGE) host.scrollTop += 16
}

// 选中语义对齐 Windows 资源管理器：
// 单击 = 只选它；Ctrl+单击 = 互斥切换（已选则取消，未选则加入）；
// Shift+单击 = 以锚点为界整段替换（不移动锚点）；
// 列表空白处单击 = 全部取消；点列表外其他区域 = 取消。
// 在已选中项上普通按下不立即收拢，等 mouseup 且未发生拖拽再收拢为单选，
// 这样按住已选节点拖动仍能带走整组。
function onRowMouseDown(e, index) {
  if (e.button !== 0) return
  if (isInteractive(e.target)) return
  e.preventDefault() // 阻止拖拽时选中文本
  const uid = uidOf(workflow.value.steps[index])
  const additive = e.ctrlKey || e.metaKey
  const rangePick = e.shiftKey && anchorIndex != null

  if (rangePick) {
    const [a, b] = anchorIndex <= index ? [anchorIndex, index] : [index, anchorIndex]
    // Ctrl/⌘+Shift：范围并入现有选中（资源管理器语义）；纯 Shift：整段替换
    const next = additive ? new Set(selection.value) : new Set()
    for (let i = a; i <= b; i++) next.add(uidOf(workflow.value.steps[i]))
    selection.value = next
  } else if (additive) {
    const next = new Set(selection.value)
    if (next.has(uid)) next.delete(uid)
    else next.add(uid)
    selection.value = next
  } else if (!selection.value.has(uid)) {
    selection.value = new Set([uid])
  }

  if (!rangePick) anchorIndex = index
  session = {
    type: 'marquee',
    x0: e.clientX,
    y0: e.clientY,
    moved: false,
    base: new Set(selection.value),
    additive: additive || rangePick,
    clickedUid: uid,
    wasSelected: selection.value.has(uid),
  }
  dragMode.value = 'marquee'
  bindSession()
}

// 列表空白处（最后一行下方）按下：清空选择并从空白处开始框选
function onStepsMouseDown(e) {
  if (e.button !== 0) return
  if (e.target.closest('.wf-row')) return
  e.preventDefault()
  const additive = e.ctrlKey || e.metaKey || e.shiftKey
  if (!additive) selection.value = new Set()
  anchorIndex = null
  session = {
    type: 'marquee',
    x0: e.clientX,
    y0: e.clientY,
    moved: false,
    base: new Set(selection.value),
    additive,
  }
  dragMode.value = 'marquee'
  bindSession()
}

// 点到列表与节点操作栏之外的其他区域：取消选中（下拉面板、弹窗除外）
function onDocMouseDown(e) {
  if (e.button !== 0) return
  if (store.activeTab !== 'workflow') return
  if (!selection.value.size) return
  const t = e.target
  if (!t || !t.closest) return
  if (
    t.closest(
      '.wf-steps, .wf-node-bar, .n-base-select-menu, .v-binder-follower-container, .n-modal, .n-drawer, .n-card'
    )
  ) {
    return
  }
  selection.value.clear()
  anchorIndex = null
}

function onGripMouseDown(e, index) {
  if (e.button !== 0) return
  e.preventDefault()
  e.stopPropagation()
  const uid = uidOf(workflow.value.steps[index])
  if (!selection.value.has(uid)) selection.value = new Set([uid])
  anchorIndex = index
  session = {
    type: 'move',
    x0: e.clientX,
    y0: e.clientY,
    moved: false,
    indices: selIndices.value.slice(),
  }
  dragMode.value = 'move'
  setDropIndex(e.clientY)
  bindSession()
}

function applyMarquee(base) {
  const m = marquee.value
  if (!m) return
  const top = Math.min(m.y0, m.y1)
  const bottom = Math.max(m.y0, m.y1)
  const next = new Set(base)
  const rows = rowElements()
  rows.forEach((row, i) => {
    const rt = row.offsetTop
    const rb = rt + row.offsetHeight
    if (rb >= top && rt <= bottom) next.add(uidOf(workflow.value.steps[i]))
  })
  selection.value = next
}

function onDocMouseMove(e) {
  if (!session) return
  lastPointerY = e.clientY
  const dx = e.clientX - session.x0
  const dy = e.clientY - session.y0
  if (!session.moved && Math.abs(dx) + Math.abs(dy) < 4) return
  session.moved = true
  if (session.type === 'marquee') {
    const base = stepsWrap.value?.getBoundingClientRect()
    if (!base) return
    marquee.value = {
      x0: session.x0 - base.left,
      y0: session.y0 - base.top,
      x1: e.clientX - base.left,
      y1: e.clientY - base.top,
    }
    applyMarquee(session.base)
  } else {
    setDropIndex(e.clientY)
    autoScroll(e.clientY)
  }
}

function onDocMouseUp() {
  if (!session) return
  const s = session
  session = null
  dragMode.value = null
  marquee.value = null
  unbindSession()
  if (s.type === 'marquee') {
    // 在已选中的节点上普通按下且未拖动：收拢为只选它（资源管理器语义）
    if (!s.moved && !s.additive && s.wasSelected) selection.value = new Set([s.clickedUid])
  }
  if (s.type === 'move') {
    if (s.moved && dropIndex.value != null) moveIndicesTo(s.indices, dropIndex.value)
    // 结束后按当前指针位置重算指示线（剪贴板非空时继续提示粘贴落点）
    if (clipboard.value.length) setDropIndex(lastPointerY)
    else dropIndex.value = null
  }
}

function bindSession() {
  document.addEventListener('mousemove', onDocMouseMove)
  document.addEventListener('mouseup', onDocMouseUp)
}

function unbindSession() {
  document.removeEventListener('mousemove', onDocMouseMove)
  document.removeEventListener('mouseup', onDocMouseUp)
}

// 列表内悬停：剪贴板非空时实时指示粘贴落点
function onListMouseMove(e) {
  lastPointerY = e.clientY
  if (session) return
  if (clipboard.value.length) setDropIndex(e.clientY)
}

function onListMouseEnter(e) {
  hoverList.value = true
  lastPointerY = e.clientY
}

function onListMouseLeave() {
  hoverList.value = false
  if (!session) dropIndex.value = null
}

const indicatorVisible = computed(() => {
  if (dropIndex.value == null || !stepsWrap.value) return false
  if (dragMode.value === 'move') return true
  return clipboard.value.length > 0 && hoverList.value && !dragMode.value
})

const marqueeStyle = computed(() => {
  const m = marquee.value
  if (!m) return {}
  return {
    left: Math.min(m.x0, m.x1) + 'px',
    top: Math.min(m.y0, m.y1) + 'px',
    width: Math.abs(m.x1 - m.x0) + 'px',
    height: Math.abs(m.y1 - m.y0) + 'px',
  }
})

// ===== 键盘快捷键 =====
function onKeyDown(e) {
  if (store.activeTab !== 'workflow') return
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
  if (t && t.closest && t.closest('.n-base-selection')) return
  const mod = e.ctrlKey || e.metaKey

  if (mod) {
    const k = (e.key || '').toLowerCase()
    if (k === 'a') {
      e.preventDefault()
      selectAll()
      return
    }
    if (k === 'c' || k === 'x') {
      // 有文本选区时让位给系统复制
      if (window.getSelection && String(window.getSelection() || '')) return
      if (!selCount.value) return
      e.preventDefault()
      if (k === 'c') copySelected()
      else cutSelected()
      return
    }
    if (k === 'v') {
      if (!clipboard.value.length) return
      e.preventDefault()
      pasteSteps(dropIndex.value != null ? dropIndex.value : null)
      return
    }
    if (k === 'z' && !e.shiftKey) {
      e.preventDefault()
      undo()
    }
    return
  }

  if (e.key === 'Escape') {
    clearSelection()
    dropIndex.value = null
    return
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selCount.value) {
    e.preventDefault()
    deleteSelected()
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeyDown)
  document.addEventListener('mousedown', onDocMouseDown)
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeyDown)
  document.removeEventListener('mousedown', onDocMouseDown)
  unbindSession()
})

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
  resetEditorState()
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
  resetEditorState()
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
    resetEditorState()
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
    <!-- ===== 顶部悬浮：工作流整体操作区（sticky 常驻，长列表下不用回到顶部） ===== -->
    <div class="wf-dock wf-dock-top">
      <div class="card wf-top-card">
        <div class="wf-top-row">
          <div class="wf-tune">
            <span class="wf-tune-title">自动调优</span>
            <n-switch :value="autoTune" size="small" @update:value="onAutoTuneUpdate" />
            <n-tooltip trigger="hover" placement="bottom-start">
              <template #trigger>
                <span class="wf-help">?</span>
              </template>
              <div class="wf-help-box">
                <b>自动调优</b>：记录每个节点的耗时与重试，按 RFC 6298 超时估计自动整定。<br />
                超时 = 平滑平均耗时 + 4 × 平均偏差，另加 25% 余量；<br />
                重试按几何分布模型取「达到 95% 累计成功率」所需的最少次数。<br />
                每个节点累计运行 ≥3 次产生超时建议、≥5 次产生重试建议。<br />
                开启后每次运行结束自动应用，也可点「一键应用调优」立即写回。
              </div>
            </n-tooltip>
            <span class="wf-sep"></span>
            <n-tooltip trigger="hover" placement="bottom">
              <template #trigger>
                <span class="wf-legend">
                  <span class="wf-legend-label">稳定度</span>
                  <span class="wf-legend-bar"></span>
                </span>
              </template>
              <div class="wf-help-box">
                稳定度 = 100 −（70% × 平均重试因子 + 30% × 耗时因子）。<br />
                重试意味着节点本身不稳定，权重高于耗时；<br />
                颜色越绿表示耗时越短、重试越少。
              </div>
            </n-tooltip>
            <n-button size="small" secondary :loading="applyingTune" @click="applyTuning">
              一键应用调优
            </n-button>
          </div>

          <div class="wf-actions">
            <n-button size="small" @click="duplicateWorkflow" :disabled="saving"
              title="复制当前工作流为副本，可在此基础上修改">复制</n-button>
            <n-button size="small" @click="newWorkflow">新建</n-button>
            <n-button size="small" @click="saveAs" :disabled="saving">另存为</n-button>
            <n-button size="small" type="primary" @click="save" :disabled="saving">保存</n-button>
            <n-button size="small" @click="reset">恢复内置</n-button>
            <n-button size="small" type="error" secondary @click="removeWorkflow">删除</n-button>
          </div>
        </div>
        <div class="we-message" :class="{ error: messageError }">{{ message }}</div>
      </div>
    </div>

    <!-- ===== 工作流元信息（不常改动，跟随内容滚动） ===== -->
    <section class="card wf-meta-card">
      <div class="we-header">
        <div class="section-title">
          工作流编排
          <n-tooltip trigger="hover" placement="bottom-start">
            <template #trigger>
              <span class="wf-help">?</span>
            </template>
            <div class="wf-help-box">
              支持多个独立工作流、注销节点、WARP 服务重启节点；勾选托盘显示后会加入托盘菜单。<br />
              建议先「复制」内置工作流再修改，避免后续升级覆盖。<br /><br />
              <b>选中</b>：单击选中 · Ctrl / ⌘ + 单击加选 · Shift + 单击连选 · 在节点间按住拖动可框选。<br />
              <b>快捷键</b>：Ctrl+A 全选 · Ctrl+C 复制 · Ctrl+X 剪切 · Ctrl+V 粘贴到指示线 · Ctrl+Z 撤销 ·
              Delete 删除 · Esc 取消选择。
            </div>
          </n-tooltip>
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
    </section>

    <!-- ===== 节点列表（紧凑单行） ===== -->
    <div ref="stepsWrap" class="wf-steps" :class="{ 'is-marquee': dragMode === 'marquee', 'is-moving': dragMode === 'move' }"
      @mousedown="onStepsMouseDown" @mousemove="onListMouseMove" @mouseenter="onListMouseEnter"
      @mouseleave="onListMouseLeave">
      <template v-if="workflow.steps.length">
        <div v-for="row in stepRows" :key="row.uid" class="wf-row"
          :class="{ 'is-selected': row.selected, 'is-off': row.step.enabled === false }"
          :style="row.rail ? { borderLeftColor: row.rail } : {}" @mousedown="onRowMouseDown($event, row.index)">
          <span class="wf-grip" title="按住拖动可调整顺序" @mousedown="onGripMouseDown($event, row.index)">⠿</span>
          <n-checkbox class="wf-en" size="small" :checked="row.step.enabled !== false"
            @update:checked="(v) => updateStep(row.index, 'enabled', v)" title="启用该节点" />
          <span class="wf-idx">{{ row.index + 1 }}</span>
          <div class="wf-title">
            <span class="wf-name" :title="row.desc">{{ row.name }}</span>
            <span v-if="row.step.continue_on_error" class="wf-skip-tag"
              title="该节点失败时跳过，继续执行后续节点（在底部操作区批量设置）">跳过</span>
          </div>

          <!-- 步进按钮关闭：按钮会挤占宽度导致数值截断；聚焦后仍可用 ↑/↓ 键步进 -->
          <div class="wf-params">
            <div class="wf-p">
              <span class="wf-p-label" title="单次执行超时（秒）">超时</span>
              <n-input-number class="wf-num" size="tiny" :show-button="false"
                :value="Number(row.step.timeout || 15)" :min="1" :max="180"
                @update:value="(v) => updateStep(row.index, 'timeout', Number(v))" />
            </div>
            <div class="wf-p">
              <span class="wf-p-label" title="失败后的重试次数">重试</span>
              <n-input-number class="wf-num wf-num-xs" size="tiny" :show-button="false"
                :value="Number(row.step.retries || 0)" :min="0" :max="5"
                @update:value="(v) => updateStep(row.index, 'retries', Number(v))" />
            </div>
            <div class="wf-p">
              <span class="wf-p-label" title="两次重试之间的间隔（秒）">间隔</span>
              <n-input-number class="wf-num" size="tiny" :show-button="false"
                :value="Number(row.step.retry_delay ?? 1)" :min="0" :max="30" :step="0.5"
                @update:value="(v) => updateStep(row.index, 'retry_delay', Number(v))" />
            </div>
          </div>

          <!-- 计时统计：稳定度 / 平均耗时 / 平均重试 / 运行次数 -->
          <div class="wf-stats">
            <template v-if="row.stats">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger>
                  <span class="wf-chip wf-chip-score" :style="row.scoreStyle">
                    <i class="wf-chip-dot" :style="{ background: row.rail }"></i>{{ row.stats.score }}
                  </span>
                </template>
                <div class="wf-help-box">
                  稳定度 {{ row.stats.score }} 分（{{ stabilityLabel(row.stats.score) }}）<br />
                  累计运行 {{ row.stats.runs }} 次 · 平均耗时 {{ row.elapsed }} · 平均重试 {{ row.retries }} 次
                  <template v-if="!autoTune">
                    <br />
                    <template v-if="row.stats.suggested_timeout != null">
                      建议超时 {{ row.stats.suggested_timeout }}s
                    </template>
                    <template v-if="row.stats.suggested_retries != null">
                      · 建议重试 {{ row.stats.suggested_retries }} 次
                    </template>
                  </template>
                </div>
              </n-tooltip>
              <span class="wf-chip" title="累计平均耗时">{{ row.elapsed }}</span>
              <span class="wf-chip" title="累计平均重试次数">↻ {{ row.retries }}</span>
              <span class="wf-chip wf-chip-dim" title="累计运行次数">{{ row.stats.runs }} 次</span>
            </template>
            <span v-else class="wf-stats-empty">
              {{ currentId ? '暂无数据' : '保存后开始统计' }}
            </span>
          </div>
        </div>

        <!-- 放置指示线：拖拽排序 / 粘贴落点 -->
        <div v-if="indicatorVisible" class="wf-drop-line" :style="{ top: dropTop + 'px' }"></div>
        <div v-if="marquee" class="wf-marquee" :style="marqueeStyle"></div>
      </template>
      <div v-else class="empty-hint">暂无节点，请从下方按钮添加</div>
    </div>

    <!-- ===== 底部居中悬浮：节点操作区（有选中才出现完整操作） ===== -->
    <div class="wf-dock wf-dock-bottom">
      <div class="wf-node-bar">
        <template v-if="!selCount">
          <n-select v-model:value="selectedStep" :options="stepOptions" filterable placeholder="选择节点类型"
            class="wf-picker" />
          <n-button size="small" secondary @click="appendStep">添加节点</n-button>
          <template v-if="clipboard.length">
            <span class="wf-sep"></span>
            <n-button size="small" quaternary @click="pasteSteps(null)">粘贴 {{ clipboard.length }} 个</n-button>
          </template>
        </template>

        <template v-else>
          <template v-if="selCount === 1">
            <n-select v-model:value="selectedStep" :options="stepOptions" filterable placeholder="选择节点类型"
              class="wf-picker" />
            <n-button size="small" secondary @click="addAt(selIndices[0])">在此节点前添加</n-button>
            <n-button size="small" secondary @click="addAt(selIndices[0] + 1)">在此节点后添加</n-button>
            <span class="wf-sep"></span>
          </template>

          <n-button size="small" quaternary :disabled="!canMoveUp" @click="moveSelected(-1)"
            title="整体上移（多选时保持组内顺序）">↑ 上移</n-button>
          <n-button size="small" quaternary :disabled="!canMoveDown" @click="moveSelected(1)"
            title="整体下移（多选时保持组内顺序）">↓ 下移</n-button>
          <n-checkbox size="small" :checked="allSkip" :indeterminate="someSkip"
            @update:checked="toggleSkipSelected" title="失败时跳过，继续执行后续节点">允许跳过</n-checkbox>
          <span class="wf-sep"></span>
          <n-button size="small" quaternary @click="copySelected" title="Ctrl+C">复制</n-button>
          <n-button size="small" quaternary @click="cutSelected" title="Ctrl+X">剪切</n-button>
          <n-button size="small" quaternary :disabled="!clipboard.length" @click="pasteSteps(dropIndex)"
            title="Ctrl+V：粘贴到指示线位置">粘贴</n-button>
          <span class="wf-sep"></span>
          <n-button size="small" quaternary type="error" @click="deleteSelected()" title="Delete">删除</n-button>
          <span class="wf-sep"></span>
          <span class="wf-sel-count">已选 {{ selCount }} 个</span>
          <n-button size="tiny" quaternary @click="clearSelection" title="Esc">取消</n-button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.workflow-view {
  padding: 12px clamp(16px, 2.2vw, 30px) 0;
  max-width: 1080px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ===== 顶部悬浮操作区 ===== */
/* sticky 依赖 .app-content 作为滚动容器；背景不透明以免滚动内容透出 */
.wf-dock {
  position: sticky;
  z-index: 20;
}

.wf-dock-top {
  top: 0;
  padding: 2px 0 4px;
  background: var(--bg-base);
  box-shadow: 0 10px 14px -12px rgba(0, 0, 0, 0.55);
}

.wf-top-card {
  padding: 8px 14px 7px;
}

.wf-top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}

.wf-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

/* ===== 自动调优（紧凑） ===== */
.wf-tune {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.wf-tune-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
}

.wf-sep {
  width: 1px;
  height: 16px;
  background: var(--border);
  flex: none;
}

/* 问号图标：详细说明按需展开，不占用常驻版面 */
.wf-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border: 1px solid var(--border-strong);
  border-radius: 50%;
  font-size: 10px;
  line-height: 1;
  color: var(--text-tertiary);
  cursor: help;
  flex: none;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.wf-help:hover {
  color: var(--text-primary);
  border-color: var(--text-secondary);
}

.wf-help-box {
  max-width: 340px;
  font-size: 12px;
  line-height: 1.75;
  color: var(--text-secondary);
}

.wf-help-box b {
  color: var(--text-primary);
}

.wf-legend {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: help;
}

.wf-legend-label {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.wf-legend-bar {
  width: 62px;
  height: 7px;
  border-radius: 4px;
  background: linear-gradient(90deg,
      hsl(145, 63%, 42%),
      hsl(38, 90%, 50%),
      hsl(4, 76%, 55%));
}

.we-message {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 5px;
  min-height: 15px;
  line-height: 1.5;
}

.we-message.error {
  color: var(--error);
}

/* ===== 元信息卡 ===== */
.wf-meta-card {
  padding: 12px 16px 14px;
}

.we-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.we-meta {
  display: grid;
  grid-template-columns: 1.25fr 1.25fr auto 130px;
  gap: 12px;
  align-items: end;
  margin-top: 12px;
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

/* ===== 节点列表 ===== */
.wf-steps {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 6px;
  /* 节点少时末行下方也保留一段可交互留白：可点按取消选中、可从这里起手框选，
     同时让粘贴指示线有更大的落点区域 */
  min-height: 220px;
}

/* 拖拽期间禁掉文本选中，否则拖选会顺带刷蓝一片文字 */
.wf-steps.is-marquee,
.wf-steps.is-moving {
  user-select: none;
}

.wf-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 4px 10px 4px 6px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: 8px;
  background: var(--bg-elevated);
  cursor: default;
  transition: background 0.12s ease, border-color 0.12s ease;
}

.wf-row:hover {
  background: var(--bg-hover);
}

.wf-row.is-selected {
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-elevated));
  border-color: var(--border-strong);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 22%, transparent);
}

.wf-row.is-off .wf-name,
.wf-row.is-off .wf-stats {
  opacity: 0.45;
}

.wf-grip {
  flex: none;
  width: 12px;
  font-size: 12px;
  line-height: 1;
  color: var(--text-tertiary);
  cursor: grab;
  user-select: none;
}

.wf-grip:hover {
  color: var(--text-primary);
}

.wf-grip:active {
  cursor: grabbing;
}

.wf-en {
  flex: none;
}

.wf-idx {
  flex: none;
  width: 18px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

/* 名称与"跳过"标签同容器：固定总宽保证参数区起始位置在所有行上对齐，
   名称超长以省略号截断，标签紧贴名称文字 */
.wf-title {
  flex: 0 1 230px;
  min-width: 120px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.wf-name {
  flex: 0 1 auto;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 行内不再放"跳过"复选框（底部操作区已有批量开关），只保留只读状态标签 */
.wf-skip-tag {
  flex: none;
  height: 16px;
  line-height: 14px;
  padding: 0 5px;
  font-size: 10px;
  color: var(--text-tertiary);
  border: 1px dashed var(--border-strong);
  border-radius: 4px;
}

.wf-params {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  justify-content: flex-start;
}

.wf-p {
  display: flex;
  align-items: center;
  gap: 4px;
}

.wf-p-label {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

/* 步进按钮已关闭，整行宽度都留给数值：≥4 位数字（含小数点）不会截断。
   强制左对齐——naive-ui 的 input-number 默认右对齐，数值变动时起始位会跳 */
.wf-num {
  width: 78px;
}

.wf-num-xs {
  width: 60px;
}

.wf-num :deep(.n-input__input-el),
.wf-num-xs :deep(.n-input__input-el) {
  text-align: left;
}

.wf-stats {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  flex: none;
}

/* ===== 统计 chips ===== */
.wf-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 19px;
  padding: 0 7px;
  font-size: 11px;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg-panel);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.wf-chip-score {
  font-weight: 600;
  cursor: help;
}

.wf-chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}

.wf-chip-dim {
  color: var(--text-tertiary);
}

.wf-stats-empty {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

/* ===== 放置指示线 / 框选矩形 ===== */
.wf-drop-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 2px;
  border-radius: 2px;
  background: var(--accent);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 30%, transparent);
  pointer-events: none;
  z-index: 5;
}

.wf-drop-line::before {
  content: '';
  position: absolute;
  left: -1px;
  top: -3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.wf-marquee {
  position: absolute;
  border: 1px solid color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-radius: 4px;
  pointer-events: none;
  z-index: 4;
}

/* ===== 底部居中悬浮：节点操作区 ===== */
.wf-dock-bottom {
  bottom: 0;
  display: flex;
  justify-content: center;
  padding: 24px 0 10px;
  /* 上缘渐隐：滚动内容从条下方淡出，视觉上更像"浮起" */
  background: linear-gradient(to top,
      var(--bg-base) 0%,
      var(--bg-base) 58%,
      color-mix(in srgb, var(--bg-base) 0%, transparent) 100%);
  pointer-events: none;
}

.wf-node-bar {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  justify-content: center;
  padding: 7px 11px;
  border: 1px solid var(--border-strong);
  border-radius: 11px;
  background: var(--bg-panel);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.32);
}

.wf-picker {
  width: 190px;
  flex: none;
}

.wf-sel-count {
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

@media (max-width: 1180px) {
  .wf-stats {
    margin-left: 0;
  }
}
</style>
