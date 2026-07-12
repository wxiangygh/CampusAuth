# 主页tab合并设计：连接+设置 → 主页

> **日期：** 2026-07-12
> **背景：** 页面合并后形成4-tab结构（连接/设置/WARP排除/流量），其中"连接"和"设置"两个tab内容过少，在放大视口（85%屏幕，约1740×979）下出现大面积空白。本设计将两者合并为单一"主页"tab，并补充可折叠的网络详情卡片，彻底填满空间。

## 1. 目标与决策

### 问题
- **连接tab**：六边形状态图标 + 标题/副标题 + 进度条 + 3按钮，内容约400px高，窗口约900px高 → 下方约500px空白
- **设置tab**：6个表单项（WiFi/账号/密码/4个开关/WARP路径），紧凑堆顶，内容约350px高 → 下方约550px空白

### 决策
1. **合并方向**：将"连接"和"设置"两个tab合并成一个页面
2. **布局**：上下分区（状态上 + 设置下）
3. **内容补充**：在状态区与设置区之间增加可折叠的"网络详情"卡片，展示6项网络信息
4. **Tab命名**："主页"，作为默认首页含状态+设置的综合页
5. **详情卡折叠**：支持点击标题栏折叠/展开，状态持久化

### Tab结构变化
| 原 | 新 |
|---|---|
| 4 tab：连接/设置/WARP排除/流量 | 3 tab：主页/WARP排除/流量 |

## 2. 整体架构与布局

### 主页布局（上下三区）

```
┌─────────────────────────────────┐
│  [主页] [WARP排除] [流量]        │  ← tab栏
├─────────────────────────────────┤
│         ┌─────────┐             │
│         │ 六边形   │             │  ← 状态区（上）
│         │ 状态图标 │             │    现有 status-page 内容
│         └─────────┘             │    hexagon + 标题/副标题 + 进度条 + 认证按钮
│         WARP已连接               │
│         IPv4已禁用                │
│    [开始认证] [恢复网络]          │
├─────────────────────────────────┤
│  网络详情 ▼                      │  ← 网络详情卡（中）新增，可折叠
│  ┌──────┬──────┬──────┐        │    3×2 网格布局
│  │IPv4  │IPv6  │MAC   │        │    展示：IPv4/IPv6/MAC/
│  │10.x  │2001..│AABB..│        │    WiFi名/接口/WARP状态
│  ├──────┼──────┼──────┤        │
│  │WiFi  │接口   │WARP  │        │
│  │CMCC..│WLAN  │已连接 │        │
│  └──────┴──────┴──────┘        │
├─────────────────────────────────┤
│  WiFi 网络    [扫描 ▼]          │
│  认证账号    [________]         │  ← 设置区（下）
│  认证密码    [______] [显示]     │    现有 settings-page 表单
│  ┌自动认证┐ ┌开机自启┐         │    保持原样
│  └────────┘ └────────┘         │
│  ┌自动恢复┐ ┌静默启动┐         │
│  └────────┘ └────────┘         │
│  WARP-CLI路径 [____] [浏览]     │
└─────────────────────────────────┘
```

### 网络详情卡折叠机制
- 详情卡顶部加可点击的标题栏："网络详情 ▼/▶"
- 点击切换展开/收起，收起时只显示标题栏一行
- 折叠状态持久化到 `ui_prefs.network_detail_collapsed`（参考现有 `traffic_subview` 模式）
- 默认展开（首次使用时展示信息，用户嫌占空间可手动收起）
- 折叠动画用 `max-height` transition 实现平滑过渡

## 3. 后端接口与数据流

### 新增 ApiBridge 方法（tray_app.py）

```python
def get_network_detail(self):
    """聚合网络详情，供主页tab展示。
    返回 dict，失败字段为空字符串。
    复用 core.network 已有函数：
    - get_local_ip()          → ipv4
    - has_public_ipv6()       → (bool, addr) → ipv6 / ipv6_status
    - get_mac_address()       → mac
    - get_current_wifi_ssid() → wifi_ssid
    - get_wifi_interface_name() → interface
    WARP 状态：复用 check_network_status 逻辑
    """
    return {
        'ipv4': '10.x.x.x',
        'ipv6': '2001:da8:...',
        'ipv6_status': 'public',  # public|none
        'mac': 'AABBCCDDEEFF',
        'wifi_ssid': 'CMCC_BJUT_SUSHE_H1010-5G',
        'interface': 'WLAN',
        'warp_connected': True,
    }
```

