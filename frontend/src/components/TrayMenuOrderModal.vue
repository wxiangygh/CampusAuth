<script setup>
// 「托盘工作流顺序」设置弹窗：类托盘菜单的预览。
// 只有工作流条目可以按住拖动调整顺序（原生托盘菜单不支持拖动），
// 其余条目仅为位置示意，不可调整。拖动松手后立即提交新顺序。
import { ref, computed, watch } from 'vue'
import { NButton, NModal } from 'naive-ui'
import { api } from '../bridge'
import { ui } from '../ui'

const props = defineProps({
  show: { type: Boolean, default: false },
})
const emit = defineEmits(['update:show'])

const loading = ref(false)
const saving = ref(false)
// 后端返回的托盘菜单数据：{ workflows: [{id,name,tray_order}], startup_enabled, is_admin }
const data = ref({ workflows: [], startup_enabled: false, is_admin: true })

const dropIndex = ref(null)
let drag = null // { index, startY, moved }
let rowHeight = 34

async function load() {
  loading.value = true
  try {
    const payload = await api()?.get_tray_menu_data?.()
    if (payload && Array.isArray(payload.workflows)) data.value = payload
  } catch (e) {
    console.warn('get_tray_menu_data failed:', e)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      dropIndex.value = null
      drag = null
      load()
    }
  }
)

function close() {
  emit('update:show', false)
}

// ===== 拖动排序（与工作流节点列表同一套按下→移动→松手提交语义）=====
function measureRowHeight() {
  const row = document.querySelector('.tm-modal .tm-wf-row')
  if (row) rowHeight = row.offsetHeight || 34
}

function computeDropIndex(clientY) {
  const list = document.querySelector('.tm-modal .tm-wf-list')
  if (!list) return data.value.workflows.length
  const rect = list.getBoundingClientRect()
  const offset = clientY - rect.top
  const count = data.value.workflows.length
  // 行的上半段落该行之前、下半段落该行之后
  return Math.max(0, Math.min(count, Math.floor(offset / (rowHeight + 6) + 0.5)))
}

function onRowPointerDown(e, index) {
  if (e.button !== 0) return
  e.preventDefault()
  measureRowHeight()
  drag = { index, startY: e.clientY, moved: false }
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', onDragUp)
}

function onDragMove(e) {
  if (!drag) return
  if (!drag.moved && Math.abs(e.clientY - drag.startY) < 5) return
  drag.moved = true
  dropIndex.value = computeDropIndex(e.clientY)
}

async function onDragUp(e) {
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', onDragUp)
  const session = drag
  drag = null
  dropIndex.value = null
  if (!session || !session.moved) return
  const target = computeDropIndex(e.clientY)
  const ids = data.value.workflows.map((w) => w.id)
  const [moved] = ids.splice(session.index, 1)
  const adjusted = target > session.index ? target - 1 : target
  ids.splice(adjusted, 0, moved)
  if (ids.join('|') === data.value.workflows.map((w) => w.id).join('|')) return
  // 本地先行更新（序号随即重排），提交失败时回读配置
  const byId = Object.fromEntries(data.value.workflows.map((w) => [w.id, w]))
  data.value.workflows = ids.map((id, i) => ({ ...byId[id], tray_order: i + 1 }))
  saving.value = true
  try {
    const result = await api()?.set_tray_workflow_order?.(ids)
    if (result && result.success === false) {
      ui.toast(result.message || '保存顺序失败', 'error')
      await load()
    } else if (result && result.menu_data) {
      data.value = result.menu_data
    }
    ui.toast('托盘顺序已保存', 'success')
  } catch (err) {
    ui.toast('保存顺序失败：' + err.message, 'error')
    await load()
  } finally {
    saving.value = false
  }
}

const dropLineTop = computed(() => {
  if (dropIndex.value == null) return 0
  return dropIndex.value * (rowHeight + 6) - 3
})
</script>

