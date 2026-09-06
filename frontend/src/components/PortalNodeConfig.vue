<script setup>
import { ref, computed, watch } from 'vue'
import { NSelect, NInput, NRadioGroup, NRadioButton, NCollapse, NCollapseItem, NButton } from 'naive-ui'

// Portal 节点（认证/注销）的自定义配置编辑器。
// params 结构：{ link_mode, wireless:{...}, wired:{...} }，两套有线/无线分别保存。
const props = defineProps({
  params: { type: Object, default: () => ({}) },
  // 全局 portal_ip:portal_port，作为 HTTP 方式 server 留空时的回退提示
  defaultServer: { type: String, default: '' },
  // 注销节点的文案略有不同
  kind: { type: String, default: 'login' },
})
const emit = defineEmits(['update:params'])

const isLogout = computed(() => props.kind === 'logout')
const actionWord = computed(() => (isLogout.value ? '注销' : '登录'))

// 当前编辑哪一套（无线/有线）；初值跟随 link_mode 的手动指定，否则无线
const editLink = ref(props.params?.link_mode === 'wired' ? 'wired' : 'wireless')
watch(
  () => props.params?.link_mode,
  (mode) => {
    if (mode === 'wired' || mode === 'wireless') editLink.value = mode
  }
)

const methodOptions = [
  { label: '网页点击（推荐）', value: 'web' },
  { label: '直连 HTTP（dr1003 等）', value: 'http' },
]
const linkModeOptions = [
  { label: '自动检测（优先无线）', value: 'auto' },
  { label: '指定有线', value: 'wired' },
  { label: '指定无线', value: 'wireless' },
]
const matchOptions = [
  { label: '全字匹配', value: 'exact' },
  { label: '包含匹配', value: 'contains' },
]

function variantOf(link) {
  const v = props.params?.[link]
  return v && typeof v === 'object' ? v : {}
}

const cur = computed(() => variantOf(editLink.value))
const method = computed(() => cur.value.method || 'http')
const linkMode = computed(() => props.params?.link_mode || 'auto')

// —— 多步点击列表 ——
// click_steps: [{ name, match: 'exact'|'contains' }]；旧配置只有 button_name 时
// 以它为首步种子（保存时写入 click_steps，旧字段保留作回退）。
const MAX_STEPS = 8
function stepsOf(variant) {
  const raw = variant?.click_steps
  if (Array.isArray(raw) && raw.length) {
    return raw.map((s) => ({
      name: String(s?.name ?? ''),
      match: s?.match === 'exact' ? 'exact' : 'contains',
    }))
  }
  const legacy = String(variant?.button_name || '').trim()
  return legacy ? [{ name: legacy, match: 'contains' }] : []
}
const steps = computed(() => stepsOf(cur.value))

function emitSteps(rows) {
  // 空名行保留（正在输入的新行）；后端执行时 normalize_click_steps 会剔除空行
  const cleaned = rows
    .map((s) => ({ name: String(s.name || '').trim(), match: s.match === 'exact' ? 'exact' : 'contains' }))
    .slice(0, MAX_STEPS)
  setField('click_steps', cleaned)
}
function addStep() {
  if (steps.value.length >= MAX_STEPS) return
  emitSteps([...steps.value, { name: '', match: 'contains' }])
}
function removeStep(idx) {
  emitSteps(steps.value.filter((_, i) => i !== idx))
}
function setStepName(idx, name) {
  emitSteps(steps.value.map((s, i) => (i === idx ? { ...s, name } : s)))
}
function setStepMatch(idx, match) {
  emitSteps(steps.value.map((s, i) => (i === idx ? { ...s, match } : s)))
}

function emitUpdate(patch) {
  const next = {
    link_mode: props.params?.link_mode || 'auto',
    wireless: { ...variantOf('wireless') },
    wired: { ...variantOf('wired') },
    ...patch,
  }
  emit('update:params', next)
}

function setField(key, value) {
  emitUpdate({ [editLink.value]: { ...variantOf(editLink.value), [key]: value } })
}
function setMethod(v) {
  setField('method', v)
}
function setLinkMode(v) {
  emitUpdate({ link_mode: v })
}

const serverPlaceholder = computed(() =>
  props.defaultServer ? `留空则用全局：${props.defaultServer}` : 'IP 或域名，端口可选，如 10.21.221.98:801 或 portal.example.com'
)
</script>

