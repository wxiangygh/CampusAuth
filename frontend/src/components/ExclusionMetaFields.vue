<script setup>
import { computed } from 'vue'
import { NInput, NButton } from 'naive-ui'
import AppAvatar from './AppAvatar.vue'
import { ui } from '../ui'

// 排除条目的应用元数据编辑：应用名（必填由父级校验）/ 图标 / 备注
// 图标优先级：自定义 icon > 进程图标 icon_url（后端按 icon_exe 解析）> 应用名首字母
const props = defineProps({
  modelValue: { type: Object, required: true },
})
const emit = defineEmits(['update:modelValue'])

const meta = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function patch(p) {
  meta.value = { ...meta.value, ...p }
}

const previewIcon = computed(() => meta.value.icon || meta.value.icon_url || null)

function onPickFile(file) {
  const f = file.target.files && file.target.files[0]
  file.target.value = ''
  if (!f) return
  if (f.size > 200 * 1024) {
    ui.toast('图标请控制在 200KB 以内', 'error')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    // 自定义图标覆盖进程图标，避免两套图标并存产生歧义
    patch({ icon: String(reader.result), icon_exe: '', icon_url: null })
  }
  reader.readAsDataURL(f)
}

function clearIcon() {
  patch({ icon: '', icon_exe: '', icon_url: null })
}
</script>

<template>
  <div class="meta-fields">
    <div class="meta-row">
      <label class="meta-label">应用名 <i>*</i></label>
      <n-input :value="meta.app_name || ''" size="small" placeholder="如：抖音、微信、Steam"
        @update:value="(v) => patch({ app_name: v })" />
    </div>
    <div class="meta-row">
      <label class="meta-label">图标</label>
      <div class="meta-icon-line">
        <AppAvatar :name="meta.app_name || ''" :icon="previewIcon" :size="30" />
        <label class="icon-pick">
          <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="onPickFile" />
          <n-button size="tiny">上传图标</n-button>
        </label>
        <n-button v-if="meta.icon || meta.icon_exe" size="tiny" quaternary @click="clearIcon">清除</n-button>
        <span class="meta-hint">不上传则按应用名首字母显示</span>
      </div>
    </div>
    <div class="meta-row">
      <label class="meta-label">备注</label>
      <n-input :value="meta.note || ''" size="small" placeholder="选填，如：视频走 IPv6 免流"
        @update:value="(v) => patch({ note: v })" />
    </div>
  </div>
</template>

<style scoped>
.meta-fields {
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

.meta-label i {
  color: var(--error, #e05252);
  font-style: normal;
}

.meta-icon-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}

.icon-pick input {
  display: none;
}

.meta-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
