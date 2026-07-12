# 页面合并与分页搜索优化设计

> **创建日期**: 2026-07-12
> **状态**: 已批准
> **分支**: refactor/core-module-extraction（基于 main）

## 1. 背景与目标

### 1.1 现状

项目当前有4个独立的 HTML 页面，各自通过 pywebview 创建独立窗口：

| 页面 | 尺寸 | 功能 |
|------|------|------|
| settings.html | 400×560 | 主窗口，含"连接/设置"2个tab |
| warp_exclusion.html | 520×700 | WARP排除管理（学习域名列表、规则列表、流量推荐） |
| traffic_monitor.html | 640×760 | 流量监控（6类统计卡片 + 连接列表） |
| traffic_flow.html | 960×820 | 流量可视化（粒子画布动画 + 连接列表） |

**问题**：
- 4个独立窗口体验割裂，用户需在不同窗口间切换
- 主窗口尺寸过小（400×560），信息密度低
- 列表无分页，数据量大时滚动体验差
- 搜索为简单 `includes` 子串匹配，无法模糊匹配

### 1.2 目标

1. **合并4个页面为单窗口**，通过顶部tab切换
2. **放大视口**，自适应屏幕尺寸，可调整大小
3. **分页展示**大列表，支持每页大小可选（10/20/50/100，默认20）
4. **子序列模糊搜索**（保序）：输入 `api` 匹配 `application`、`api.example.com`

### 1.3 非目标

- 不引入前端构建工具（webpack/vite），保持纯HTML+JS
- 不引入前端框架（Vue/React），保持原生JS
- 不引入测试框架（pytest/jest），手动验证为主
- 不优化 PowerShell 启动开销
- 不改变流量连接列表的搜索方式（保持 `includes`）
- 不改变认证流程逻辑（仅合并UI）
- 不改变后端API接口签名（仅新增 `save_ui_prefs`/`get_ui_prefs`）

## 2. 整体架构

### 2.1 合并方案

**方案A（选定）：扩展 settings.html 为单文件**

复用 settings.html 现有的 tab 机制，新增"WARP排除""流量"两个tab，将其他3个HTML的HTML/CSS/JS合并进来。

- **优点**：pywebview 的 `js_api` 在主frame直接可用，无需桥接；复用现有tab机制和CSS变量；无跨文件加载问题
- **缺点**：单文件较大（约3700行），需用注释严格分区组织

**保留 `settings.html` 文件名不重命名**，减少改动面（spec、get_resource_path 调用、文档引用都无需改）。

### 2.2 窗口布局

```
┌─────────────────────────────────────────────┐
│  CampusAuth                    _ □ ✕        │  ← 自定义标题栏（拖拽区）
├─────────────────────────────────────────────┤
│ [连接] [设置] [WARP排除] [流量]              │  ← tab栏
├─────────────────────────────────────────────┤
│                                             │
│             当前 tab 内容区                  │
│         （自适应剩余高度）                    │
│                                             │
└─────────────────────────────────────────────┘
```

### 2.3 tab 结构

| tab | 来源 | 说明 |
|-----|------|------|
| 连接 | settings.html `tab-status` | 一键认证状态，保持不变 |
| 设置 | settings.html `tab-settings` | 配置页，保持不变 |
| WARP排除 | warp_exclusion.html 全部内容 | 含学习域名列表（分页+子序列搜索）、规则列表（分页+新增子序列搜索）、流量推荐 |
| 流量 | traffic_monitor.html + traffic_flow.html 合并 | 顶部子视图切换按钮：[列表视图] [画布动画]；列表视图来自 traffic_monitor，画布动画来自 traffic_flow |

### 2.4 流量tab子视图切换

