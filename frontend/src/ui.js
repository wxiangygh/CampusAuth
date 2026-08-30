import { h, reactive } from 'vue'
import { createDiscreteApi } from 'naive-ui'
import { naiveTheme, themeOverrides } from './theme'

// 独立挂载的 message / dialog，可在任何模块（含非组件上下文）中调用
// configProviderProps 传入响应式对象，随主题切换自动更新
const { message, dialog } = createDiscreteApi(['message', 'dialog'], {
  configProviderProps: {
    theme: naiveTheme,
    themeOverrides,
  },
})

// 自定义全屏操作遮罩（替代原 loadingMask）
const loadingState = reactive({ active: false, text: '处理中...' })

export const ui = {
  message,
  dialog,

  toast(msg, type = 'info') {
    const fn = message[type] || message.info
    fn(String(msg ?? ''))
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
