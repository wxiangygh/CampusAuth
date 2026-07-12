# 主页tab合并实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将"连接"和"设置"两个内容过少的tab合并为单一"主页"tab（上下分区：状态上+详情中+设置下），新增可折叠的网络详情卡片，彻底填满放大视口下的空白。

**Architecture:** 后端在 `tray_app.py` 新增 `ApiBridge.get_network_detail()` 聚合方法（复用 `core.network` 现有函数）并扩展 `ui_prefs` 支持 `network_detail_collapsed`。前端在 `settings.html` 合并 `tab-status`+`tab-settings` 为 `tab-home`，新增 `HomeTab` 命名空间统一管理状态/设置/详情三区，详情卡用 `max-height` transition 实现折叠动画，状态持久化到 `tray_config.json`。

**Tech Stack:** Python 3.12 / pywebview 6.2.1 / HTML+CSS+JS（单文件）/ pystray

## Global Constraints

- 操作系统：Windows，命令链接用 `;`（PowerShell 不支持 `&&`）
- pywebview 版本：6.2.1，`create_window` 参数名为 `min_size`（不是 `minsize`）
- 项目无前端构建工具，`settings.html` 是单文件 HTML（含 `<style>` 和 `<script>`）
- 命名空间隔离：所有 JS 函数放在命名空间对象内（`HomeTab`/`WarpTab`/`TrafficTab`/`Utils`），避免全局污染
- ui_prefs 持久化模式：`save_ui_prefs(prefs_dict)` 合并保存，`get_ui_prefs()` 返回全部字段
- 不新增独立 Python 文件，全部修改在 `tray_app.py` 和 `settings.html` 内
- 中文注释，关键逻辑添加简明中文注释
- 分支：main，当前 HEAD：114ab68，计划 BASE commit：114ab68

---

## 文件结构

| 文件 | 责任 | 变化 |
|------|------|------|
| `d:\project_code\ipv6\tray_app.py` | 后端窗口管理与 API 桥 | 新增 `get_network_detail`；扩展 `save_ui_prefs`/`get_ui_prefs`；托盘菜单 `'settings'`→`'home'` |
| `d:\project_code\ipv6\settings.html` | 前端单文件（HTML+CSS+JS） | 合并两个tab为一个；新增网络详情卡 HTML/CSS；新增 `HomeTab` 命名空间；删除 `StatusTab`/`SettingsTab`；调整 tab 栏和 switchTab |

### 任务依赖图

```
Task 1 (后端: get_network_detail + ui_prefs 扩展)
   ↓
Task 2 (后端: 托盘菜单 'settings'→'home')
   ↓
Task 3 (前端: tab栏 4→3 + tab-home 容器创建)
   ↓
Task 4 (前端: 网络详情卡 HTML + CSS)
   ↓
Task 5 (前端: HomeTab 命名空间 + 删除 StatusTab/SettingsTab)
   ↓
Task 6 (前端: switchTab + initApp + finishAuth 整合)
   ↓
Task 7 (验证与提交)
```

---

## Task 1: 后端 - 新增 get_network_detail + 扩展 ui_prefs

**Files:**
- Modify: `d:\project_code\ipv6\tray_app.py`
  - `ApiBridge.save_ui_prefs`（约第 685-702 行）
  - `ApiBridge.get_ui_prefs`（约第 704-725 行）
  - 在 `ApiBridge` 类内新增 `get_network_detail` 方法（放在 `get_ui_prefs` 之后）

**Interfaces:**
- Consumes: `core.network.get_local_ip()`、`core.network.has_public_ipv6()`（返回 `tuple[bool, str]`）、`core.network.get_mac_address()`、`core.network.get_current_wifi_ssid()`、`core.network.get_wifi_interface_name()`、现有 `check_network_status()` 内部逻辑
- Produces:
  - `ApiBridge.get_network_detail() -> dict`：返回 `{'ipv4': str, 'ipv6': str, 'ipv6_status': str, 'mac': str, 'wifi_ssid': str, 'interface': str, 'warp_connected': bool}`
  - `ApiBridge.save_ui_prefs({'network_detail_collapsed': bool})`：支持新字段
  - `ApiBridge.get_ui_prefs()`：返回新增 `network_detail_collapsed: bool` 字段

- [ ] **Step 1: 修改 `save_ui_prefs` 文档字符串**

找到 `d:\project_code\ipv6\tray_app.py` 第 685-691 行的 `save_ui_prefs` 方法，替换文档字符串：

原代码：
```python
    def save_ui_prefs(self, prefs):
        """保存 UI 偏好（page_size, traffic_subview）。
        Args:
            prefs: dict，如 {'page_size': 50} 或 {'traffic_subview': 'canvas'}
        Returns:
            dict: {'success': bool}
        """
```

新代码：
```python
    def save_ui_prefs(self, prefs):
        """保存 UI 偏好（page_size, traffic_subview, network_detail_collapsed）。
        Args:
            prefs: dict，如 {'page_size': 50} 或 {'traffic_subview': 'canvas'} 或 {'network_detail_collapsed': True}
        Returns:
            dict: {'success': bool}
        """
```

- [ ] **Step 2: 修改 `get_ui_prefs` 返回值**

找到 `d:\project_code\ipv6\tray_app.py` 第 704-725 行的 `get_ui_prefs` 方法，替换为：