"流量"tab内部顶部有两个按钮切换子视图：
- **列表视图**：来自 traffic_monitor.html（6类统计卡片 + 连接列表，保持现有 `includes` 搜索）
- **画布动画**：来自 traffic_flow.html（粒子画布 + 连接列表，保持现有 `includes` 搜索）
- 切换时：隐藏当前子视图DOM，显示另一个
- 画布动画切走时 `cancelAnimationFrame` 暂停，切回时 `requestAnimationFrame` 恢复
- 子视图选择持久化（记住上次看的是列表还是画布）

### 2.5 文件组织

合并后 `settings.html` 内用注释分区：

```
<!-- ===== 共享CSS ===== -->
<!-- ===== 标题栏 + tab栏 ===== -->
<!-- ===== 连接tab ===== -->
<!-- ===== 设置tab ===== -->
<!-- ===== WARP排除tab ===== -->
<!-- ===== 流量tab ===== -->
<!-- ===== 共享JS工具 ===== -->
<!-- ===== 各tab JS ===== -->
```

原 `warp_exclusion.html`、`traffic_monitor.html`、`traffic_flow.html` 合并后**删除**。

## 3. 窗口尺寸与持久化

### 3.1 窗口尺寸

- 启动时默认占屏幕 85%（宽高均按屏幕分辨率计算）
- `resizable=True`（当前为 `False`，需改为 `True`）
- 最小尺寸限制：800×600（避免过小导致布局错乱）
- `TrayApp.WIN_W / WIN_H` 改为初始默认值，实际从配置读取

### 3.2 持久化字段

存入现有 `tray_config.json`，新增字段：

