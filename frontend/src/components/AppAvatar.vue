<script setup>
// 应用头像：有图标用图标，没有就取应用名首字母占位
defineProps({
  name: { type: String, default: '' },
  icon: { type: String, default: null },
  size: { type: Number, default: 22 },
})
</script>

<template>
  <span class="app-avatar" :style="{ width: size + 'px', height: size + 'px', fontSize: Math.round(size * 0.52) + 'px' }">
    <img v-if="icon" :src="icon" alt="" />
    <span v-else>{{ (name || '?').trim().charAt(0).toUpperCase() }}</span>
  </span>
</template>

<style scoped>
.app-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border-radius: 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-weight: 700;
  overflow: hidden;
  user-select: none;
}

.app-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
</style>