```python
    def get_ui_prefs(self):
        """读取 UI 偏好，供前端初始化。
        Returns:
            dict: {'page_size': int, 'traffic_subview': str, 'network_detail_collapsed': bool}
        """
        try:
            cfg = load_config()
            prefs = cfg.get('ui_prefs') or {}
            result = {
                'page_size': int(prefs.get('page_size', 20)),
                'traffic_subview': prefs.get('traffic_subview', 'list'),
                'network_detail_collapsed': bool(prefs.get('network_detail_collapsed', False))
            }
            # 校验 page_size 取值范围
            if result['page_size'] not in (10, 20, 50, 100):
                result['page_size'] = 20
            if result['traffic_subview'] not in ('list', 'canvas'):
                result['traffic_subview'] = 'list'
            logger.info(f"[get_ui_prefs] Returning: {result}")
            return result
        except Exception as e:
            logger.error(f"[get_ui_prefs] FAILED: {e}\n{traceback.format_exc()}")
            return {'page_size': 20, 'traffic_subview': 'list', 'network_detail_collapsed': False}
```

- [ ] **Step 3: 新增 `get_network_detail` 方法**

在 `get_ui_prefs` 方法之后（第 725 行 `icon_instance = None` 之前）插入新方法：

```python
    def get_network_detail(self):
        """聚合网络详情，供主页tab展示。
        复用 core.network 现有函数，任一字段获取失败返回空字符串。
        Returns:
            dict: {'ipv4': str, 'ipv6': str, 'ipv6_status': str,
                   'mac': str, 'wifi_ssid': str, 'interface': str,
                   'warp_connected': bool}
        """
        result = {
            'ipv4': '', 'ipv6': '', 'ipv6_status': 'none',
            'mac': '', 'wifi_ssid': '', 'interface': '',
            'warp_connected': False
        }
        try:
            # IPv4 地址
            try:
                result['ipv4'] = get_local_ip() or ''
            except Exception as e:
                logger.warning(f"[get_network_detail] get_local_ip failed: {e}")

            # IPv6 公网地址（has_public_ipv6 返回 tuple[bool, str]）
            try:
                has_ipv6, ipv6_addr = has_public_ipv6()
                if has_ipv6 and ipv6_addr:
                    result['ipv6'] = ipv6_addr
                    result['ipv6_status'] = 'public'
            except Exception as e:
                logger.warning(f"[get_network_detail] has_public_ipv6 failed: {e}")

            # MAC 地址
            try:
                result['mac'] = get_mac_address() or ''
            except Exception as e:
                logger.warning(f"[get_network_detail] get_mac_address failed: {e}")

            # WiFi SSID
            try:
                result['wifi_ssid'] = get_current_wifi_ssid() or ''
            except Exception as e:
                logger.warning(f"[get_network_detail] get_current_wifi_ssid failed: {e}")

            # 网络接口名
            try:
                result['interface'] = get_wifi_interface_name() or ''
            except Exception as e:
                logger.warning(f"[get_network_detail] get_wifi_interface_name failed: {e}")

            # WARP 连接状态（复用 check_network_status 逻辑）
            try:
                status = self.check_network_status()
                # status 为 'connected' 或 'partial' 时认为 WARP 已连接
                result['warp_connected'] = status.get('status') in ('connected', 'partial')
            except Exception as e:
                logger.warning(f"[get_network_detail] check_network_status failed: {e}")

            logger.info(f"[get_network_detail] Returning: ipv4={result['ipv4']}, ipv6_status={result['ipv6_status']}, warp={result['warp_connected']}")
            return result
        except Exception as e:
            logger.error(f"[get_network_detail] FAILED: {e}\n{traceback.format_exc()}")
            return result
```

- [ ] **Step 4: 验证后端导入和方法存在**

运行：
```powershell
python -c "from tray_app import ApiBridge; print('get_network_detail:', hasattr(ApiBridge, 'get_network_detail')); print('save_ui_prefs:', hasattr(ApiBridge, 'save_ui_prefs')); print('get_ui_prefs:', hasattr(ApiBridge, 'get_ui_prefs'))"
```

预期输出：
```
get_network_detail: True
save_ui_prefs: True
get_ui_prefs: True
```

- [ ] **Step 5: 验证 get_network_detail 功能**

运行：
```powershell
python -c "from tray_app import ApiBridge; ab = ApiBridge(); d = ab.get_network_detail(); print('keys:', sorted(d.keys())); print('warp_connected type:', type(d['warp_connected']).__name__); print('ipv6_status:', d['ipv6_status'])"
```

预期输出（字段值取决于实际网络，但 keys 和类型必须正确）：
```
keys: ['interface', 'ipv4', 'ipv6', 'ipv6_status', 'mac', 'wifi_ssid', 'warp_connected']
warp_connected type: bool
ipv6_status: public 或 none
```

- [ ] **Step 6: 提交**

```powershell
git add tray_app.py
git commit -m "feat: 新增 get_network_detail 聚合接口，ui_prefs 扩展 network_detail_collapsed"
```

---

## Task 2: 后端 - 托盘菜单 'settings' → 'home'

**Files:**
- Modify: `d:\project_code\ipv6\tray_app.py`
  - `create_tray` 方法（约第 960-962 行）
  - `_refresh_tray_menu` 方法（约第 1038-1040 行）

**Interfaces:**
- Consumes: Task 1 的 `get_network_detail`（无直接依赖，仅确保 `show_main_window` 能接收 `'home'` 参数）
- Produces: 托盘菜单"打开设置"项调用 `show_main_window('home')`

