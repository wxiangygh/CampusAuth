<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { store } from '../store'
import { themeMode, setThemeMode } from '../theme'
import { api } from '../bridge'
import AppIcon from './AppIcon.vue'

const NAV_ITEMS = [
  { key: 'home', label: '主页', icon: 'home', hint: '状态与认证' },
  { key: 'workflow', label: '工作流', icon: 'workflow', hint: '节点编排' },
  { key: 'warp', label: '分流规则', icon: 'globe', hint: 'WARP 排除' },
  { key: 'traffic', label: '流量监控', icon: 'activity', hint: '活动连接' },
  { key: 'settings', label: '设置', icon: 'settings', hint: '基本配置' },
]

const THEME_MODES = [
  { key: 'light', label: '浅色', icon: 'sun' },
  { key: 'dark', label: '深色', icon: 'moon' },
  { key: 'system', label: '跟随系统', icon: 'monitor' },
]

// 滑动指示器位置：每个按钮宽 30px + 间距 2px
const themeIndex = computed(() =>
  Math.max(0, THEME_MODES.findIndex((m) => m.key === themeMode.value))
)

const indicatorInner = ref(null)

// 指示器滑动时的液态拉伸效果（参考 iOS 分段控件商业实现）：
// 外层元素做带过冲的弹性位移，内层做 scaleX 拉伸变形，每次切换重新触发
watch(themeIndex, async () => {
  await nextTick()
  const el = indicatorInner.value
  if (!el || typeof el.animate !== 'function') return
  el.animate(
    [
      { transform: 'scaleX(1)' },
      { transform: 'scaleX(1.35)', offset: 0.38 },
      { transform: 'scaleX(1)' },
    ],
    { duration: 520, easing: 'ease-out' }
  )
})

function switchTo(key) {
  store.activeTab = key
}

function onThemeChange(mode) {
  setThemeMode(mode)
  api()?.save_ui_prefs({ theme: mode })?.catch(() => {})
}
</script>

<template>
  <aside class="side-nav">
    <nav class="nav-list">
      <div v-for="item in NAV_ITEMS" :key="item.key" class="nav-item" :class="{ active: store.activeTab === item.key }"
        @click="switchTo(item.key)">
        <AppIcon :name="item.icon" :size="17" />
        <div class="nav-text">
          <div class="nav-label">{{ item.label }}</div>
          <div class="nav-hint">{{ item.hint }}</div>
        </div>
      </div>
    </nav>
    <div class="nav-footer">
      <div class="theme-toggle">
        <span class="theme-indicator" :style="{ transform: `translateX(${themeIndex * 32}px)` }">
          <span class="theme-indicator-inner" ref="indicatorInner"></span>
        </span>
        <button v-for="m in THEME_MODES" :key="m.key" class="theme-btn" :class="{ active: themeMode === m.key }"
          :title="m.label" @click="onThemeChange(m.key)">
          <AppIcon :name="m.icon" :size="14" />
        </button>
      </div>
      <div class="nav-footer-text">CAuth · Campus Network Assistant</div>
    </div>
  </aside>
</template>

<style scoped>
.side-nav {
  width: 184px;
  flex-shrink: 0;
  background: var(--bg-panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 12px 0;
}

.nav-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 10px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 12px;
  border-radius: 8px;
  color: var(--text-secondary);
  cursor: pointer;
  position: relative;
  transition: all 0.15s;
  border: 1px solid transparent;
}

.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.nav-item.active {
  background: var(--accent-dim);
  color: var(--accent);
  border-color: var(--border-strong);
}

.nav-item.active::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 20%;
  bottom: 20%;
  width: 3px;
  border-radius: 2px;
  background: var(--accent);
}

.nav-label {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.3;
}

.nav-hint {
  font-size: 10px;
  color: var(--text-tertiary);
  line-height: 1.3;
  margin-top: 1px;
}

.nav-item.active .nav-hint {
  color: var(--text-secondary);
}

.nav-footer {
  padding: 10px 14px 2px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.theme-toggle {
  position: relative;
  display: flex;
  gap: 2px;
  padding: 3px;
  background: var(--bg-elevated);
  border-radius: 8px;
}

/* 滑动指示器：外层负责弹性位移（带过冲的 back-out 曲线，
   参考商业分段控件：先冲过头再回弹，行程清晰可见），
   内层负责移动中的 scaleX 拉伸变形（由 Web Animations API 驱动） */
.theme-indicator {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 30px;
  height: 26px;
  border-radius: 6px;
  transition: transform 0.45s cubic-bezier(0.3, 1.25, 0.35, 1);
  will-change: transform;
}

.theme-indicator-inner {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: 6px;
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}

.theme-btn {
  position: relative;
  z-index: 1;
  width: 30px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-tertiary);
  transition: color 0.2s;
}

.theme-btn:hover {
  color: var(--text-primary);
}

.theme-btn.active {
  color: var(--accent);
}

/* 选中图标轻微弹出（back-out 过冲），与滑块动画呼应 */
.theme-btn :deep(svg) {
  transition: transform 0.35s cubic-bezier(0.3, 1.5, 0.5, 1);
}

.theme-btn.active :deep(svg) {
  transform: scale(1.15);
}

.nav-footer-text {
  font-size: 10px;
  color: var(--text-tertiary);
  letter-spacing: 0.3px;
  font-family: var(--font-mono);
  text-align: center;
}
</style>
