// pywebview JS Bridge 契约层
// Python 侧通过 evaluate_js 调用 onAuthProgress / onAppState / switchTab，
// 前端通过 window.pywebview.api.* 调用 ApiBridge 方法。

export function waitForApi(timeout = 15000) {
  return new Promise((resolve, reject) => {
    if (window.pywebview && window.pywebview.api) {
      resolve(true)
      return
    }
    const start = Date.now()
    const check = setInterval(() => {
      if (window.pywebview && window.pywebview.api) {
        clearInterval(check)
        resolve(true)
      } else if (Date.now() - start > timeout) {
        clearInterval(check)
        reject(new Error('pywebview API 等待超时'))
      }
    }, 100)
  })
}

export const api = () => (window.pywebview && window.pywebview.api) || null

export function apiReady() {
  return !!(window.pywebview && window.pywebview.api)
}