- [ ] **Step 1: 修改 `create_tray` 中的菜单项**

找到 `d:\project_code\ipv6\tray_app.py` 约第 962 行：

原代码：
```python
            pystray.MenuItem('打开设置', lambda i, item: self.show_main_window('settings')),
```

新代码：
```python
            pystray.MenuItem('打开主页', lambda i, item: self.show_main_window('home')),
```

- [ ] **Step 2: 修改 `_refresh_tray_menu` 中的菜单项**

找到 `d:\project_code\ipv6\tray_app.py` 约第 1040 行：

原代码：
```python
                pystray.MenuItem('打开设置', lambda i, item: self.show_main_window('settings')),
```

新代码：
```python
                pystray.MenuItem('打开主页', lambda i, item: self.show_main_window('home')),
```

- [ ] **Step 3: 验证修改**

运行：
```powershell
python -c "import tray_app; src = open('tray_app.py', encoding='utf-8').read(); assert \"show_main_window('home')\" in src, 'missing home'; assert \"show_main_window('settings')\" not in src, 'still has settings'; print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 4: 提交**

```powershell
git add tray_app.py
git commit -m "refactor: 托盘菜单 '打开设置' → '打开主页'，调用 show_main_window('home')"
```

---

## Task 3: 前端 - tab栏 4→3 + tab-home 容器创建

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`
  - tab 栏按钮（约第 1168-1171 行）
  - `tab-status` 容器（约第 1175-1211 行）
  - `tab-settings` 容器（约第 1214-1300 行）

**Interfaces:**
- Consumes: 无
- Produces: `id="tab-home"` 容器，内含状态区+（待 Task 4 填充的详情卡占位）+设置区

- [ ] **Step 1: 修改 tab 栏按钮为 3 个**

找到 `d:\project_code\ipv6\settings.html` 约第 1168-1171 行：

原代码：
```html
            <button class="tab-btn active" onclick="switchTab('status', event)">连接</button>
            <button class="tab-btn" onclick="switchTab('settings', event)">设置</button>
            <button class="tab-btn" onclick="switchTab('warp', event)">WARP排除</button>
            <button class="tab-btn" onclick="switchTab('traffic', event)">流量</button>
```

新代码：
```html
            <button class="tab-btn active" onclick="switchTab('home', event)">主页</button>
            <button class="tab-btn" onclick="switchTab('warp', event)">WARP排除</button>
            <button class="tab-btn" onclick="switchTab('traffic', event)">流量</button>
```

- [ ] **Step 2: 合并 tab-status 和 tab-settings 为 tab-home**

找到 `d:\project_code\ipv6\settings.html` 约第 1175-1300 行（从 `<div class="tab-content active" id="tab-status">` 到 `</div>` 结束 `tab-settings`）。

替换整个 `tab-status` 和 `tab-settings` 两个容器为单一的 `tab-home` 容器：

```html
        <div class="tab-content active" id="tab-home">
            <!-- 状态区（上）-->
            <div class="status-page">
                <canvas class="particle-canvas" id="particleCanvas" width="300" height="300"></canvas>

                <div class="status-indicator idle" id="statusIndicator">
                    <svg class="hexagon-svg" viewBox="0 0 140 140">
                        <polygon class="hexagon-shape" points="70,5 125,35 125,105 70,135 15,105 15,35"/>
                    </svg>
                    <svg class="hexagon-rotate" viewBox="0 0 156 156">
                        <polygon class="hexagon-rotate-line" points="78,5 141,39 141,117 78,151 15,117 15,39"/>
                    </svg>
                    <div class="status-icon" id="statusIcon">
                        <svg viewBox="0 0 24 24"><path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.56 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/></svg>
                    </div>
                </div>

                <div class="status-label" id="statusLabel">STATUS</div>
                <div class="status-title" id="statusTitle">一键认证</div>
                <div class="status-subtitle" id="statusSubtitle">点击下方按钮开始认证</div>

                <div class="progress-container" id="progressContainer">
                    <div class="progress-track">
                        <div class="progress-fill" id="progressFill" style="width: 0%"></div>
                    </div>
                    <div class="progress-info">
                        <div class="progress-label" id="progressLabelText">认证中</div>
                        <div class="progress-text" id="progressText">0%</div>
                    </div>
                </div>

                <div class="status-actions">
                    <button class="btn btn-primary" id="authBtn" onclick="startAuth()">开始认证</button>
                    <button class="btn btn-secondary" id="restoreBtn" onclick="startRestore()">恢复网络</button>
                    <button class="btn btn-danger" id="cancelBtn" onclick="cancelOperation()" style="display:none;">取消</button>
                </div>
            </div>

            <!-- 网络详情卡（中，可折叠）- Task 4 填充 -->
            <!-- NETWORK_DETAIL_PLACEHOLDER -->

            <!-- 设置区（下）-->
            <div class="settings-page">
                <div class="form-group">
                    <label>WiFi 网络</label>
                    <div class="dropdown">
                        <div class="input-row">
                            <input type="text" id="wifiName" placeholder="选择或输入WiFi名称" />
                            <button class="btn-icon" id="refreshBtn" onclick="refreshWifi()">扫描</button>
                        </div>
                        <div class="dropdown-list" id="wifiDropdown"></div>
                    </div>
                </div>

                <div class="form-group">
                    <label>认证账号</label>
                    <input type="text" id="username" placeholder="输入上网账号" />
                </div>

                <div class="form-group">
                    <label>认证密码</label>
                    <div class="input-row">
                        <input type="password" id="password" placeholder="输入账号密码" />
                        <button class="btn-icon" id="showPassBtn" onclick="togglePassword()">显示</button>
                    </div>
                </div>

                <div class="form-group">
                    <div style="display:flex;gap:8px;">
                        <div class="toggle-card" style="flex:1;">
                            <div class="toggle-card-header">
                                <span class="toggle-card-title">自动认证</span>
                                <label class="toggle">
                                    <input type="checkbox" id="autoAuth">
                                    <span class="toggle-slider"></span>
                                </label>
                            </div>
                            <div class="toggle-card-tip">WiFi后自动认证</div>
                        </div>
                        <div class="toggle-card" style="flex:1;">
                            <div class="toggle-card-header">
                                <span class="toggle-card-title">开机自启</span>
                                <label class="toggle">
                                    <input type="checkbox" id="autoStartup">
                                    <span class="toggle-slider"></span>
                                </label>
                            </div>
                            <div class="toggle-card-tip">登录后自动启动</div>
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <div style="display:flex;gap:8px;">
                        <div class="toggle-card" style="flex:1;">
                            <div class="toggle-card-header">
                                <span class="toggle-card-title">自动恢复</span>
                                <label class="toggle">
                                    <input type="checkbox" id="autoRestore">
                                    <span class="toggle-slider"></span>
                                </label>
                            </div>
                            <div class="toggle-card-tip">切换WiFi后自动恢复网络</div>
                        </div>
                        <div class="toggle-card" style="flex:1;">
                            <div class="toggle-card-header">
                                <span class="toggle-card-title">静默启动</span>
                                <label class="toggle">
                                    <input type="checkbox" id="silentStartup">
                                    <span class="toggle-slider"></span>
                                </label>
                            </div>
                            <div class="toggle-card-tip">开机自启时不显示主窗口</div>
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <label>WARP-CLI 路径</label>
                    <div class="input-row">
                        <input type="text" id="warpCliPath" placeholder="留空则自动检测" />
                        <button class="btn-icon" id="browseWarpBtn" onclick="browseWarpPath()">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        </button>
                    </div>
                </div>
            </div>
        </div>
```

