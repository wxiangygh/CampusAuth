import { ref, computed, nextTick } from 'vue'
import { darkTheme } from 'naive-ui'

// ===== 主题模式：light / dark / system =====
export const themeMode = ref(localStorage.getItem('cauth-theme') || 'system')

const systemDark = ref(false)
try {
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = mq.matches
  mq.addEventListener('change', (e) => {
    // 仅 system 模式下系统主题变化才会引起界面切换，才需要过渡动画
    if (themeMode.value !== 'system') {
      systemDark.value = e.matches
      return
    }
    applyThemeChange(() => {
      systemDark.value = e.matches
    })
  })
} catch (e) {
  systemDark.value = false
}

export const isDark = computed(() => {
  if (themeMode.value === 'dark') return true
  if (themeMode.value === 'light') return false
  return systemDark.value
})

export const naiveTheme = computed(() => (isDark.value ? darkTheme : null))

// 黑白单色主题：强调色 = 深色模式下的近白 / 浅色模式下的近黑
function makeOverrides(dark) {
  const common = dark
    ? {
        primaryColor: '#E8E8EA',
        primaryColorHover: '#FFFFFF',
        primaryColorPressed: '#C9C9CE',
        primaryColorSuppl: '#FFFFFF',
        borderRadius: '6px',
        borderRadiusSmall: '4px',
        bodyColor: '#0D0D0D',
        cardColor: '#131316',
        modalColor: '#1A1A1E',
        popoverColor: '#1E1E22',
        inputColor: '#1A1A1E',
        tableColor: '#131316',
        actionColor: '#1A1A1E',
        hoverColor: 'rgba(255,255,255,0.06)',
        borderColor: 'rgba(255,255,255,0.09)',
        dividerColor: 'rgba(255,255,255,0.06)',
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        fontSizeMedium: '13px',
      }
    : {
        primaryColor: '#111113',
        primaryColorHover: '#2A2A2E',
        primaryColorPressed: '#000000',
        primaryColorSuppl: '#2A2A2E',
        borderRadius: '6px',
        borderRadiusSmall: '4px',
        bodyColor: '#F7F7F8',
        cardColor: '#FFFFFF',
        modalColor: '#FFFFFF',
        popoverColor: '#FFFFFF',
        inputColor: '#FFFFFF',
        tableColor: '#FFFFFF',
        actionColor: '#F1F1F3',
        hoverColor: 'rgba(0,0,0,0.045)',
        borderColor: 'rgba(0,0,0,0.13)',
        dividerColor: 'rgba(0,0,0,0.08)',
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        fontSizeMedium: '13px',
      }

  const overrides = { common }
  if (dark) {
    // 深色模式下主按钮为白底，需要黑色文字保持对比
    overrides.Button = {
      textColorPrimary: '#0D0D0D',
      textColorHoverPrimary: '#0D0D0D',
      textColorPressedPrimary: '#0D0D0D',
      textColorFocusPrimary: '#0D0D0D',
    }
  }
  return overrides
}

export const themeOverrides = computed(() => makeOverrides(isDark.value))

// ===== 画布/粒子使用的动态强调色 =====
export function accentHex() {
  return isDark.value ? '#E8E8EA' : '#1A1A1C'
}

export function accentRGBA(alpha) {
  return isDark.value ? `rgba(255,255,255,${alpha})` : `rgba(17,17,19,${alpha})`
}

// 主题应用到 UI 的方式：App.vue 根元素上的响应式 :data-theme 绑定
// （见 App.vue 模板），不依赖 <html> 属性，避免外部脚本覆写。

// ===== 主题切换动画 =====
// 优先使用 View Transitions API（WebView2/Chromium 支持）：浏览器对切换
// 前后的界面各截一张图，由 GPU 合成交叉淡入，界面本身瞬时完成重排，
// 避免旧方案（对全部元素施加 0.45s transition）在大 DOM 下造成的
// 样式重算卡顿。回调返回 nextTick()，保证新主题的 DOM 更新完成后再截图。
// 不支持 View Transitions 的环境直接切换（瞬时，无动画但不卡顿）。
function applyThemeChange(update) {
  if (typeof document !== 'undefined' && document.startViewTransition) {
    document.startViewTransition(() => {
      update()
      return nextTick()
    })
  } else {
    update()
  }
}

export function setThemeMode(mode) {
  if (!['light', 'dark', 'system'].includes(mode)) return
  applyThemeChange(() => {
    themeMode.value = mode
    localStorage.setItem('cauth-theme', mode)
  })
}