```json
{
  "window": {
    "width": 1632,
    "height": 918,
    "x": 144,
    "y": 81
  },
  "ui_prefs": {
    "page_size": 20,
    "traffic_subview": "list"
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `window.width/height` | 上次关闭时窗口尺寸 | 屏幕85% |
| `window.x/y` | 上次关闭时窗口位置 | 居中 |
| `ui_prefs.page_size` | WARP排除列表每页条数（10/20/50/100） | 20 |
| `ui_prefs.traffic_subview` | 流量tab子视图（"list" 或 "canvas"） | "list" |

### 3.3 持久化时机

- **窗口尺寸/位置**：窗口 `on_closing` 事件触发时读取当前 `x/y/width/height` 写入配置（扩展现有 `save_window_position` 模式）
- **ui_prefs**：用户改变每页大小或切换子视图时，前端调用 `api.save_ui_prefs({page_size: 50})` 立即写入；后端 `ApiBridge` 新增方法 `save_ui_prefs` / `get_ui_prefs`

### 3.4 首次启动与容错

- 配置中无 `window` 字段时，按屏幕85%计算并居中
- 配置中无 `ui_prefs` 字段时，使用默认值
- 若保存的尺寸超出当前屏幕边界（如外接显示器拔出），启动时校正：`x = max(0, min(x, screen_w - width))`，`y` 同理；若 `width > screen_w` 则回退到85%

### 3.5 后端改动点

1. **tray_app.py**：
   - `TrayApp.WIN_W/WIN_H` 改为初始默认（用于首次启动计算）
   - `run()` 方法中 `webview.create_window` 的 `width/height/x/y` 从配置读取
   - `resizable=False` → `True`
   - 新增 `minsize=(800, 600)` 参数
   - `on_closing` 中调用 `save_window_geometry()` 保存尺寸+位置

2. **ApiBridge 新增**：
   ```python
   def save_ui_prefs(self, prefs):
       """保存 UI 偏好（page_size, traffic_subview）"""

   def get_ui_prefs(self):
       """读取 UI 偏好，供前端初始化"""
   ```

3. **core/config.py**：CONFIG 数据结构扩展 `window` 和 `ui_prefs` 字段（向后兼容，缺失时用默认值）

## 4. 分页与搜索

### 4.1 分页应用范围

仅 WARP排除tab的两个列表应用分页：
- **学习域名列表**（`learnedList`）
- **规则列表**（`rulesList`）

流量连接列表不分页（保持现有滚动行为）。

### 4.2 分页UI

每个列表下方统一分页栏：

```
┌─────────────────────────────────────────┐
│ 搜索框 [模糊搜索域名...]      [12/85]   │
├─────────────────────────────────────────┤
│ ▢ domain1.com                          │
│ ▢ domain2.com                          │
│ ▢ domain3.com                          │
│ ...（当前页条目）                       │
├─────────────────────────────────────────┤
│ 每页 [20 ▼]  ← 1 2 3 4 5 →  共85条     │
└─────────────────────────────────────────┘
```

- **每页大小选择器**：下拉框，选项 10/20/50/100，默认20，改变后立即生效并持久化到 `ui_prefs.page_size`
- **页码导航**：首页、上一页、页码（最多显示7个，超长省略为 `1 2 ... 5 6 7 ... 20`）、下一页、末页
- **总数显示**：右侧显示"共 N 条"
- **搜索结果计数**：搜索框右侧显示 `匹配数/总数`（如 `12/85`）

### 4.3 分页交互规则

- **搜索+分页协同**：输入搜索词后，先过滤再分页。例如85条中搜索匹配12条，每页20，则显示第1页12条，页码只有1页
- **改变每页大小**：重置到第1页
- **翻页**：保持当前搜索条件
- **全选**：仅作用于当前页可见的条目（与现有 `toggleSelectAll` 行为一致）
- **数据刷新**：后台数据更新时（如新增规则后），保持当前页码，若当前页超出范围则自动回退到最后一页

### 4.4 分页状态

每个列表独立的分页状态对象：

```javascript
const listPagination = {
  learned: { page: 1, pageSize: 20, total: 0, keyword: '' },
  rules:   { page: 1, pageSize: 20, total: 0, keyword: '' }
};
```

两个列表共享 `pageSize`（从 `ui_prefs.page_size` 读取），改变一个列表的 pageSize 同步另一个并持久化。

### 4.5 子序列匹配算法

仅 WARP排除两个列表应用子序列匹配。流量连接列表保持现有 `includes` 子串匹配。

```javascript
function fuzzyMatch(text, query) {
    if (!query) return true;
    text = text.toLowerCase();
    query = query.toLowerCase();
    let qi = 0;  // query 指针
    for (let ti = 0; ti < text.length && qi < query.length; ti++) {
        if (text[ti] === query[qi]) qi++;
    }
    return qi === query.length;
}
```

- 保序：`api` 匹配 `application`、`api.example.com`
- 不要求连续：`api` 也匹配 `a-x-p-i.com`
- 大小写不敏感
- 空搜索词匹配所有

### 4.6 搜索应用位置

| 列表 | 原匹配方式 | 新匹配方式 |
|------|-----------|-----------|
| WARP排除-学习域名 | `includes` 子串 | 子序列保序 |
| WARP排除-规则列表 | 无搜索框 | 新增搜索框 + 子序列保序 |
| 流量-连接列表（2处） | `includes` 子串 | 保持不变 |

### 4.7 规则列表搜索框

规则列表原本无搜索框，在 `rulesList` 上方新增一个与学习域名列表相同样式的搜索框，搜索字段为 `domain`。过滤后应用分页。

### 4.8 搜索去抖

输入搜索词时，150ms 去抖后才触发过滤+重置到第1页+渲染，避免快速输入时卡顿。

## 5. 托盘菜单与tab切换

### 5.1 托盘菜单调整

| 原菜单项 | 新菜单项 | 点击行为 |
|---------|---------|---------|
| WARP排除管理 | WARP排除 | 打开主窗口 → 切到"WARP排除"tab |
| 流量监控 | 流量 | 打开主窗口 → 切到"流量"tab → 默认"列表"子视图 |
| 流量可视化 | （合并到"流量"） | 同上，可通过流量tab内子视图切换 |
| 打开主窗口 / 设置 | 保持不变 | 打开主窗口，保持上次tab |

### 5.2 后端改动

#### 删除独立窗口逻辑

tray_app.py 中删除以下方法和相关引用：
- `show_exclusion()` 方法
- `show_traffic_monitor()` 方法
- `show_flow_monitor()` 方法
- 对应的窗口引用字段：`self._exclusion_window` / `self._traffic_window` / `self._flow_window`

#### 新增统一入口方法

```python
def show_main_window(self, tab=None):
    """打开主窗口并切换到指定tab。
    tab: 'status' | 'settings' | 'warp' | 'traffic' | None（保持上次）
    """
    # 复用现有 show_settings() 的窗口显示逻辑
    # 若窗口未创建则创建，已创建则显示并置顶
    if tab:
        # 窗口显示后通过 JS API 切到指定 tab
        self.settings_window.evaluate_js(f"switchTab('{tab}')")