**注意：** 上述代码中 `<!-- NETWORK_DETAIL_PLACEHOLDER -->` 是 Task 4 的占位符，Task 4 会替换它。

- [ ] **Step 3: 修改状态区 padding（紧凑化）**

找到 `d:\project_code\ipv6\settings.html` 约第 531 行的 `.status-page` CSS：

原代码：
```css
        .status-page {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            padding: 32px 24px 32px;
            position: relative;
        }
```

新代码：
```css
        .status-page {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            padding: 20px 24px 16px;
            position: relative;
        }
```

- [ ] **Step 4: 新增 tab-home 容器 CSS**

在 `.status-page` CSS 之后新增：

```css
        /* 主页tab容器 */
        #tab-home {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
        }
```

- [ ] **Step 5: 修改 settings-page 的 flex:1（避免抢占空间）**

找到 `d:\project_code\ipv6\settings.html` 约第 807 行的 `.settings-page` CSS：

原代码：
```css
        .settings-page {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 16px 20px;
            overflow: hidden;
        }
```

新代码：
```css
        .settings-page {
            display: flex;
            flex-direction: column;
            padding: 16px 20px;
            overflow: hidden;
        }
```

（移除 `flex: 1`，让设置区按内容高度自适应，不抢占状态区空间）

- [ ] **Step 6: 验证 HTML 结构**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); assert 'id=\"tab-home\"' in src, 'missing tab-home'; assert 'id=\"tab-status\"' not in src, 'still has tab-status'; assert 'id=\"tab-settings\"' not in src, 'still has tab-settings'; assert src.count('<script>') == src.count('</script>'); assert src.count('<style>') == src.count('</style>'); print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 7: 提交**

```powershell
git add settings.html
git commit -m "feat: 合并 tab-status+tab-settings 为 tab-home，tab栏 4→3"
```

---

## Task 4: 前端 - 网络详情卡 HTML + CSS

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`
  - 替换 Task 3 留下的 `<!-- NETWORK_DETAIL_PLACEHOLDER -->` 占位符
  - 在 `<style>` 内新增网络详情卡 CSS

**Interfaces:**
- Consumes: Task 3 的 `tab-home` 容器和占位符
- Produces: `#networkDetail`（可折叠卡片）、`#networkDetailGrid`（6项网格容器）

- [ ] **Step 1: 替换占位符为详情卡 HTML**

找到 `d:\project_code\ipv6\settings.html` 中的 `<!-- NETWORK_DETAIL_PLACEHOLDER -->`，替换为：

```html
            <!-- 网络详情卡（中，可折叠）-->
            <div class="network-detail" id="networkDetail">
                <div class="network-detail-header" onclick="HomeTab.toggleDetail()">
                    <span class="network-detail-title">网络详情</span>
                    <span class="network-detail-arrow" id="networkDetailArrow">▼</span>
                </div>
                <div class="network-detail-body" id="networkDetailBody">
                    <div class="network-detail-grid" id="networkDetailGrid">
                        <!-- JS 动态填充 6 项 -->
                    </div>
                </div>
            </div>
```

- [ ] **Step 2: 新增网络详情卡 CSS**

在 `d:\project_code\ipv6\settings.html` 的 `<style>` 标签内，找到 `/* ===== 流量tab ===== */` 注释之前，新增：

