<script setup>
import { computed, reactive, watch } from 'vue'
import { NModal, NInput, NSelect, NButton } from 'naive-ui'
import ExclusionMetaFields from './ExclusionMetaFields.vue'
import { ui } from '../ui'

// 排除规则添加/编辑元数据的通用弹窗：目标 + 路由 + 应用名/图标/备注
// 应用名必填；图标缺省按应用名首字母；从流量页进入时由调用方预填应用名与图标
const props = defineProps({
  show: { type: Boolean, default: false },
  kind: { type: String, default: 'domain' }, // domain | ip | dns
  mode: { type: String, default: 'add' },     // add | meta（meta = 只改应用信息）
  target: { type: String, default: '' },
  targets: { type: Array, default: () => [] }, // 批量添加（学习模式多选）
  route: { type: String, default: 'ipv6' },
  meta: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:show', 'submit'])

const ROUTE_OPTIONS = [
  { label: '走 IPv6 校园网（免流）', value: 'ipv6' },
  { label: '走 IPv4 校园网', value: 'ipv4' },
]

const state = reactive({
  target: '',
  route: 'ipv6',
  meta: { app_name: '', icon: '', icon_exe: '', icon_url: null, note: '' },
})

const KIND_TITLE = { domain: '域名排除', ip: 'IP 排除', dns: 'DNS Fallback' }

const isBulk = computed(() => props.mode === 'add' && props.targets.length > 1)

watch(
  () => props.show,
  (v) => {
    if (!v) return
    state.target = props.target || (props.targets.length === 1 ? props.targets[0] : '')
    state.route = props.route || (props.kind === 'ip' ? 'ipv4' : 'ipv6')
    state.meta = {
      app_name: props.meta.app_name || '',
      icon: props.meta.icon || '',
      icon_exe: props.meta.icon_exe || '',
      icon_url: props.meta.icon_url || null,
      note: props.meta.note || '',
    }
  }
)

function close() {
  emit('update:show', false)
}

function submit() {
  if (props.mode === 'add' && !isBulk.value && !state.target.trim()) {
    ui.toast('请填写要排除的目标', 'error')
    return
  }
  if (!state.meta.app_name || !state.meta.app_name.trim()) {
    ui.toast('应用名必填，用于分组展示', 'error')
    return
  }
  emit('submit', {
    kind: props.kind,
    mode: props.mode,
    target: state.target.trim(),
    targets: isBulk.value ? props.targets.slice() : [state.target.trim()],
    route: state.route,
    app_name: state.meta.app_name.trim(),
    icon: state.meta.icon || '',
    icon_exe: state.meta.icon_exe || '',
    note: state.meta.note || '',
  })
  close()
}
</script>

<template>
  <n-modal :show="show" preset="card" :title="`${mode === 'meta' ? '编辑应用信息' : '添加'} · ${KIND_TITLE[kind]}`"
    style="width: min(460px, 92vw)" :bordered="false" @update:show="(v) => emit('update:show', v)">
    <div class="form-stack">
      <div v-if="mode === 'add' && isBulk" class="meta-row">
        <label class="meta-label">目标</label>
        <span class="bulk-hint">批量添加 {{ targets.length }} 个目标，共用下面的应用名与备注</span>
      </div>
      <div v-else-if="mode === 'add'" class="meta-row">
        <label class="meta-label">目标</label>
        <n-input v-model:value="state.target" size="small"
          :placeholder="kind === 'ip' ? 'IP 或 CIDR，如 2402:4e00::/32' : '域名，如 www.douyin.com'" />
      </div>
      <div v-if="mode === 'add' && kind !== 'dns'" class="meta-row">
        <label class="meta-label">路由</label>
        <n-select v-model:value="state.route" size="small" :options="ROUTE_OPTIONS" />
      </div>
      <ExclusionMetaFields v-model="state.meta" />
    </div>
    <template #footer>
      <div class="form-footer">
        <n-button size="small" @click="close">取消</n-button>
        <n-button size="small" type="primary" @click="submit">确定</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.form-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.meta-label {
  width: 62px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.form-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.bulk-hint {
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