```

#### 托盘菜单调用

```python
menu_items = [
    MenuItem('打开主窗口', lambda: self.show_main_window()),
    MenuItem('WARP排除', lambda: self.show_main_window('warp')),
    MenuItem('流量', lambda: self.show_main_window('traffic')),
    # ... 其他菜单项
]
```

### 5.3 前端 switchTab 扩展

现有 `switchTab` 函数扩展为支持4个tab：

```javascript
function switchTab(tabName) {
    // 切换所有 tab-btn 和 tab-content 的 active 状态
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector(`.tab-btn[onclick*="${tabName}"]`).classList.add('active');
    document.getElementById(`tab-${tabName}`).classList.add('active');
    
    // 流量tab特殊处理：切到时恢复子视图，切走时暂停画布
    if (tabName === 'traffic') {
        TrafficTab.showSubview(TrafficTab.subview);
        TrafficTab.startAutoRefresh();
    } else {
        TrafficTab.stopAutoRefresh();
        TrafficTab.pauseCanvas();
    }
}
```

### 5.4 CampusAuth.spec 调整

- 移除 `warp_exclusion.html`、`traffic_monitor.html`、`traffic_flow.html` 的引用
- 保留 `settings.html` 文件名

### 5.5 启动行为

启动时默认显示"连接"tab，不恢复上次tab。

## 6. CSS合并与样式统一

### 6.1 CSS 变量统一

4个HTML的 `:root` 变量已高度一致（`--bg-primary`、`--accent` 等全部相同），合并为一个 `:root` 块。

差异处理：
- `--accent-dim`：settings 用 `rgba(246,131,32,0.15)`，warp_exclusion 用 `rgba(246,131,32,0.12)` → 统一为 `rgba(246,131,32,0.15)`
- 流量页面特有的6类颜色变量（`--c-ipv4` 等）保留在 `:root`

### 6.2 标题栏统一

合并后顶部为自定义标题栏 + tab栏，取代各页面原有的标题栏：

```css
.app-title-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 12px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    -webkit-app-region: drag;
}
```

各页面原 `.title-bar` / `.title-bar-btns` 删除，统一用 `.app-title-bar`。

### 6.3 tab栏样式

复用 settings.html 现有 `.tab-bar` / `.tab-btn`，从2个tab扩展为4个。

### 6.4 组件样式合并

重复的组件样式合并：
- **按钮 `.btn`**：4个HTML都有定义且高度一致，合并为一个定义
- **输入框 `.search-input`**：3个HTML都有，合并
- **空提示 `.empty-hint`**：合并
- **加载指示器 `.loading-spinner`**：合并
- **Toast 提示**：合并为一个统一的 `showToast()` 实现
- **列表项**（`.domain-item` / `.conn-item` / `.rule-item`）：样式不同，各自保留但放在对应tab的CSS区块

### 6.5 文件内CSS组织

```css
/* ===== 共享变量 ===== */
:root { ... }

/* ===== 共享组件（按钮、输入框、提示等）===== */
.btn { ... }
.search-input { ... }
.empty-hint { ... }
.loading-spinner { ... }