```css
        /* ===== 网络详情卡（主页tab）===== */
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
            font-family: 'Consolas', 'Monaco', monospace;
        }
        .network-detail-value.empty { color: var(--text-tertiary); }
        .network-detail-value.success { color: var(--success); }
        .network-detail-value.warning { color: var(--warning); }

        /* 响应式适配（窄窗口）*/
        @media (max-width: 600px) {
            .network-detail-grid { grid-template-columns: repeat(2, 1fr); }
        }
```

- [ ] **Step 3: 验证 HTML 和 CSS**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); assert 'id=\"networkDetail\"' in src, 'missing networkDetail'; assert 'id=\"networkDetailGrid\"' in src, 'missing grid'; assert 'HomeTab.toggleDetail()' in src, 'missing toggleDetail onclick'; assert '.network-detail {' in src, 'missing CSS'; assert '.network-detail.collapsed' in src, 'missing collapsed CSS'; print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 4: 提交**

```powershell
git add settings.html
git commit -m "feat: 新增网络详情卡 HTML+CSS（可折叠，3×2网格）"
```

---

## Task 5: 前端 - HomeTab 命名空间 + 删除 StatusTab/SettingsTab

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`
  - 删除 `const StatusTab = {...}`（约第 3953-3967 行）
  - 删除 `const SettingsTab = {...}`（约第 3970-3983 行）
  - 在 `Utils` 命名空间之后新增 `HomeTab` 命名空间

**Interfaces:**
- Consumes: Task 1 的 `ApiBridge.get_network_detail()`、`ApiBridge.save_ui_prefs()`；`Utils.escapeHtml()`、`Utils.showToast()`
- Produces: `HomeTab` 对象，含 `init()`、`initStatus()`、`initSettings()`、`refreshDetail()`、`renderDetailGrid(data)`、`toggleDetail()`、`setDetailCollapsed(collapsed)`、`_detailCollapsed`

- [ ] **Step 1: 找到 StatusTab 和 SettingsTab 的位置**

用 Grep 搜索：
```
const StatusTab = {|const SettingsTab = {
```

记录两者的行号范围（StatusTab 约 3953-3967，SettingsTab 约 3970-3983）。

- [ ] **Step 2: 删除 StatusTab 对象**

找到 `d:\project_code\ipv6\settings.html` 中的 StatusTab 对象：

原代码（约第 3953-3967 行）：
```javascript
        const StatusTab = {
            init() {
                setTimeout(async () => {
                    try {
                        const status = await window.pywebview.api.check_network_status();
                        updateStatusFromCheck(status);
                    } catch (e) { console.error('Failed to check network status:', e); }
                    try {
                        const startup = await window.pywebview.api.get_startup_status();
                        document.getElementById('autoStartup').checked = startup.enabled || false;
                    } catch (e) { console.error('Failed to get startup status:', e); }
                    document.getElementById('loadingOverlay').classList.add('hidden');
                }, 100);
            }
        };
```

删除整个 StatusTab 对象（包括前面的注释 `// ===== 连接tab =====` 如果有的话，但保留后面的空行）。

- [ ] **Step 3: 删除 SettingsTab 对象**

找到 `d:\project_code\ipv6\settings.html` 中的 SettingsTab 对象：

原代码（约第 3969-3983 行）：
```javascript
        // ===== 设置tab（包装原有全局初始化逻辑） =====
        const SettingsTab = {
            async init() {
                try {
                    const config = await window.pywebview.api.load_config();
                    document.getElementById('wifiName').value = config.wifi_name || '';
                    document.getElementById('username').value = config.username || '';
                    document.getElementById('password').value = config.password || '';
                    document.getElementById('autoAuth').checked = config.auto_auth || false;
                    document.getElementById('autoRestore').checked = config.auto_restore || false;
                    document.getElementById('warpCliPath').value = config.warp_cli_path || '';
                    document.getElementById('silentStartup').checked = config.silent_startup || false;
                } catch (e) { console.error('Failed to load config:', e); }
            }
        };
```

删除整个 SettingsTab 对象（包括前面的注释）。

- [ ] **Step 4: 新增 HomeTab 命名空间**

在 `Utils` 命名空间对象之后（即 `const WarpTab = {` 之前）插入：

```javascript
        // ===== 主页tab（合并原 StatusTab + SettingsTab + 网络详情）=====
        const HomeTab = {
            _detailCollapsed: false,

            // 状态区初始化（原 StatusTab.init 逻辑）
            initStatus() {
                setTimeout(async () => {
                    try {
                        const status = await window.pywebview.api.check_network_status();
                        updateStatusFromCheck(status);
                    } catch (e) { console.error('Failed to check network status:', e); }
                    try {
                        const startup = await window.pywebview.api.get_startup_status();
                        document.getElementById('autoStartup').checked = startup.enabled || false;
                    } catch (e) { console.error('Failed to get startup status:', e); }
                    const overlay = document.getElementById('loadingOverlay');
                    if (overlay) overlay.classList.add('hidden');
                }, 100);
            },

            // 设置区初始化（原 SettingsTab.init 逻辑）
            async initSettings() {
                try {
                    const config = await window.pywebview.api.load_config();
                    document.getElementById('wifiName').value = config.wifi_name || '';
                    document.getElementById('username').value = config.username || '';
                    document.getElementById('password').value = config.password || '';
                    document.getElementById('autoAuth').checked = config.auto_auth || false;
                    document.getElementById('autoRestore').checked = config.auto_restore || false;
                    document.getElementById('warpCliPath').value = config.warp_cli_path || '';
                    document.getElementById('silentStartup').checked = config.silent_startup || false;
                } catch (e) { console.error('Failed to load config:', e); }
            },

            // 网络详情区：拉取数据并渲染
            async refreshDetail() {
                if (!window.pywebview?.api) return;
                try {
                    const data = await window.pywebview.api.get_network_detail();
                    this.renderDetailGrid(data);
                } catch (e) {
                    console.error('HomeTab.refreshDetail failed:', e);
                    Utils.showToast('网络详情获取失败', 'error');
                }
            },

            // 渲染 6 项到详情网格
            renderDetailGrid(data) {
                const items = [
                    { label: 'IPv4', value: data.ipv4 || '—', class: data.ipv4 ? '' : 'empty' },
                    { label: 'IPv6', value: data.ipv6 || '无公网IPv6', class: data.ipv6 ? 'success' : 'warning' },
                    { label: 'MAC', value: data.mac || '—', class: data.mac ? '' : 'empty' },
                    { label: 'WiFi', value: data.wifi_ssid || '未连接', class: data.wifi_ssid ? '' : 'warning' },
                    { label: '接口', value: data.interface || '未知', class: data.interface ? '' : 'empty' },
                    { label: 'WARP', value: data.warp_connected ? '已连接' : '未连接', class: data.warp_connected ? 'success' : 'warning' },
                ];
                const grid = document.getElementById('networkDetailGrid');
                if (!grid) return;
                grid.innerHTML = items.map(it => `
                    <div class="network-detail-item">
                        <div class="network-detail-label">${it.label}</div>
                        <div class="network-detail-value ${it.class}">${Utils.escapeHtml(it.value)}</div>
                    </div>
                `).join('');
            },

            // 切换详情卡折叠/展开
            toggleDetail() {
                this._detailCollapsed = !this._detailCollapsed;
                this._applyCollapsed();
                // 持久化
                try {
                    window.pywebview.api.save_ui_prefs({ network_detail_collapsed: this._detailCollapsed });
                } catch (e) { console.warn('save_ui_prefs failed:', e); }
            },

            // 初始化时设置折叠状态（不触发持久化）
            setDetailCollapsed(collapsed) {
                this._detailCollapsed = !!collapsed;
                this._applyCollapsed();
            },

            // 应用折叠样式
            _applyCollapsed() {
                const el = document.getElementById('networkDetail');
                if (!el) return;
                if (this._detailCollapsed) {
                    el.classList.add('collapsed');
                } else {
                    el.classList.remove('collapsed');
                }
            },

            // 统一初始化入口
            async init() {
                await this.initSettings();   // 先加载设置表单
                this.initStatus();           // 再检查网络状态（异步，不阻塞）
                await this.refreshDetail();  // 最后加载网络详情
            }
        };
```

- [ ] **Step 5: 验证 HomeTab 定义和 StatusTab/SettingsTab 删除**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); assert 'const HomeTab = {' in src, 'missing HomeTab'; assert 'const StatusTab' not in src, 'StatusTab not deleted'; assert 'const SettingsTab' not in src, 'SettingsTab not deleted'; assert 'HomeTab.toggleDetail' in src, 'missing toggleDetail'; assert 'HomeTab.refreshDetail' in src, 'missing refreshDetail'; assert 'HomeTab.setDetailCollapsed' in src, 'missing setDetailCollapsed'; print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 6: 提交**

```powershell
git add settings.html
git commit -m "feat: 新增 HomeTab 命名空间，删除 StatusTab/SettingsTab"
```

---

## Task 6: 前端 - switchTab + initApp + finishAuth 整合

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`
  - `switchTab` 函数（约第 1622-1645 行）
  - `initApp` 函数（约第 3986-4012 行，Task 5 后行号会变化）
  - `finishAuth` 函数（约第 1723-1760 行）

**Interfaces:**
- Consumes: Task 5 的 `HomeTab` 对象
- Produces: 完整的主页tab生命周期管理

- [ ] **Step 1: 修改 switchTab 函数**

找到 `d:\project_code\ipv6\settings.html` 中的 `switchTab` 函数：

原代码：
```javascript
        function switchTab(name, event) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');
            if (event && event.target) {
                event.target.classList.add('active');
            } else {
                // 通过 evaluate_js 调用时无 event，按 name 激活按钮
                document.querySelector(`.tab-btn[onclick*="${name}"]`)?.classList.add('active');
            }

            // 流量tab特殊处理（TrafficTab 在 Task 7 中创建，此处用 typeof 保护）
            if (name === 'traffic') {
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.showSubview(TrafficTab.subview);
                    TrafficTab.startAutoRefresh();
                }
            } else {
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.stopAutoRefresh();
                    TrafficTab.pauseCanvas();
                }
            }
        }