<template>
  <div class="portal-cfg">
    <div class="pcfg-row">
      <label class="pcfg-label">运行时连接</label>
      <n-select size="small" :value="linkMode" :options="linkModeOptions" @update:value="setLinkMode" />
          <span class="pcfg-hint">自动时按当前联网方式选用下面对应的一套配置；对应一套未配置（如无线还空着）时自动回退另一套</span>
    </div>

    <div class="pcfg-row">
      <label class="pcfg-label">编辑配置</label>
      <n-radio-group v-model:value="editLink" size="small">
        <n-radio-button value="wireless">无线</n-radio-button>
        <n-radio-button value="wired">有线</n-radio-button>
      </n-radio-group>
      <span class="pcfg-hint">有线与无线分别保存，互不影响</span>
    </div>

    <div class="pcfg-divider"></div>

    <div class="pcfg-row">
      <label class="pcfg-label">认证方式</label>
      <n-select size="small" :value="method" :options="methodOptions" @update:value="setMethod" />
    </div>

    <template v-if="method === 'web'">
      <div class="pcfg-row">
        <label class="pcfg-label">认证网址</label>
        <n-input size="small" :value="cur.auth_url || ''" type="text"
          :placeholder="`需要输入账号密码的完整${actionWord}页网址，由你提供，系统不拼接`"
          @update:value="(v) => setField('auth_url', v)" />
      </div>
      <div class="pcfg-steps">
        <div class="pcfg-steps-head">
          <span class="pcfg-label">{{ actionWord }}点击步骤</span>
          <span class="pcfg-hint-inline">按顺序点击，每步之间等待页面变化再推进；点「＋」加一步</span>
        </div>
        <div v-for="(step, idx) in steps" :key="idx" class="pcfg-step-row">
          <span class="pcfg-step-no">{{ idx + 1 }}</span>
          <n-input size="small" :value="step.name" type="text"
            :placeholder="`第 ${idx + 1} 步按钮文字，如「${idx === 0 ? (isLogout ? '注销' : '登录') : '确定'}」`"
            @update:value="(v) => setStepName(idx, v)" />
          <n-select size="small" class="pcfg-step-match" :value="step.match"
            :options="matchOptions" @update:value="(v) => setStepMatch(idx, v)" />
          <n-button size="tiny" quaternary class="pcfg-step-del"
            :disabled="steps.length <= 1" @click="removeStep(idx)">删除</n-button>
        </div>
        <n-button size="small" dashed class="pcfg-step-add" :disabled="steps.length >= 8"
          @click="addStep">＋ 添加一步</n-button>
        <span class="pcfg-hint">全字匹配=按钮文字完全一致；包含匹配=文字包含即命中。建议把该节点超时调大（如 20s）；
        若页面用浏览器原生弹窗确认（非网页内按钮），暂不支持自动点击</span>
      </div>

      <n-collapse class="pcfg-adv">
        <n-collapse-item title="高级选项（可选，一般无需填写）" name="adv">
          <div class="pcfg-row">
            <label class="pcfg-label">账号框选择器</label>
            <n-input size="small" :value="cur.user_selector || ''" type="text" placeholder="留空自动定位，如 #username"
              @update:value="(v) => setField('user_selector', v)" />
          </div>
          <div class="pcfg-row">
            <label class="pcfg-label">密码框选择器</label>
            <n-input size="small" :value="cur.pass_selector || ''" type="text" placeholder="留空自动定位，如 #password"
              @update:value="(v) => setField('pass_selector', v)" />
          </div>
          <div class="pcfg-row">
            <label class="pcfg-label">成功关键词</label>
            <n-input size="small" :value="cur.success_keyword || ''" type="text" placeholder="留空用内置判定，如 认证成功"
              @update:value="(v) => setField('success_keyword', v)" />
          </div>
          <div class="pcfg-row">
            <label class="pcfg-label">失败关键词</label>
            <n-input size="small" :value="cur.fail_keyword || ''" type="text" placeholder="留空用内置判定，如 密码错误"
              @update:value="(v) => setField('fail_keyword', v)" />
          </div>
        </n-collapse-item>
      </n-collapse>
    </template>

    <template v-else>
      <div class="pcfg-row">
        <label class="pcfg-label">认证服务器</label>
        <n-input size="small" :value="cur.server || ''" type="text" :placeholder="serverPlaceholder"
          @update:value="(v) => setField('server', v)" />
        <span class="pcfg-hint">IP 或域名均可，端口可选（如 10.21.221.98:801 或 portal.example.com）；留空回退到全局服务器</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.portal-cfg {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pcfg-row {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  gap: 6px 10px;
  align-items: center;
}

.pcfg-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  white-space: nowrap;
}

.pcfg-hint {
  grid-column: 2;
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.5;
}

.pcfg-divider {
  height: 1px;
  background: var(--border);
  margin: 2px 0;
}

.pcfg-adv {
  margin-top: 2px;
}

/* —— 多步点击列表 —— */
.pcfg-steps {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcfg-steps-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.pcfg-hint-inline {
  font-size: 11px;
  color: var(--text-tertiary);
}

.pcfg-step-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) 96px auto;
  gap: 6px;
  align-items: center;
}

.pcfg-step-no {
  font-size: 11px;
  color: var(--text-tertiary);
  text-align: center;
}

.pcfg-step-match {
  width: 96px;
}

.pcfg-step-del {
  color: var(--text-tertiary);
}

.pcfg-step-add {
  align-self: flex-start;
}
</style>