/* ===== 标题栏 + tab栏 ===== */
.app-title-bar { ... }
.tab-bar { ... }

/* ===== 连接tab ===== */
.status-page { ... }

/* ===== 设置tab ===== */
.settings-page { ... }

/* ===== WARP排除tab ===== */
.domain-list { ... }
.rule-item { ... }
.pagination-bar { ... }

/* ===== 流量tab ===== */
.stats-grid { ... }
.traffic-canvas { ... }
```

### 6.6 body overflow

- `body { overflow: hidden }` 保持
- 各 tab-content 内部列表区域 `overflow-y: auto`，仅列表滚动而非整页

## 7. JS合并与生命周期管理

### 7.1 命名空间隔离

采用命名空间对象隔离各tab的状态和函数：

```javascript
// ===== 连接tab =====
const StatusTab = {
    statusIndicator: null,
    init() { ... },
    startAuth() { ... },
};

// ===== 设置tab =====
const SettingsTab = {
    config: null,
    init() { ... },
    saveConfig() { ... },
};

// ===== WARP排除tab =====
const WarpTab = {
    allLearnedDomains: [],
    checkedDomains: new Set(),
    pagination: { learned: {...}, rules: {...} },
    init() { ... },
    loadRules() { ... },
    renderLearnedList() { ... },
    renderRules() { ... },
};