```

新代码：
```javascript
        function switchTab(name, event) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');
            if (event && event.target) {
                event.target.classList.add('active');
            } else {
                // 通过 evaluate_js 调用时无 event，按 name 激活按钮
                document.querySelector(`.tab-btn[onclick*="${name}"]`)?.classList.add('active');
            }

            // 主页tab：切回时刷新网络详情
            if (name === 'home') {
                if (typeof HomeTab !== 'undefined' && HomeTab.refreshDetail) {
                    HomeTab.refreshDetail();
                }
            }

            // 流量tab特殊处理
            if (name === 'traffic') {
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.showSubview(TrafficTab.subview);
                    TrafficTab.startAutoRefresh();
                }
            } else {
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.stopAutoRefresh();
                    TrafficTab.pauseCanvas();
                }
            }
        }
```

- [ ] **Step 2: 修改 initApp 函数**

找到 `d:\project_code\ipv6\settings.html` 中的 `initApp` 函数。

原代码（Task 5 后行号会变化，用 Grep 定位）：
```javascript
        async function initApp() {
            try {
                await waitForApi(15000);
            } catch (e) {
                Utils.showToast('API 加载超时: ' + e.message);
                return;
            }
            // 加载 UI 偏好
            try {
                const prefs = await window.pywebview.api.get_ui_prefs();
                WarpTab.setPageSize(prefs.page_size || 20);
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.subview = prefs.traffic_subview || 'list';
                }
            } catch(e) {
                console.warn('get_ui_prefs failed:', e);
            }
            // 各tab初始化
            if (typeof StatusTab !== 'undefined' && StatusTab.init) StatusTab.init();
            if (typeof SettingsTab !== 'undefined' && SettingsTab.init) await SettingsTab.init();
            WarpTab.init();
            if (typeof TrafficTab !== 'undefined') {
                TrafficTab.init();
            }
            // 默认显示连接tab
            switchTab('status');
        }
