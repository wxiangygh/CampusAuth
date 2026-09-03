<script setup>
// 列表排序方向切换按钮（升序 A→Z / 降序 Z→A）。
// 用法：<SortToggle v-model:dir="someSortDir" /> 或 <SortToggle :dir="d" compact @toggle="..." />
import AppIcon from './AppIcon.vue'

const props = defineProps({
  // 1 = 升序 A→Z，-1 = 降序 Z→A
  dir: { type: Number, default: 1 },
  // 紧凑模式：只显示图标（用于列头等空间紧张的位置）
  compact: { type: Boolean, default: false },
  // 无障碍/悬浮提示里的列表名，如「进程名」「域名」
  subject: { type: String, default: '' },
})

const emit = defineEmits(['update:dir', 'toggle'])

function toggle() {
  const next = props.dir === 1 ? -1 : 1
  emit('update:dir', next)
  emit('toggle', next)
}
</script>

<template>
  <button type="button" class="sort-toggle" :class="{ compact }" :aria-label="`切换排序方向，当前${dir === 1 ? '升序' : '降序'}`"
    :title="dir === 1
      ? `按字母升序排列${subject ? '（' + subject + '）' : ''}，点击切换为降序 Z→A`
      : `按字母降序排列${subject ? '（' + subject + '）' : ''}，点击切换为升序 A→Z`" @click="toggle">
    <AppIcon :name="dir === 1 ? 'sortAsc' : 'sortDesc'" :size="compact ? 12 : 13" />
    <span v-if="!compact" class="sort-toggle-text">{{ dir === 1 ? 'A→Z' : 'Z→A' }}</span>
  </button>
</template>

<style scoped>
.sort-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  padding: 0 8px;
  height: 26px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-panel);
  color: var(--text-tertiary);
  font-size: 12px;
  font-family: inherit;
  line-height: 1;
  cursor: pointer;
  transition: color .15s, border-color .15s, background .15s;
}

.sort-toggle.compact {
  width: 22px;
  height: 22px;
  padding: 0;
  justify-content: center;
  border-color: transparent;
  background: transparent;
}

.sort-toggle:hover {
  color: var(--text-primary);
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.sort-toggle.compact:hover {
  border-color: transparent;
  background: var(--bg-hover);
}

.sort-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.sort-toggle-text {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .2px;
  white-space: nowrap;
}
</style>