### 为什么新建聚合方法而非前端多次调用
- 前端若分别调6个接口，需6次pywebview RPC往返（每次 `netsh`/`ipconfig` 子进程约100-300ms，串行总耗1-2s）
- 聚合到后端一次调用，省去RPC开销，总耗约500-800ms
- 单一数据源，字段缺失/错误处理集中

### 数据流

```
主页tab初始化
  ├─ HomeTab.initSettings()   ← 原 SettingsTab.init 逻辑，调 load_config()
  ├─ HomeTab.initStatus()     ← 原 StatusTab.init 逻辑，调 check_network_status()
  └─ HomeTab.refreshDetail()  ← 新增，调 get_network_detail()
       └─ 渲染到 #network-detail-grid
```

### 刷新时机

| 触发点 | 调用 | 说明 |
|--------|------|------|
| `initApp()` 启动 | `HomeTab.init()` | 首次加载，设置表单→状态→详情 |
| `switchTab('home')` | `HomeTab.refreshDetail()` | 切回主页时刷新网络详情（状态/设置不重复加载） |
| `finishAuth()` 完成 | `HomeTab.refreshDetail()` | 认证/恢复完成后网络状态可能变化 |
| `onAuthProgress` 自动认证完成 | 同上 | 后端触发的自动认证完成时 |

**不做自动定时刷新**（避免 `netsh`/`ipconfig` 频繁子进程）。

### ui_prefs 扩展（tray_app.py 的 save_ui_prefs/get_ui_prefs）
- 新增字段 `network_detail_collapsed: bool`，默认 `False`
- 验证逻辑参考现有 `traffic_subview` 模式

**不新增独立 Python 文件**，全部修改在 `tray_app.py` 和 `settings.html` 内。

## 4. 前端结构与命名空间调整

### HTML 结构变化（settings.html）

```html
<!-- 原 tab-status + tab-settings 合并为 tab-home -->
<div class="tab-content active" id="tab-home">
    <!-- 状态区（上）-->
    <div class="status-page">
        <canvas class="particle-canvas" id="particleCanvas" width="300" height="300"></canvas>
        <div class="status-indicator idle" id="statusIndicator">...</div>
        <div class="status-label" id="statusLabel">STATUS</div>
        <div class="status-title" id="statusTitle">一键认证</div>
        <div class="status-subtitle" id="statusSubtitle">点击下方按钮开始认证</div>
        <div class="progress-container" id="progressContainer">...</div>
        <div class="status-actions">
            <button onclick="startAuth()">开始认证</button>
            <button onclick="startRestore()">恢复网络</button>
            <button onclick="cancelOperation()">取消</button>
        </div>
    </div>

    <!-- 网络详情卡（中，可折叠）-->
    <div class="network-detail" id="networkDetail">
        <div class="network-detail-header" onclick="HomeTab.toggleDetail()">
            <span class="network-detail-title">网络详情</span>
            <span class="network-detail-arrow" id="networkDetailArrow">▼</span>
        </div>
        <div class="network-detail-body" id="networkDetailBody">
            <div class="network-detail-grid" id="networkDetailGrid">
                <!-- 6 项：IPv4/IPv6/MAC/WiFi/接口/WARP -->
                <!-- JS 动态填充 -->
            </div>
        </div>
    </div>

    <!-- 设置区（下）-->
    <div class="settings-page">
        <!-- 现有 settings-page 内容原样搬入 -->
    </div>
</div>
```

### Tab 栏调整

```html
<button class="tab-btn active" onclick="switchTab('home', event)">主页</button>
<button class="tab-btn" onclick="switchTab('warp', event)">WARP排除</button>
<button class="tab-btn" onclick="switchTab('traffic', event)">流量</button>
```

### 命名空间调整