```

新代码：
```javascript
        async function initApp() {
            try {
                await waitForApi(15000);
            } catch (e) {
                Utils.showToast('API 加载超时: ' + e.message);
                return;
            }
            // 加载 UI 偏好
            try {
                const prefs = await window.pywebview.api.get_ui_prefs();
                WarpTab.setPageSize(prefs.page_size || 20);
                if (typeof TrafficTab !== 'undefined') {
                    TrafficTab.subview = prefs.traffic_subview || 'list';
                }
                // 主页详情卡折叠状态
                if (typeof HomeTab !== 'undefined' && HomeTab.setDetailCollapsed) {
                    HomeTab.setDetailCollapsed(prefs.network_detail_collapsed || false);
                }
            } catch(e) {
                console.warn('get_ui_prefs failed:', e);
            }
            // 各tab初始化
            if (typeof HomeTab !== 'undefined' && HomeTab.init) await HomeTab.init();
            WarpTab.init();
            if (typeof TrafficTab !== 'undefined') {
                TrafficTab.init();
            }
            // 默认显示主页tab
            switchTab('home');
        }
```

- [ ] **Step 3: 在 finishAuth 完成时刷新网络详情**

找到 `d:\project_code\ipv6\settings.html` 中的 `finishAuth` 函数（用 Grep 搜索 `function finishAuth`）。

在 `finishAuth` 函数末尾（`}` 闭合大括号之前）新增刷新调用。原函数末尾大致是：

```javascript
            } else if (isCancelled) {
                setStatus('idle', '已取消', '操作已取消', 'wifi');
                showProgress(100, '已取消');
            } else {
                setStatus('error', action === 'restore' ? '恢复失败' : '认证失败', message, 'cross');
                showProgress(100, '失败');
            }

            setTimeout(() => {
                if (_authRunning) return;
                window.pywebview.api.check_network_status().then(s => {
                    if (!_authRunning) updateStatusFromCheck(s);
                }).catch(() => {
                    document.getElementById('authBtn').disabled = false;
                    document.getElementById('restoreBtn').disabled = false;
                });
            }, 1500);
        }
```

在最后的 `}` 之前（即 `}, 1500);` 之后、`}` 之前）新增：

```javascript

            // 认证/恢复完成后刷新网络详情卡（WARP/IPv4 状态可能变化）
            if (typeof HomeTab !== 'undefined' && HomeTab.refreshDetail) {
                setTimeout(() => HomeTab.refreshDetail(), 1800);
            }
```

完整修改后的函数末尾应为：
```javascript
            setTimeout(() => {
                if (_authRunning) return;
                window.pywebview.api.check_network_status().then(s => {
                    if (!_authRunning) updateStatusFromCheck(s);
                }).catch(() => {
                    document.getElementById('authBtn').disabled = false;
                    document.getElementById('restoreBtn').disabled = false;
                });
            }, 1500);

            // 认证/恢复完成后刷新网络详情卡（WARP/IPv4 状态可能变化）
            if (typeof HomeTab !== 'undefined' && HomeTab.refreshDetail) {
                setTimeout(() => HomeTab.refreshDetail(), 1800);
            }
        }