// ===== 流量tab =====
const TrafficTab = {
    subview: 'list',
    lastData: null,
    cumulativeConns: new Map(),
    _loadingFast: false,
    _loadingSlow: false,
    _canvasAnimId: null,
    init() { ... },
    refreshFast() { ... },
    refreshSlow() { ... },
    showSubview(name) { ... },
    pauseCanvas() { ... },
    resumeCanvas() { ... },
};
```

### 7.2 共享工具函数

```javascript
const Utils = {
    escapeHtml(s) { ... },
    fuzzyMatch(text, query) { ... },
    showToast(msg, type) { ... },
    debounce(fn, delay) { ... },
    paginate(items, page, pageSize) {
        const start = (page - 1) * pageSize;
        return items.slice(start, start + pageSize);
    },
    renderPagination(container, state, onPageChange) { ... }
};
```

### 7.3 函数重命名去重

| 原函数 | 来源 | 冲突处理 |
|--------|------|---------|
| `escapeHtml` | traffic_flow/traffic_monitor | 合并为 `Utils.escapeHtml` |
| `showToast` | settings/warp_exclusion/traffic_flow | 合并为 `Utils.showToast` |
| `showLoading`/`hideLoading` | settings/warp_exclusion | 合并为 `Utils.showLoading`/`Utils.hideLoading` |
| `renderConnections` | traffic_monitor | → `TrafficTab.renderConnList()` |
| `renderConnList` | traffic_flow | → `TrafficTab.renderConnListCanvas()` |
| `filterLearnedList` | warp_exclusion | → `WarpTab.filterLearnedList()` |
| `renderRules` | warp_exclusion | → `WarpTab.renderRules()` |
| `init` | traffic_monitor/traffic_flow 各自的 init | → `TrafficTab.init()` 合并 |
| `refreshFast`/`refreshSlow` | traffic_monitor/traffic_flow 各自的 | → `TrafficTab.refreshFast()`/`refreshSlow()` 合并（按 `subview` 渲染目标） |

### 7.4 流量tab子视图合并

traffic_monitor 和 traffic_flow 的 `refreshFast` / `refreshSlow` 逻辑高度相似，合并为一套，通过 `subview` 判断渲染目标：

```javascript
TrafficTab.refreshFast = async function() {
    if (!api || this._loadingFast) return;
    this._loadingFast = true;
    try {
        const data = await api.get_traffic_status_fast();
        // 保留上一轮 hostname（IF-1 修复逻辑）
        if (this.lastData?.connections) {
            const oldHost = new Map();
            for (const c of this.lastData.connections) {
                if (c.hostname) oldHost.set(c.remote_ip, c.hostname);
            }
            for (const c of data.connections) {
                if (!c.hostname && oldHost.has(c.remote_ip)) {
                    c.hostname = oldHost.get(c.remote_ip);
                }
            }
        }
        this.lastData = data;
        this.stats = data.stats || this.stats;
        this.warpUnderlay = data.warp_underlay || 'ipv4';
        if (this.cumulativeMode && data.connections?.length) {
            for (const c of data.connections) this.cumulativeConns.set(this.connId(c), c);
        }
        // 根据当前子视图渲染
        if (this.subview === 'list') {
            this.updateStats();
            this.renderConnList();
        } else {
            this.updateCanvasStats();
            this.renderCanvasConnList();
            this.drawCanvas();
        }
    } catch (e) {
        Utils.showToast('获取失败: ' + e);
    } finally {
        this._loadingFast = false;
    }
};
```

### 7.5 画布动画生命周期

- **切到画布子视图**：`TrafficTab.resumeCanvas()` → 恢复 `requestAnimationFrame` 循环
- **切到列表子视图**：`TrafficTab.pauseCanvas()` → `cancelAnimationFrame(this._canvasAnimId)`
- **切走流量tab**：`switchTab` 中调用 `TrafficTab.pauseCanvas()` 节省CPU
- **切回流量tab**：恢复上次子视图，若是画布则 `resumeCanvas()`

### 7.6 自动刷新管理

- 切到流量tab时启动 `setInterval(refreshFast, 3000)`
- 切走流量tab时 `clearInterval` 停止自动刷新
- 连接tab的认证状态轮询、WARP排除tab的数据加载保持各自原有逻辑，切走时不停止

### 7.7 初始化时序

```javascript
async function initApp() {
    await waitForApi(15000);
    const prefs = await api.get_ui_prefs();
    WarpTab.pageSize = prefs.page_size || 20;
    TrafficTab.subview = prefs.traffic_subview || 'list';
    StatusTab.init();
    SettingsTab.init();
    WarpTab.init();
    TrafficTab.init();
    switchTab('status');
}
```

## 8. 错误处理

### 8.1 前端错误处理

| 场景 | 处理方式 |
|------|---------|
| API 调用失败 | `try/catch` + `Utils.showToast('操作失败: ' + e, 'error')`，不阻塞UI |
| 配置读取失败 | 使用默认值，不报错 |
| 配置保存失败 | Toast 提示，不影响当前操作 |
| 分页越界 | 自动回退到最后一页 |
| 画布动画异常 | `try/catch` 包裹，失败时暂停画布并 Toast 提示 |
| 子视图切换时 API 未就绪 | `if (!api) return;` 前置检查 |

### 8.2 后端错误处理

| 场景 | 处理方式 |
|------|---------|
| 窗口尺寸读取（配置无字段） | 返回默认值 |
| 窗口尺寸越界 | 启动时校正 |
| `save_ui_prefs` 写入失败 | `try/except` + `logger.error`，返回 `False` |
| `evaluate_js` 失败 | `try/except` + `logger.warning`，不重试 |
| 窗口已销毁仍调用 | `try/except AttributeError` 静默处理 |

## 9. 测试策略

### 9.1 后端验证

| 验证项 | 方法 |
|--------|------|
| 模块导入正常 | `python -c "import tray_app; from core import config"` |
| 配置读写 | `python -c "from core.config import CONFIG; CONFIG.save(); print(CONFIG.to_dict())"` |
| ApiBridge 新方法存在 | `python -c "from tray_app import ApiBridge; print(hasattr(ApiBridge, 'save_ui_prefs'), hasattr(ApiBridge, 'get_ui_prefs'))"` |
| 无密码泄漏 | `grep -r "password" core/ tray_app.py` |
| 无 `os._exit` | `grep -r "os._exit" core/ tray_app.py` |

### 9.2 前端验证清单（手动）

**tab切换**
- [ ] 4个tab都能切换，内容正确显示
- [ ] 流量tab切到画布子视图，画布动画运行
- [ ] 流量tab切到列表子视图，画布暂停
- [ ] 切走流量tab，画布暂停
- [ ] 切走流量tab后CPU占用下降

**分页**
- [ ] 学习域名列表分页正确（85条/20每页 → 5页）
- [ ] 规则列表分页正确
- [ ] 改变每页大小（10/20/50/100）立即生效
- [ ] 翻页后数据正确
- [ ] 搜索后分页重置到第1页
- [ ] 改变每页大小后重置到第1页
- [ ] 全选仅作用于当前页

**搜索**
- [ ] 输入 `api` 匹配 `application`、`api.example.com`
- [ ] 输入 `api` 匹配 `a-x-p-i.com`（不要求连续）
- [ ] 输入 `api` 不匹配 `pia.com`（不保序）
- [ ] 大小写不敏感
- [ ] 搜索框150ms去抖生效
- [ ] 流量连接列表搜索保持 `includes` 子串匹配

**持久化**
- [ ] 调整窗口大小后关闭重启，尺寸保持
- [ ] 拖动窗口位置后关闭重启，位置保持
- [ ] 改变每页大小后关闭重启，每页大小保持
- [ ] 切换流量子视图后关闭重启，子视图保持
- [ ] 拔外接显示器后启动，窗口在屏幕内

**托盘菜单**
- [ ] 点击"WARP排除"打开主窗口并切到WARP排除tab
- [ ] 点击"流量"打开主窗口并切到流量tab
- [ ] 点击"打开主窗口"保持上次tab
- [ ] 原独立窗口方法已删除

**合并完整性**
- [ ] `warp_exclusion.html`、`traffic_monitor.html`、`traffic_flow.html` 已删除
- [ ] `CampusAuth.spec` 无3个HTML的引用
- [ ] 打包后 `settings.html` 正常加载

### 9.3 性能验证

| 指标 | 目标 |
|------|------|
| 流量tab列表视图首屏（fast） | <4s（保持优化成果） |
| 画布动画帧率 | 30-60fps（CPU<15%） |
| 切tab延迟 | <100ms（无网络请求的tab） |
| 分页渲染（100条） | <50ms |
| 搜索过滤（100条） | <20ms |

## 10. 涉及文件清单

### 修改

| 文件 | 改动 |
|------|------|
| `settings.html` | 合并3个HTML内容，扩展为4个tab，新增分页/搜索/命名空间JS |
| `tray_app.py` | 删除3个独立窗口方法，新增 `show_main_window`，窗口尺寸持久化，`resizable=True`，`minsize` |
| `core/config.py` | CONFIG 扩展 `window` 和 `ui_prefs` 字段 |
| `CampusAuth.spec` | 移除3个HTML的引用 |

### 删除

| 文件 | 原因 |
|------|------|
| `warp_exclusion.html` | 内容合并进 settings.html |
| `traffic_monitor.html` | 内容合并进 settings.html |
| `traffic_flow.html` | 内容合并进 settings.html |

### 新增

无新文件。

## 11. 设计决策记录

1. **为何选择单文件合并而非iframe/片段加载**：pywebview 的 `js_api` 必须在主frame可用，iframe需postMessage桥接复杂且样式隔离，fetch片段在 `file://` 下有CORS风险
2. **为何保留 settings.html 文件名**：减少改动面，spec/get_resource_path/文档引用都无需改
3. **为何仅WARP排除列表分页**：流量连接列表实时变化，分页会导致频繁重置；WARP排除列表相对稳定
4. **为何仅WARP排除列表用子序列搜索**：用户明确要求；流量连接列表用户可能需要精确匹配IP/端口
5. **为何启动时不恢复上次tab**：避免认证状态未就绪时进入流量tab导致空数据
6. **为何流量tab切走时停止自动刷新**：避免后台轮询浪费资源，切回时重新启动