| 原命名空间 | 变化 |
|-----------|------|
| `StatusTab` | 合并到新的 `HomeTab` |
| `SettingsTab` | 合并到新的 `HomeTab` |
| `WarpTab` | 不变 |
| `TrafficTab` | 不变 |
| `Utils` | 不变 |
| — | 新增 `HomeTab` |

```javascript
const HomeTab = {
    // 状态区（继承原 StatusTab）
    async initStatus() { /* 原 StatusTab.init 逻辑 */ },

    // 设置区（继承原 SettingsTab）
    async initSettings() { /* 原 SettingsTab.init 逻辑 */ },

    // 网络详情区（新增）
    _detailCollapsed: false,
    async refreshDetail() {
        const data = await window.pywebview.api.get_network_detail();
        this.renderDetailGrid(data);
    },
    renderDetailGrid(data) { /* 渲染6项到 #networkDetailGrid */ },
    toggleDetail() {
        this._detailCollapsed = !this._detailCollapsed;
        // 切换 .collapsed class 控制展开/收起
        // 持久化到 ui_prefs.network_detail_collapsed
    },
    setDetailCollapsed(collapsed) { /* 初始化时调用 */ },

    // 统一入口
    async init() {
        await this.initSettings();   // 先加载设置表单
        this.initStatus();           // 再检查网络状态
        await this.refreshDetail();  // 最后加载网络详情
    }
};
```

### 全局函数保留
原 `startAuth`/`startRestore`/`cancelOperation`/`updateStatusFromCheck`/`setStatus`/`showProgress` 等全局函数保持不变（已存在并被 onclick 直接引用，无需迁移到 HomeTab 内，避免大规模改动）。

### switchTab 调整
- `'home'` 进入时：调 `HomeTab.refreshDetail()` 刷新网络详情
- `'warp'`/`'traffic'` 切走时：现有 TrafficTab 生命周期逻辑不变，主页无特殊清理

### 托盘菜单（tray_app.py）调整
- 原"打开设置"→ `show_main_window('settings')` 改为 `show_main_window('home')`
- 其他托盘项（WARP排除/流量）不变

### 删除内容
- 删除 `id="tab-status"` 和 `id="tab-settings"` 两个容器
- 删除 `StatusTab` 和 `SettingsTab` 两个命名空间对象
- 删除 `switchTab` 中对 `'status'`/`'settings'` 的处理分支

## 5. CSS 样式与折叠动画

### 主页容器布局

```css
#tab-home {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
}

/* 状态区：紧凑居中 */
#tab-home .status-page {
    padding: 20px 24px 16px;  /* 原 32px → 减小 */
    justify-content: flex-start;  /* 保持顶部对齐，避免六边形下沉 */
}
```

### 网络详情卡样式

```css
.network-detail {
    margin: 0 20px 12px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
}

.network-detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    cursor: pointer;
    user-select: none;
    transition: background 0.15s;
}
.network-detail-header:hover { background: var(--bg-tertiary); }
.network-detail-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
}
.network-detail-arrow {
    font-size: 10px;
    color: var(--text-tertiary);
    transition: transform 0.2s;
}
.network-detail.collapsed .network-detail-arrow { transform: rotate(-90deg); }

/* 折叠动画：max-height 过渡 */
.network-detail-body {
    max-height: 200px;
    transition: max-height 0.25s ease, padding 0.25s ease;
    padding: 0 14px 12px;
    overflow: hidden;
}
.network-detail.collapsed .network-detail-body {
    max-height: 0;
    padding: 0 14px 0;
}

/* 6 项网格布局 */
.network-detail-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
}
.network-detail-item {
    padding: 8px 10px;
    background: var(--bg-tertiary);
    border-radius: 6px;
}
.network-detail-label {
    font-size: 10px;
    color: var(--text-tertiary);
    text-transform: uppercase;
    margin-bottom: 2px;
}
.network-detail-value {
    font-size: 12px;
    color: var(--text-primary);
    word-break: break-all;
    font-family: 'Consolas', 'Monaco', monospace;  /* 等宽字体便于读 IP/MAC */
}
.network-detail-value.empty { color: var(--text-tertiary); }
.network-detail-value.success { color: var(--success); }
.network-detail-value.warning { color: var(--warning); }
```