<template>
  <n-modal :show="show" preset="card" title="托盘菜单工作流顺序" style="width: 400px; max-width: 92vw"
    :bordered="false" transform-origin="center" @update:show="(v) => emit('update:show', v)">
    <div class="tm-modal">
      <div class="tm-tip">
        原生托盘菜单按下方顺序加序号显示工作流。按住工作流条目上下拖动即可调整，
        其余条目仅作位置示意，不可调整。
      </div>

      <div class="tm-panel" :class="{ loading }">
        <!-- 静态条目：位置示意，不可调整 -->
        <div class="tm-item static">
          <span class="tm-label">显示主窗口</span>
        </div>
        <div class="tm-sep"></div>

        <div class="tm-section-title">工作流（按住拖动排序）</div>
        <div class="tm-wf-list">
          <div v-for="(wf, index) in data.workflows" :key="wf.id" class="tm-item tm-wf-row"
            @pointerdown="onRowPointerDown($event, index)">
            <span class="tm-order">{{ index + 1 }}</span>
            <span class="tm-label">{{ wf.name }}</span>
            <span class="tm-grip">⠿</span>
          </div>
          <div v-if="!data.workflows.length" class="tm-empty">
            暂无显示在托盘的工作流：可在「工作流」页勾选「托盘菜单」
          </div>
          <div v-if="dropIndex != null" class="tm-drop-line" :style="{ top: dropLineTop + 'px' }"></div>
        </div>

        <div class="tm-sep"></div>
        <div class="tm-item static"><span class="tm-label">恢复正常模式</span></div>
        <div class="tm-item static"><span class="tm-label">WARP 排除</span></div>
        <div class="tm-item static"><span class="tm-label">流量</span></div>
        <div class="tm-item static"><span class="tm-label">打开主页</span></div>
        <div class="tm-sep"></div>
        <div v-if="!data.is_admin" class="tm-item static"><span class="tm-label">以管理员身份运行</span></div>
        <div class="tm-item static">
          <span class="tm-label">设置/取消开机自启</span>
        </div>
        <div class="tm-item static"><span class="tm-label">查看日志</span></div>
        <div class="tm-item static danger"><span class="tm-label">退出</span></div>
      </div>

      <div class="tm-footer">
        <span class="tm-foot-hint">{{ saving ? '正在保存顺序…' : '松手即自动保存' }}</span>
        <n-button size="small" @click="close">关闭</n-button>
      </div>
    </div>
  </n-modal>
</template>

<style scoped>
.tm-modal {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.tm-tip {
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
}

/* 类托盘菜单面板：与托盘菜单同观感 */
.tm-panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  padding: 6px;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.25);
  opacity: 1;
  transition: opacity 0.15s ease;
}

.tm-panel.loading {
  opacity: 0.55;
  pointer-events: none;
}

.tm-item {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 10px;
  border-radius: 7px;
  font-size: 12.5px;
  color: var(--text-primary);
}

.tm-item.static {
  color: var(--text-tertiary);
}

.tm-item.static .tm-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tm-item.static.danger {
  color: var(--error);
}

.tm-label {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tm-section-title {
  padding: 2px 10px 4px;
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--text-tertiary);
}

.tm-wf-list {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tm-wf-row {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  cursor: grab;
  height: 34px;
  user-select: none;
}

.tm-wf-row:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.tm-wf-row:active {
  cursor: grabbing;
}

.tm-order {
  flex: none;
  width: 16px;
  height: 16px;
  line-height: 15px;
  border-radius: 5px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--text-tertiary);
  font-size: 10px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.tm-grip {
  flex: none;
  color: var(--text-tertiary);
  font-size: 12px;
}

.tm-empty {
  padding: 8px 10px;
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.tm-drop-line {
  position: absolute;
  left: 4px;
  right: 4px;
  height: 2px;
  border-radius: 2px;
  background: var(--text-secondary);
  pointer-events: none;
}

.tm-sep {
  height: 1px;
  background: var(--border);
  margin: 5px 8px;
}

.tm-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.tm-foot-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
