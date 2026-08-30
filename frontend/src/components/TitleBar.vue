<script setup>
import { api } from '../bridge'
import LogoMark from './LogoMark.vue'
import AppIcon from './AppIcon.vue'

function minimizeWindow() {
  try {
    api()?.minimize_window()
  } catch (e) {
    console.error('minimizeWindow failed:', e)
  }
}

function closeWindow() {
  try {
    api()?.close_window()
  } catch (e) {
    console.error('closeWindow failed:', e)
  }
}
</script>

<template>
  <div class="title-bar pywebview-drag-region">
    <div class="title-left">
      <LogoMark />
    </div>
    <div class="title-right">
      <button class="title-btn" @click="minimizeWindow" title="最小化">
        <AppIcon name="minus" :size="14" />
      </button>
      <button class="title-btn close" @click="closeWindow" title="关闭">
        <AppIcon name="x" :size="14" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.title-bar {
  height: 40px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px 0 16px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
}

.title-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.title-right {
  display: flex;
  gap: 4px;
}

.title-btn {
  width: 30px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-tertiary);
  transition: all 0.15s;
}

.title-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.title-btn.close:hover {
  background: rgba(239, 68, 68, 0.22);
  color: #ef4444;
}
</style>
