<script setup>
import { computed } from 'vue'
import { store } from '../store'

const STATUS_META = {
  idle: { color: '#6B6B72', label: '待机' },
  running: { color: '#F68320', label: '运行中' },
  success: { color: '#22C55E', label: '已连接' },
  error: { color: '#EF4444', label: '异常' },
  normal: { color: '#22C55E', label: '正常' },
}

const statusMeta = computed(() => STATUS_META[store.status.state] || STATUS_META.idle)

const detail = computed(() => store.detail || {})
const ipv4Text = computed(() => detail.value.ipv4 || '—')
const ipv6Text = computed(() => (detail.value.ipv6 ? '可用' : '无'))
const warpText = computed(() => (detail.value.warp_connected ? '已连接' : '未连接'))
const wifiText = computed(() => detail.value.wifi_ssid || '未连接')
</script>

<template>
  <footer class="status-bar">
    <div class="sb-left">
      <span class="sb-dot" :style="{ background: statusMeta.color }"></span>
      <span class="sb-status">{{ store.status.title }}</span>
    </div>
    <div class="sb-right">
      <span class="sb-item">WiFi <span class="sb-val">{{ wifiText }}</span></span>
      <span class="sb-sep"></span>
      <span class="sb-item">IPv4 <span class="sb-val mono">{{ ipv4Text }}</span></span>
      <span class="sb-sep"></span>
      <span class="sb-item">IPv6 <span class="sb-val">{{ ipv6Text }}</span></span>
      <span class="sb-sep"></span>
      <span class="sb-item">WARP <span class="sb-val">{{ warpText }}</span></span>
    </div>
  </footer>
</template>

<style scoped>
.status-bar {
  height: 26px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  background: var(--bg-panel);
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-tertiary);
  user-select: none;
}

.sb-left {
  display: flex;
  align-items: center;
  gap: 7px;
}

.sb-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}

.sb-status {
  color: var(--text-secondary);
}

.sb-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sb-item {
  white-space: nowrap;
}

.sb-val {
  color: var(--text-secondary);
}

.sb-sep {
  width: 1px;
  height: 10px;
  background: var(--border-strong);
}
</style>
