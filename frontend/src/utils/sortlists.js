// 列表字母排序工具（全应用统一约定）。
//
// 域名排序规则：
//   *.example.com 与 example.com 视为同一个"分组"，分组内部裸域名永远排在前。
//   升序：[example.com, *.example.com, other.com]
//   降序：只翻转分组（按 base domain）之间的相对位置，组内顺序不变：
//         [other.com, example.com, *.example.com]
//   示例：[*.a.com, b.com, a.com] 升序 → [a.com, *.a.com, b.com]
//
// 关键点：排序方向（dir）只作用于"分组之间"，不作用于"分组内部"。
// 所以 dir 必须透传给比较函数，由比较函数自行决定哪一部分会被翻转——
// 若在外层把整个比较结果取反，降序时 *.a.com 会跑到 a.com 前面，破坏分组约定。
//
// 其他列表（进程名、SSID、IP/CIDR、域名等）使用通用 compareText 做纯字母序比较。

// dir: 1 升序（A→Z），-1 降序（Z→A）
export function compareText(a, b, dir = 1) {
  const c = String(a || '').toLowerCase().localeCompare(String(b || '').toLowerCase())
  return dir < 0 ? -c : c
}

// 域名排序键：base（去掉 *. 前缀并小写）+ 是否通配符
function domainKey(d) {
  const s = String(d || '')
  const wildcard = s.startsWith('*.')
  return { base: (wildcard ? s.slice(2) : s).toLowerCase(), wildcard }
}

export function compareDomain(a, b, dir = 1) {
  const ka = domainKey(a)
  const kb = domainKey(b)
  // 分组之间：受排序方向影响
  const c = ka.base.localeCompare(kb.base)
  if (c) return dir < 0 ? -c : c
  // 分组内部：裸域名在前，恒定不变
  if (ka.wildcard === kb.wildcard) return 0
  return ka.wildcard ? 1 : -1
}

// 通用排序：keyFn 从元素取出排序键字符串，cmp 负责比较。
// dir 透传给 cmp，保证"分组内部顺序"这类固定规则不被方向翻转。
export function sortBy(arr, keyFn, dir = 1, cmp = compareText) {
  const sign = dir < 0 ? -1 : 1
  return [...arr].sort((x, y) => cmp(keyFn(x), keyFn(y), sign))
}
