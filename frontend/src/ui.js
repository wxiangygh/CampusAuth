import { h, reactive, computed } from 'vue'
import { createDiscreteApi, NButton } from 'naive-ui'
import { naiveTheme, themeOverrides } from './theme'

// 独立挂载的 message / dialog，可在任何模块（含非组件上下文）中调用
// configProviderProps 必须传 computed：直接传 { theme: naiveTheme } 会让
// naive-ui 拿到 Ref 对象本身而非取值，主题永远不生效（弹窗一直白底）。
const configProviderProps = computed(() => ({
  theme: naiveTheme.value,
  themeOverrides: themeOverrides.value,
}))

// 消息提示定位到「内容展示区」上方居中（排除侧栏/标题栏/状态栏），
// 而不是整个应用窗口顶部：容器固定在侧栏(184px)右侧、标题栏(40px)下方。
const CONTENT_NAV_WIDTH = 184
const CONTENT_TITLE_BAR_HEIGHT = 40

const messageProviderProps = {
  placement: 'top',
  containerStyle: {
    position: 'fixed',
    top: `${CONTENT_TITLE_BAR_HEIGHT + 6}px`,
    left: `${CONTENT_NAV_WIDTH}px`,
    right: '0px',
  },
}

const { message, dialog } = createDiscreteApi(['message', 'dialog'], {
  configProviderProps,
  messageProviderProps,
})

// 自定义全屏操作遮罩（替代原 loadingMask）
const loadingState = reactive({ active: false, text: '处理中...' })

export const ui = {
  message,
  dialog,

  toast(msg, type = 'info', action = null) {
    const fn = message[type] || message.info
    // action: { label, onClick } — 给错误/风险提示一条可见的修复动作，
    // 错误类驻留更久（8s），常规 3s 自动消失
    if (action && action.label) {
      fn({
        content: String(msg ?? ''),
        duration: type === 'error' ? 8000 : 3000,
        closable: true,
        action: () =>
          h(NButton, {
            size: 'tiny',
            quaternary: true,
            type: type === 'error' ? 'error' : 'primary',
            onClick: () => action.onClick?.(),
          }, { default: () => String(action.label) }),
      })
    } else {
      fn(String(msg ?? ''))
    }
  },

  showLoading(text) {
    loadingState.active = true
    loadingState.text = text || '处理中...'
  },

  hideLoading() {
    loadingState.active = false
  },

  loadingState,

  confirm(content, title = '确认操作') {
    return new Promise((resolve) => {
      dialog.warning({
        title,
        content,
        positiveText: '确定',
        negativeText: '取消',
        onPositiveClick: () => resolve(true),
        onNegativeClick: () => resolve(false),
        onClose: () => resolve(false),
        onMaskClick: () => resolve(false),
      })
    })
  },

  alert(content, title = '提示', type = 'info') {
    const show = type === 'error' ? dialog.error : type === 'success' ? dialog.success : dialog.info
    show({
      title,
      content: () =>
        h(
          'div',
          { style: 'white-space:pre-line;font-size:13px;line-height:1.7;max-width:420px' },
          String(content)
        ),
      positiveText: '确定',
    })
  },
}