### 响应式适配（窄窗口）

```css
@media (max-width: 600px) {
    .network-detail-grid { grid-template-columns: repeat(2, 1fr); }
}
```

### 设置区无样式变化
原 `.settings-page` 保持。

## 6. 渲染逻辑

### `HomeTab.renderDetailGrid(data)` 渲染逻辑

```javascript
renderDetailGrid(data) {
    const items = [
        { label: 'IPv4', value: data.ipv4, class: data.ipv4 ? '' : 'empty' },
        { label: 'IPv6', value: data.ipv6 || '无公网IPv6',
          class: data.ipv6 ? 'success' : 'warning' },
        { label: 'MAC', value: data.mac, class: '' },
        { label: 'WiFi', value: data.wifi_ssid || '未连接', class: data.wifi_ssid ? '' : 'warning' },
        { label: '接口', value: data.interface || '未知', class: '' },
        { label: 'WARP', value: data.warp_connected ? '已连接' : '未连接',
          class: data.warp_connected ? 'success' : 'warning' },
    ];
    document.getElementById('networkDetailGrid').innerHTML = items.map(it => `
        <div class="network-detail-item">
            <div class="network-detail-label">${it.label}</div>
            <div class="network-detail-value ${it.class}">${Utils.escapeHtml(it.value)}</div>
        </div>
    `).join('');
}
```

### 折叠状态持久化
- `initApp()` 中读取 `ui_prefs.network_detail_collapsed`，调用 `HomeTab.setDetailCollapsed(collapsed)` 设置初始状态
- `HomeTab.toggleDetail()` 切换后调用 `window.pywebview.api.save_ui_prefs({ network_detail_collapsed: newVal })`

### 异常处理
- `get_network_detail()` 后端任一字段获取失败返回空字符串，前端显示"—"，不阻断其他字段
- `HomeTab.refreshDetail()` 失败时显示 toast 但不阻塞主页其他功能

## 7. 测试与验证清单

### 功能验证
1. 启动应用，默认显示"主页"tab，状态区（六边形+按钮）+ 详情卡（展开）+ 设置表单三区可见
2. 详情卡显示6项网络信息，IPv4/IPv6/MAC/WiFi/接口/WARP状态正确
3. 点击"网络详情"标题栏，详情卡平滑折叠/展开，箭头旋转
4. 折叠后关闭重启应用，保持折叠状态
5. 点击"开始认证"，认证完成后详情卡自动刷新（WARP状态变化可见）
6. 切到 WARP排除/流量 tab 再切回主页，详情卡刷新
7. 窄窗口（<600px）时详情网格变为2列

### 回归验证
8. WARP排除 tab 功能正常（分页/搜索/学习）
9. 流量 tab 功能正常（列表/画布子视图切换/生命周期）
10. 托盘菜单"打开设置"→打开主页tab
11. 窗口尺寸持久化正常
12. `python -c "import tray_app; print('OK')"` 无报错
13. `python -c "from tray_app import ApiBridge; print(hasattr(ApiBridge, 'get_network_detail'))"` 返回 True

### 清理验证
14. Grep `tab-status`/`tab-settings` 在 settings.html 中无残留（除注释）
15. Grep `StatusTab`/`SettingsTab` 在 settings.html 中无残留

## 8. 影响范围

### 修改文件
| 文件 | 修改内容 |
|------|---------|
| `tray_app.py` | 新增 `ApiBridge.get_network_detail()`；扩展 `save_ui_prefs`/`get_ui_prefs` 支持 `network_detail_collapsed`；托盘菜单 `'settings'` → `'home'` |
| `settings.html` | 合并 `tab-status`+`tab-settings`→`tab-home`；新增网络详情卡 HTML/CSS；新增 `HomeTab` 命名空间；删除 `StatusTab`/`SettingsTab`；调整 tab 栏为3个；`switchTab` 新增 `'home'` 分支 |

### 不变文件
- `CampusAuth.spec`（仍只打包 `settings.html` + `app.ico`）
- `core/network.py`（复用现有函数）
- `warp_exclusion.py`/`traffic_monitor.py`（后端逻辑不变）

### 无新增文件
全部修改在现有文件内完成。
