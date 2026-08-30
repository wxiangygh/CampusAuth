<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { store, closeUpdate, startUpdate } from '../store'
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

// ===== 更新弹窗（侧边栏底部，主题切换按钮上方）=====
const updateBodyRef = ref(null)

function onUpdateConfirm() {
  startUpdate()
}

function onUpdateClose() {
  // 下载/安装进行中不允许关闭，避免更新流程被打断
  if (store.update.busy) return
  closeUpdate()
}

// 新内容到达时把说明区滚动回顶部
watch(
  () => store.update.visible,
  async (visible) => {
    if (!visible) return
    await nextTick()
    if (updateBodyRef.value) updateBodyRef.value.scrollTop = 0
  }
)
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
      <!-- 更新卡片：从侧边栏底部弹出，位于主题切换按钮上方 -->
      <transition name="update-pop">
        <div v-if="store.update.visible" class="update-card">
          <button class="update-close" title="关闭" @click="onUpdateClose">
            <AppIcon name="x" :size="12" />
          </button>
          <div class="update-head">
            <span class="update-badge">更新</span>
            <span class="update-version">v{{ store.update.version }}</span>
          </div>
          <div class="update-title">{{ store.update.name || '发现新版本' }}</div>
          <div class="update-notes" ref="updateBodyRef">{{ store.update.notes }}</div>
          <div class="update-progress" v-if="store.update.busy">
            <div class="update-track">
              <div class="update-fill" :class="{ indeterminate: store.update.status === 'installing' }"
                :style="{ width: store.update.pct + '%' }"></div>
            </div>
            <div class="update-hint">{{ store.update.message }}</div>
          </div>
          <button class="update-confirm" :disabled="store.update.busy" @click="onUpdateConfirm">
            {{ store.update.busy ? '正在处理…' : '立即更新' }}
          </button>
        </div>
      </transition>
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

/* ===== 更新卡片 =====
   从侧边栏底部弹出（向上位移 + 渐显），收起时反向 */
.update-card {
  width: 100%;
  position: relative;
  padding: 10px 11px 11px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--bg-elevated);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.22);
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.update-pop-enter-active {
  transition:
    transform 0.32s cubic-bezier(0.22, 1.1, 0.36, 1),
    opacity 0.24s ease;
}

.update-pop-leave-active {
  transition:
    transform 0.22s ease,
    opacity 0.18s ease;
}

.update-pop-enter-from,
.update-pop-leave-to {
  transform: translateY(14px) scale(0.97);
  opacity: 0;
}

.update-close {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: all 0.15s;
}

.update-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.update-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-right: 22px;
}

.update-badge {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 2px 5px;
  border-radius: 4px;
  background: var(--accent);
  color: var(--bg-base);
}

.update-version {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

.update-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.35;
}

.update-notes {
  max-height: 132px;
  overflow-y: auto;
  padding-right: 2px;
  font-size: 10.5px;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.update-progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.update-track {
  height: 3px;
  border-radius: 2px;
  background: var(--border);
  overflow: hidden;
}

.update-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--accent);
  transition: width 0.3s ease;
}

.update-fill.indeterminate {
  width: 40% !important;
  animation: update-slide 1.1s ease-in-out infinite;
}

@keyframes update-slide {
  0% {
    transform: translateX(-100%);
  }

  100% {
    transform: translateX(250%);
  }
}

.update-hint {
  font-size: 10px;
  color: var(--text-tertiary);
}

.update-confirm {
  margin-top: 1px;
  height: 28px;
  border: none;
  border-radius: 7px;
  background: var(--accent);
  color: var(--bg-base);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.15s;
}

.update-confirm:hover:not(:disabled) {
  opacity: 0.88;
}

.update-confirm:disabled {
  opacity: 0.55;
  cursor: default;
}
</style>
