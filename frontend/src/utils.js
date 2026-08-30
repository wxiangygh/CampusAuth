// 共享工具函数

// 子序列保序匹配：query 的字符按顺序出现在 text 中即可
export function fuzzyMatch(text, query) {
  if (!query) return true
  text = String(text || '').toLowerCase()
  query = String(query).toLowerCase()
  let qi = 0
  for (let ti = 0; ti < text.length && qi < query.length; ti++) {
    if (text[ti] === query[qi]) qi++
  }
  return qi === query.length
}

export function debounce(fn, delay) {
  let timer = null
  return function (...args) {
    clearTimeout(timer)
    timer = setTimeout(() => fn.apply(this, args), delay)
  }
}

export function paginate(items, page, pageSize) {
  const start = (page - 1) * pageSize
  return items.slice(start, start + pageSize)
}