```

- [ ] **Step 4: 验证整合**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); assert \"switchTab('home')\" in src, 'missing switchTab home'; assert \"switchTab('status')\" not in src, 'still has switchTab status'; assert 'HomeTab.init()' in src, 'missing HomeTab.init in initApp'; assert 'HomeTab.setDetailCollapsed' in src, 'missing setDetailCollapsed in initApp'; assert 'HomeTab.refreshDetail' in src, 'missing refreshDetail'; assert 'StatusTab.init' not in src, 'still has StatusTab.init'; assert 'SettingsTab.init' not in src, 'still has SettingsTab.init'; print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 5: 提交**

```powershell
git add settings.html
git commit -m "feat: switchTab/initApp/finishAuth 整合 HomeTab 生命周期"
```

---

## Task 7: 验证与最终检查

**Files:**
- 无修改，仅验证

- [ ] **Step 1: 验证后端导入正常**

运行：
```powershell
python -c "import tray_app; from tray_app import ApiBridge; print('OK')"
```

预期输出：
```
OK
```

- [ ] **Step 2: 验证 ApiBridge 方法完整性**

运行：
```powershell
python -c "from tray_app import ApiBridge; methods = ['get_network_detail', 'save_ui_prefs', 'get_ui_prefs', 'check_network_status']; print('All methods present:', all(hasattr(ApiBridge, m) for m in methods))"
```

预期输出：
```
All methods present: True
```

- [ ] **Step 3: 验证 get_network_detail 返回结构**

运行：
```powershell
python -c "from tray_app import ApiBridge; ab = ApiBridge(); d = ab.get_network_detail(); expected = {'ipv4','ipv6','ipv6_status','mac','wifi_ssid','interface','warp_connected'}; print('keys match:', set(d.keys()) == expected); print('warp_connected is bool:', isinstance(d['warp_connected'], bool))"
```

预期输出：
```
keys match: True
warp_connected is bool: True
```

- [ ] **Step 4: 验证 HTML 标签平衡**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); print('script:', src.count('<script>'), '/', src.count('</script>')); print('style:', src.count('<style>'), '/', src.count('</style>')); print('div:', src.count('<div'), '/', src.count('</div>'))"
```

预期输出（div 数量可能变化，但 script/style 必须平衡）：
```
script: 1 / 1
style: 1 / 1
div: <N> / <N>
```

- [ ] **Step 5: 验证清理完成**

运行：
```powershell
python -c "src = open('settings.html', encoding='utf-8').read(); assert 'id=\"tab-status\"' not in src, 'tab-status still exists'; assert 'id=\"tab-settings\"' not in src, 'tab-settings still exists'; assert 'const StatusTab' not in src, 'StatusTab still exists'; assert 'const SettingsTab' not in src, 'SettingsTab still exists'; assert \"switchTab('status')\" not in src, 'switchTab status still exists'; assert \"switchTab('settings')\" not in src, 'switchTab settings still exists'; print('Cleanup OK')"
```

预期输出：
```
Cleanup OK
```

- [ ] **Step 6: 验证托盘菜单**

运行：
```powershell
python -c "src = open('tray_app.py', encoding='utf-8').read(); assert \"show_main_window('home')\" in src, 'missing home'; assert \"show_main_window('settings')\" not in src, 'still has settings'; print('Tray menu OK')"
```

预期输出：
```
Tray menu OK
```

- [ ] **Step 7: 验证 CampusAuth.spec 语法**

运行：
```powershell
python -c "import ast; ast.parse(open('CampusAuth.spec', encoding='utf-8').read()); print('spec OK')"
```

预期输出：
```
spec OK
```

- [ ] **Step 8: 提交验证记录（如有未提交的改动）**

运行：
```powershell
git status
```

如果显示 "nothing to commit, working tree clean"，则无需提交。否则提交剩余改动。

---

## Self-Review

### 1. Spec 覆盖检查

| Spec 要求 | 对应 Task |
|-----------|----------|
| 合并连接+设置为主页tab | Task 3（HTML合并）+ Task 5（命名空间合并） |
| 上下分区布局 | Task 3（status-page + settings-page 上下排列） |
| 新增网络详情卡 | Task 4（HTML+CSS）+ Task 5（HomeTab.refreshDetail/renderDetailGrid） |
| 详情卡可折叠 | Task 4（CSS动画）+ Task 5（toggleDetail/_applyCollapsed） |
| 折叠状态持久化 | Task 1（ui_prefs 扩展）+ Task 5（toggleDetail 持久化）+ Task 6（initApp 读取） |
| Tab栏 4→3 | Task 3（Step 1） |
| Tab命名"主页" | Task 3（Step 1） |
| 后端 get_network_detail 聚合接口 | Task 1（Step 3） |
| 切回主页刷新详情 | Task 6（switchTab 'home' 分支） |
| 认证完成后刷新详情 | Task 6（finishAuth 整合） |
| 托盘菜单 'settings'→'home' | Task 2 |
| 删除 StatusTab/SettingsTab | Task 5（Step 2-3） |
| 响应式窄窗口2列 | Task 4（CSS @media） |

**无遗漏**。

### 2. Placeholder 扫描

- 所有代码块完整，无 TBD/TODO
- 所有步骤有具体代码或命令
- 函数签名和返回值明确
- Task 3 中的 `<!-- NETWORK_DETAIL_PLACEHOLDER -->` 是有意的占位符，Task 4 明确替换它

### 3. 类型一致性

- `get_network_detail() -> dict` 在 Task 1 定义，Task 5 的 `HomeTab.refreshDetail()` 调用并使用返回字段（ipv4/ipv6/mac/wifi_ssid/interface/warp_connected）✓
- `save_ui_prefs({'network_detail_collapsed': bool})` 在 Task 1 扩展，Task 5 的 `toggleDetail()` 调用 ✓
- `get_ui_prefs()` 返回 `network_detail_collapsed: bool` 在 Task 1 扩展，Task 6 的 `initApp` 读取并调用 `HomeTab.setDetailCollapsed(bool)` ✓
- `HomeTab.setDetailCollapsed(collapsed)` 在 Task 5 定义为接受 bool，Task 6 调用传入 `prefs.network_detail_collapsed || false` ✓
- `HomeTab.refreshDetail()` 在 Task 5 定义，Task 6 的 `switchTab` 和 `finishAuth` 调用 ✓

**无类型不一致**。
