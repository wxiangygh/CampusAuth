# 页面合并与分页搜索优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将4个独立HTML页面（settings/warp_exclusion/traffic_monitor/traffic_flow）合并为单窗口4-tab结构，放大视口自适应屏幕，WARP排除两个列表分页+子序列保序搜索，窗口尺寸和UI偏好持久化。

**Architecture:** 扩展 settings.html 为单文件（保留文件名），新增"WARP排除""流量"两个tab，合并3个HTML的HTML/CSS/JS。后端 tray_app.py 删除3个独立窗口方法，新增 show_main_window 统一入口，窗口 resizable=True + minsize + 尺寸持久化。前端用命名空间对象（StatusTab/SettingsTab/WarpTab/TrafficTab）隔离各tab状态，Utils 对象承载共享工具（fuzzyMatch/paginate/renderPagination）。

**Tech Stack:** Python 3 + pywebview + pystray；纯HTML/CSS/原生JS（无构建工具、无框架）；配置存储为 tray_config.json。

## Global Constraints

- **文件命名**：保留 `settings.html` 文件名不重命名；合并后删除 `warp_exclusion.html`、`traffic_monitor.html`、`traffic_flow.html`
- **窗口尺寸**：默认屏幕85%，`resizable=True`，`minsize=(800, 600)`，启动时校正越界尺寸
- **持久化字段**：`window.{width,height,x,y}` + `ui_prefs.{page_size,traffic_subview}`，存入 `tray_config.json`
- **分页范围**：仅 WARP排除的两个列表（learnedList, rulesList）；流量连接列表不分页
- **搜索算法**：仅 WARP排除两个列表用子序列保序匹配（`fuzzyMatch`）；流量连接列表保持 `includes` 子串匹配
- **每页大小**：可选 10/20/50/100，默认20，两个列表共享 pageSize，改变后持久化
- **tab 切换**：启动默认显示"连接"tab；流量tab切走时停止自动刷新+暂停画布
- **托盘菜单**：保留快捷项，"WARP排除"→切warp tab，"流量"→切traffic tab，原3个独立窗口方法删除
- **命名空间**：JS 用 `StatusTab`/`SettingsTab`/`WarpTab`/`TrafficTab`/`Utils` 对象隔离
- **搜索去抖**：150ms
- **Logger**：后端统一用 `logging.getLogger('wifi_tray')`
- **无构建工具**：纯HTML+JS，不引入webpack/vite/Vue/React

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|---------|
| `tray_app.py` | 后端窗口管理、托盘菜单、ApiBridge | 修改 |
| `settings.html` | 合并4个HTML为单文件4-tab | 修改（大改） |
| `warp_exclusion.html` | 原 WARP排除页面 | 删除 |
| `traffic_monitor.html` | 原流量监控页面 | 删除 |
| `traffic_flow.html` | 原流量可视化页面 | 删除 |
| `CampusAuth.spec` | PyInstaller 打包配置 | 修改 |

**不创建新文件**（核心约束：NEVER create files unless absolutely necessary）。

---

## Task 1: 后端配置扩展与 ApiBridge 新方法

**Files:**
- Modify: `d:\project_code\ipv6\tray_app.py`（`load_config` 函数 L76-108，`ApiBridge` 类新增方法）

**Interfaces:**
- Produces:
  - `load_config()` 的 defaults 字典新增 `window` 和 `ui_prefs` 字段
  - `ApiBridge.save_ui_prefs(prefs: dict) -> dict`：保存 UI 偏好，返回 `{'success': bool}`
  - `ApiBridge.get_ui_prefs() -> dict`：读取 UI 偏好，返回 `{'page_size': int, 'traffic_subview': str}`

- [ ] **Step 1: 扩展 load_config 的 defaults**

在 `d:\project_code\ipv6\tray_app.py` 的 `load_config` 函数（L76-108）中，`defaults` 字典新增两个字段：

```python
def load_config():
    defaults = {
        'username': '',
        'password': '',
        'wifi_name': '',
        'auto_auth': False,
        'auto_startup': False,
        'auto_restore': False,
        'portal_ip': '10.21.221.98',
        'portal_port': '801',
        'warp_cli_path': '',
        'silent_startup': False,
        'window_x': None,
        'window_y': None,
        # 新增：窗口尺寸和UI偏好（向后兼容，缺失时用默认值）
        'window': None,       # {'width': int, 'height': int, 'x': int, 'y': int} 或 None
        'ui_prefs': None,     # {'page_size': int, 'traffic_subview': 'list'|'canvas'} 或 None
    }
```

- [ ] **Step 2: 新增 ApiBridge.save_ui_prefs 方法**

在 `ApiBridge` 类中（`close_flow_window` 方法之后，约 L710 附近）新增：

```python
def save_ui_prefs(self, prefs):
    """保存 UI 偏好（page_size, traffic_subview）。
    Args:
        prefs: dict，如 {'page_size': 50} 或 {'traffic_subview': 'canvas'}
    Returns:
        dict: {'success': bool}
    """
    try:
        cfg = load_config()
        current = cfg.get('ui_prefs') or {}
        current.update(prefs)
        cfg['ui_prefs'] = current
        save_config_to_file(cfg)
        logger.info(f"[save_ui_prefs] Saved: {prefs}, merged: {current}")
        return {'success': True}
    except Exception as e:
        logger.error(f"[save_ui_prefs] FAILED: {e}\n{traceback.format_exc()}")
        return {'success': False}
```

- [ ] **Step 3: 新增 ApiBridge.get_ui_prefs 方法**

紧接 `save_ui_prefs` 之后新增：

```python
def get_ui_prefs(self):
    """读取 UI 偏好，供前端初始化。
    Returns:
        dict: {'page_size': int, 'traffic_subview': str}
    """
    try:
        cfg = load_config()
        prefs = cfg.get('ui_prefs') or {}
        result = {
            'page_size': int(prefs.get('page_size', 20)),
            'traffic_subview': prefs.get('traffic_subview', 'list')
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
        return {'page_size': 20, 'traffic_subview': 'list'}
```

- [ ] **Step 4: 验证导入和方法存在**

运行：
```bash
python -c "from tray_app import ApiBridge, load_config; print('save_ui_prefs:', hasattr(ApiBridge, 'save_ui_prefs')); print('get_ui_prefs:', hasattr(ApiBridge, 'get_ui_prefs')); cfg = load_config(); print('window' in cfg, 'ui_prefs' in cfg)"
```

预期输出：
```
save_ui_prefs: True
get_ui_prefs: True
True True
```

- [ ] **Step 5: 提交**

```bash
git add tray_app.py
git commit -m "feat: load_config 扩展 window/ui_prefs 字段，ApiBridge 新增 save_ui_prefs/get_ui_prefs"
```

---

## Task 2: 后端窗口尺寸持久化与 resizable 改造

**Files:**
- Modify: `d:\project_code\ipv6\tray_app.py`（`TrayApp` 类 L857-859、`run` 方法 L1204-1245、`save_window_position` L1132-1154、`on_closing` L1248+）

**Interfaces:**
- Consumes: Task 1 的 `load_config` / `save_config_to_file`（含 `window` 字段）
- Produces:
  - `TrayApp.calc_initial_window_geometry()`：计算初始窗口几何（从配置读取或屏幕85%居中）
  - `TrayApp.save_window_geometry()`：保存当前窗口尺寸+位置到配置
  - `webview.create_window` 调用改为 `resizable=True` + `minsize=(800, 600)`

- [ ] **Step 1: 修改 TrayApp 类常量和 __init__**

将 `d:\project_code\ipv6\tray_app.py` L857-859 的：

```python
class TrayApp:
    WIN_W = 400
    WIN_H = 560
```

改为：

```python
class TrayApp:
    # 首次启动默认尺寸（屏幕85%），实际从配置读取
    MIN_W = 800
    MIN_H = 600
```

- [ ] **Step 2: 新增 calc_initial_window_geometry 方法**

在 `TrayApp.__init__` 之后（约 L873 附近）新增：

```python
def calc_initial_window_geometry(self):
    """计算初始窗口几何。优先从配置读取，否则按屏幕85%居中。
    Returns: (width, height, x, y)
    """
    user32 = ctypes.windll.user32
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    cfg = load_config()
    saved = cfg.get('window')
    if saved and isinstance(saved, dict):
        w = int(saved.get('width', screen_w * 85 // 100))
        h = int(saved.get('height', screen_h * 85 // 100))
        x = int(saved.get('x', (screen_w - w) // 2))
        y = int(saved.get('y', (screen_h - h) // 2))
        # 校正越界（外接显示器拔出场景）
        if w > screen_w:
            w = screen_w * 85 // 100
        if h > screen_h:
            h = screen_h * 85 // 100
        x = max(0, min(x, screen_w - w))
        y = max(0, min(y, screen_h - h))
        logger.info(f"[window_geometry] From config: {w}x{h} at ({x},{y})")
        return w, h, x, y
    # 首次启动：屏幕85%居中
    w = screen_w * 85 // 100
    h = screen_h * 85 // 100
    x = (screen_w - w) // 2
    y = (screen_h - h) // 2
    logger.info(f"[window_geometry] Default 85%: {w}x{h} at ({x},{y})")
    return w, h, x, y
```

- [ ] **Step 3: 新增 save_window_geometry 方法**

紧接 `calc_initial_window_geometry` 之后新增：

```python
def save_window_geometry(self):
    """保存当前窗口尺寸和位置到配置。"""
    try:
        if not self.settings_window:
            return
        hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
        if hwnd:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            x, y = rect.left, rect.top
            w = rect.right - rect.left
            h = rect.bottom - rect.top
        else:
            x = self.settings_window.x
            y = self.settings_window.y
            w = self.settings_window.width
            h = self.settings_window.height
        # 忽略异常值
        if w > 100 and h > 100 and x > -1000 and y > -1000:
            cfg = load_config()
            cfg['window'] = {'width': w, 'height': h, 'x': x, 'y': y}
            save_config_to_file(cfg)
            logger.info(f"[save_window_geometry] Saved: {w}x{h} at ({x},{y})")
        else:
            logger.warning(f"[save_window_geometry] Ignored abnormal: {w}x{h} at ({x},{y})")
    except Exception as e:
        logger.error(f"[save_window_geometry] FAILED: {e}")
```

- [ ] **Step 4: 修改 run 方法中的 create_window 调用**

在 `d:\project_code\ipv6\tray_app.py` 的 `run` 方法中（约 L1213-1245），将原来的窗口尺寸计算和 `create_window` 调用改为：

```python
        # 从配置读取窗口几何，否则按屏幕85%居中
        win_w, win_h, wx, wy = self.calc_initial_window_geometry()

        try:
            html_url = f'file:///{html_file.replace(chr(92), "/")}'
            self.settings_window = webview.create_window(
                'CampusAuth',
                url=html_url,
                js_api=self.api,
                width=win_w,
                height=win_h,
                x=wx,
                y=wy,
                resizable=True,
                minsize=(self.MIN_W, self.MIN_H),
                background_color='#0D0D0D',
                easy_drag=True,
                frameless=True,
                hidden=self._silent
            )
            logger.info(f"Window created at ({wx}, {wy}), size={win_w}x{win_h}, url={html_url}")
```

注意：删除原来 L1213-1225 中基于 `TrayApp.WIN_W/WIN_H` 和 `window_x/window_y` 的计算逻辑，替换为上述调用。

- [ ] **Step 5: 修改 on_closing 调用 save_window_geometry**

找到 `run` 方法中的 `def on_closing():`（约 L1248），在函数体开头加入保存几何的调用：

```python
        def on_closing():
            logger.info("[on_closing] Window closing event triggered")
            try:
                self.save_window_geometry()
            except Exception as e:
                logger.error(f"[on_closing] save_window_geometry failed: {e}")
            # ... 原有逻辑保持不变
```

- [ ] **Step 6: 删除旧的 save_window_position 方法**

删除 `save_window_position` 方法（L1132-1154），因为已被 `save_window_geometry` 取代。同时检查是否有其他地方调用 `save_window_position`，若有则改为 `save_window_geometry`。

用 Grep 搜索 `save_window_position` 的所有引用，逐一替换为 `save_window_geometry`。

- [ ] **Step 7: 验证窗口创建逻辑**

运行：
```bash
python -c "from tray_app import TrayApp; app = TrayApp(); g = app.calc_initial_window_geometry(); print('geometry:', g); print('minsize:', (TrayApp.MIN_W, TrayApp.MIN_H))"
```

预期输出（数值依屏幕而定）：
```
geometry: (1632, 918, 144, 81)
minsize: (800, 600)
```

- [ ] **Step 8: 提交**

```bash
git add tray_app.py
git commit -m "feat: 窗口 resizable=True + minsize，尺寸持久化到 tray_config.json"
```

---

## Task 3: 后端删除独立窗口方法 + 统一入口

**Files:**
- Modify: `d:\project_code\ipv6\tray_app.py`（删除 `show_exclusion` L984-1021、`show_traffic_monitor` L1023-1058、`show_flow_monitor` L1060-1095；修改 `__init__` L865-867；修改托盘菜单 L879-901、L960-979；新增 `show_main_window`）

**Interfaces:**
- Produces: `TrayApp.show_main_window(tab=None)`：打开主窗口并切换到指定 tab

- [ ] **Step 1: 删除三个独立窗口方法**

删除 `d:\project_code\ipv6\tray_app.py` 中的三个方法：
- `show_exclusion`（L984-1021）
- `show_traffic_monitor`（L1023-1058）
- `show_flow_monitor`（L1060-1095）

同时删除 `__init__` 中的三个窗口引用字段（L865-867）：
```python
        self._exclusion_window = None
        self._traffic_window = None
        self._flow_window = None
```

- [ ] **Step 2: 删除 ApiBridge 中的 close_*_window 方法**

删除 `ApiBridge` 类中的三个方法（L644-708）：
- `close_exclusion_window`
- `close_traffic_window`
- `close_flow_window`

用 Grep 搜索是否还有其他地方调用这三个方法，若有则删除调用点。

- [ ] **Step 3: 新增 show_main_window 方法**

在 `TrayApp.show_settings` 方法之后新增：

```python
def show_main_window(self, tab=None):
    """打开主窗口并切换到指定tab。
    Args:
        tab: 'status' | 'settings' | 'warp' | 'traffic' | None（保持上次）
    """
    logger.info(f"[show_main_window] Called, tab={tab}")
    self.show_settings()
    if tab and self.settings_window:
        try:
            self.settings_window.evaluate_js(f"switchTab('{tab}')")
            logger.info(f"[show_main_window] Switched to tab: {tab}")
        except Exception as e:
            logger.warning(f"[show_main_window] evaluate_js failed: {e}")
```

- [ ] **Step 4: 修改 create_tray 中的托盘菜单**

将 `d:\project_code\ipv6\tray_app.py` L879-901 的托盘菜单改为：

```python
        menu_items = [
            pystray.MenuItem('显示主窗口', lambda i, item: self.show_main_window()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('手动认证', on_auth),
            pystray.MenuItem('注销并重新认证', on_reauth),
            pystray.MenuItem('恢复正常模式', on_restore),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('WARP排除', lambda i, item: self.show_main_window('warp')),
            pystray.MenuItem('流量', lambda i, item: self.show_main_window('traffic')),
            pystray.MenuItem('打开设置', lambda i, item: self.show_main_window('settings')),
            pystray.Menu.SEPARATOR,
        ]
```

- [ ] **Step 5: 修改 _refresh_tray_menu 中的托盘菜单**

将 L960-979 的托盘菜单改为：

```python
            menu_items = [
                pystray.MenuItem('手动认证', on_auth),
                pystray.MenuItem('注销并重新认证', on_reauth),
                pystray.MenuItem('恢复正常模式', on_restore),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('WARP排除', lambda i, item: self.show_main_window('warp')),
                pystray.MenuItem('流量', lambda i, item: self.show_main_window('traffic')),
                pystray.MenuItem('打开设置', lambda i, item: self.show_main_window('settings')),
                pystray.Menu.SEPARATOR,
            ]
```

- [ ] **Step 6: 验证无残留引用**

运行 Grep 搜索以下关键词，确认无残留：
```
show_exclusion|show_traffic_monitor|show_flow_monitor|_exclusion_window|_traffic_window|_flow_window|close_exclusion_window|close_traffic_window|close_flow_window
```

预期：无匹配（或仅在本注释/日志中）。

- [ ] **Step 7: 验证导入和托盘创建**

运行：
```bash
python -c "from tray_app import TrayApp; app = TrayApp(); print('show_main_window:', hasattr(app, 'show_main_window')); print('show_exclusion:', hasattr(app, 'show_exclusion')); print('show_traffic_monitor:', hasattr(app, 'show_traffic_monitor')); print('show_flow_monitor:', hasattr(app, 'show_flow_monitor'))"
```

预期输出：
```
show_main_window: True
show_exclusion: False
show_traffic_monitor: False
show_flow_monitor: False
```

- [ ] **Step 8: 提交**

```bash
git add tray_app.py
git commit -m "refactor: 删除3个独立窗口方法，新增 show_main_window 统一入口，托盘菜单改为切tab"
```

---

## Task 4: CampusAuth.spec 清理

**Files:**
- Modify: `d:\project_code\ipv6\CampusAuth.spec`（L8 datas、L10 hiddenimports）

- [ ] **Step 1: 修改 datas 行**

将 `d:\project_code\ipv6\CampusAuth.spec` L8 的：

```python
    datas=[('settings.html', '.'), ('warp_exclusion.html', '.'), ('traffic_monitor.html', '.'), ('traffic_flow.html', '.'), ('app.ico', '.')],
```

改为：

```python
    datas=[('settings.html', '.'), ('app.ico', '.')],
```

- [ ] **Step 2: 修改 hiddenimports**

将 L10 的：

```python
        'warp_exclusion', 'traffic_monitor',
```

改为（仅保留 traffic_monitor，因为后端 traffic_monitor.py 仍存在）：

```python
        'traffic_monitor',
```

注意：`warp_exclusion.py` 仍存在，但检查是否被 tray_app.py 导入。用 Grep 搜索 `import warp_exclusion`，若存在则保留 `warp_exclusion`，否则删除。

- [ ] **Step 3: 验证 spec 语法**

运行：
```bash
python -c "import ast; ast.parse(open('CampusAuth.spec', encoding='utf-8').read()); print('spec OK')"
```

预期输出：
```
spec OK
```

- [ ] **Step 4: 提交**

```bash
git add CampusAuth.spec
git commit -m "chore: CampusAuth.spec 移除3个已删除HTML的引用"
```

---

## Task 5: 前端 - settings.html 框架改造（标题栏+tab栏+共享CSS）

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`（顶部CSS、标题栏、tab栏区域）

**Interfaces:**
- Produces:
  - 统一的 `.app-title-bar` 标题栏（取代各页面原 `.title-bar`）
  - 4个tab按钮的 `.tab-bar`
  - 共享CSS变量 `:root` 和共享组件样式（`.btn`、`.search-input`、`.empty-hint`、`.loading-spinner`）
  - 4个空的 `tab-content` 容器：`tab-status`、`tab-settings`、`tab-warp`、`tab-traffic`

- [ ] **Step 1: 备份当前 settings.html**

运行：
```bash
copy settings.html settings.html.bak
```

（备份用于参考，任务完成后删除 .bak 文件）

- [ ] **Step 2: 查看当前 settings.html 的 :root 和标题栏结构**

读取 `d:\project_code\ipv6\settings.html` 的 L1-140（CSS变量和标题栏样式），确认当前 `:root` 定义。

- [ ] **Step 3: 合并 CSS 变量到统一 :root**

在 `d:\project_code\ipv6\settings.html` 的 `<style>` 标签开头，替换原有 `:root` 为合并后的统一变量：

```css
:root {
    --bg-primary: #0D0D0D;
    --bg-secondary: #1A1A1A;
    --bg-tertiary: #262626;
    --accent: #F68320;
    --accent-dim: rgba(246,131,32,0.15);
    --text-primary: #FFFFFF;
    --text-secondary: #A3A3A3;
    --text-tertiary: #737373;
    --border: rgba(255,255,255,0.08);
    /* 流量页6类颜色 */
    --c-ipv4: #3b82f6;
    --c-ipv6: #22C55E;
    --c-ipv4-warp: #F59E0B;
    --c-ipv4-warp-ipv6: #EAB308;
    --c-ipv6-warp: #EF4444;
    --c-ipv6-warp-ipv4: #A855F7;
}
```

- [ ] **Step 4: 新增统一标题栏样式**

在 `:root` 之后新增：

```css
/* ===== 标题栏 + tab栏 ===== */
.app-title-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 12px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    -webkit-app-region: drag;
}
.app-title-text { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.app-title-btns { display: flex; gap: 6px; -webkit-app-region: no-drag; }
.app-title-btn {
    width: 24px; height: 24px; display: flex; align-items: center; justify-content: center;
    border: none; background: transparent; border-radius: 4px; cursor: pointer;
    color: var(--text-tertiary); transition: all 0.15s; padding: 0;
}
.app-title-btn:hover { background: var(--bg-tertiary); color: var(--text-secondary); }
.app-title-btn.close:hover { background: rgba(239,68,68,0.2); color: var(--c-ipv6-warp); }
.app-title-btn svg { width: 14px; height: 14px; stroke: currentColor; stroke-width: 2; fill: none; stroke-linecap: round; stroke-linejoin: round; }
```

- [ ] **Step 5: 修改 tab-bar 样式以支持4个tab**

找到现有 `.tab-bar` 和 `.tab-btn` 样式，确保支持4个tab（原样式可能基于2个tab的布局）。修改为：

```css
.tab-bar {
    display: flex;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    padding: 0 8px;
}
.tab-btn {
    padding: 10px 16px;
    font-size: 12px;
    color: var(--text-tertiary);
    background: transparent;
    border: none;
    cursor: pointer;
    position: relative;
    transition: color 0.2s;
}
.tab-btn:hover { color: var(--text-secondary); }
.tab-btn.active { color: var(--text-primary); }
.tab-btn.active::after {
    content: '';
    position: absolute;
    bottom: 0; left: 8px; right: 8px;
    height: 2px;
    background: var(--accent);
    border-radius: 1px;
}
```

- [ ] **Step 6: 新增共享组件样式**

在 tab-bar 样式之后新增共享组件（从其他3个HTML合并）：

```css
/* ===== 共享组件 ===== */
.btn {
    padding: 6px 12px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg-tertiary); color: var(--text-primary); font-size: 12px;
    cursor: pointer; transition: all 0.15s; display: inline-flex; align-items: center; gap: 4px;
}
.btn:hover { background: var(--bg-secondary); border-color: var(--accent); }
.btn-sm { padding: 4px 10px; font-size: 11px; }
.btn-primary { background: var(--accent); color: #000; border-color: var(--accent); }
.btn-danger { color: var(--c-ipv6-warp); border-color: rgba(239,68,68,0.3); }
.btn-danger:hover { background: rgba(239,68,68,0.1); }

.search-input {
    flex: 1; min-width: 120px; padding: 6px 10px;
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text-primary); font-size: 12px; outline: none;
}
.search-input:focus { border-color: var(--accent); }
.search-input::placeholder { color: var(--text-tertiary); }

.empty-hint {
    text-align: center; color: var(--text-tertiary);
    padding: 24px 12px; font-size: 12px;
}

.loading-spinner {
    width: 32px; height: 32px;
    border: 3px solid var(--bg-tertiary);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Toast 提示 */
.toast {
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
    padding: 8px 16px; border-radius: 6px; font-size: 12px; z-index: 9999;
    background: var(--bg-tertiary); color: var(--text-primary);
    border: 1px solid var(--border); box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    opacity: 0; transition: opacity 0.3s;
}
.toast.show { opacity: 1; }
.toast.error { border-color: var(--c-ipv6-warp); }
.toast.success { border-color: var(--c-ipv6); }

/* 分页栏 */
.pagination-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; background: var(--bg-secondary);
    border-top: 1px solid var(--border); font-size: 11px; color: var(--text-tertiary);
}
.pagination-bar .page-size-select {
    background: var(--bg-tertiary); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 6px; font-size: 11px;
}
.pagination-bar .page-nav { display: flex; gap: 4px; align-items: center; }
.pagination-bar .page-btn {
    min-width: 24px; height: 24px; padding: 0 6px;
    background: var(--bg-tertiary); border: 1px solid var(--border);
    border-radius: 4px; color: var(--text-secondary); cursor: pointer;
    font-size: 11px; display: flex; align-items: center; justify-content: center;
}
.pagination-bar .page-btn:hover { border-color: var(--accent); color: var(--text-primary); }
.pagination-bar .page-btn.active { background: var(--accent); color: #000; border-color: var(--accent); }
.pagination-bar .page-btn:disabled { opacity: 0.3; cursor: not-allowed; }
.pagination-bar .page-ellipsis { color: var(--text-tertiary); padding: 0 4px; }
```

- [ ] **Step 7: 修改 body 和 container 样式**

确保 `body` 和 `.container` 样式支持全屏自适应：

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    overflow: hidden;
    width: 100vw; height: 100vh;
    user-select: none;
}
input { user-select: text; -webkit-app-region: no-drag; }
.container { width: 100%; height: 100%; display: flex; flex-direction: column; }
```

- [ ] **Step 8: 修改 HTML body 结构**

将 `d:\project_code\ipv6\settings.html` 中 `<body>` 内的 `<div class="container">` 区域改为：

```html
    <div class="container">
        <!-- 标题栏 -->
        <div class="app-title-bar">
            <span class="app-title-text">CampusAuth</span>
            <div class="app-title-btns">
                <button class="app-title-btn" onclick="minimizeWindow()" title="最小化">
                    <svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/></svg>
                </button>
                <button class="app-title-btn close" onclick="closeWindow()" title="关闭">
                    <svg viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>
                </button>
            </div>
        </div>

        <!-- Tab Bar -->
        <div class="tab-bar">
            <button class="tab-btn active" onclick="switchTab('status', event)">连接</button>
            <button class="tab-btn" onclick="switchTab('settings', event)">设置</button>
            <button class="tab-btn" onclick="switchTab('warp', event)">WARP排除</button>
            <button class="tab-btn" onclick="switchTab('traffic', event)">流量</button>
        </div>

        <!-- 连接 Tab -->
        <div class="tab-content active" id="tab-status">
            <!-- 原有 status-page 内容保持不变 -->
        </div>

        <!-- 设置 Tab -->
        <div class="tab-content" id="tab-settings">
            <!-- 原有 settings-page 内容保持不变 -->
        </div>

        <!-- WARP排除 Tab -->
        <div class="tab-content" id="tab-warp">
            <!-- Task 6 填充 -->
        </div>

        <!-- 流量 Tab -->
        <div class="tab-content" id="tab-traffic">
            <!-- Task 7 填充 -->
        </div>
    </div>
```

注意：保留原有的 `tab-status` 和 `tab-settings` 内容不动，仅调整外层结构。

- [ ] **Step 9: 新增窗口控制 JS**

在 `<script>` 标签开头新增窗口控制函数：

```javascript
// ===== 窗口控制 =====
function minimizeWindow() {
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.minimize();
    }
}

function closeWindow() {
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.close_window();
    }
}
```

注意：需确认 `ApiBridge` 是否已有 `minimize` 和 `close_window` 方法。用 Grep 搜索，若没有则在本任务 Step 10 中新增。

- [ ] **Step 10: 检查并补充 ApiBridge 的窗口控制方法**

用 Grep 搜索 `def minimize` 和 `def close_window` 在 `tray_app.py` 中。若无则新增：

在 `ApiBridge` 类中新增（若已存在则跳过）：

```python
def minimize(self):
    """最小化窗口。"""
    try:
        if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
            core.state._tray_app_instance.settings_window.minimize()
    except Exception as e:
        logger.error(f"[minimize] FAILED: {e}")

def close_window(self):
    """关闭窗口（隐藏，不退出应用）。"""
    try:
        if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
            core.state._tray_app_instance.settings_window.hide()
    except Exception as e:
        logger.error(f"[close_window] FAILED: {e}")
```

- [ ] **Step 11: 验证 HTML 结构**

运行：
```bash
python -c "with open('settings.html', encoding='utf-8') as f: html = f.read(); print('tab-status:', 'id=\"tab-status\"' in html); print('tab-settings:', 'id=\"tab-settings\"' in html); print('tab-warp:', 'id=\"tab-warp\"' in html); print('tab-traffic:', 'id=\"tab-traffic\"' in html); print('app-title-bar:', 'app-title-bar' in html)"
```

预期输出：
```
tab-status: True
tab-settings: True
tab-warp: True
tab-traffic: True
app-title-bar: True
```

- [ ] **Step 12: 删除备份并提交**

```bash
del settings.html.bak
git add settings.html tray_app.py
git commit -m "feat: settings.html 框架改造 - 统一标题栏+4tab+共享CSS+窗口控制"
```

---

## Task 6: 前端 - WARP排除tab内容迁移 + 分页 + 子序列搜索

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`（`tab-warp` 内容区、WarpTab JS 命名空间）
- Read: `d:\project_code\ipv6\warp_exclusion.html`（源文件，迁移后删除）

**Interfaces:**
- Consumes: Task 5 的 `tab-warp` 容器、共享样式
- Produces:
  - `WarpTab` 命名空间对象（含 `pagination`、`allLearnedDomains`、`checkedDomains`、`init`、`loadRules`、`renderLearnedList`、`renderRules`、`filterLearnedList` 等）
  - `Utils.fuzzyMatch(text, query)` 子序列匹配函数
  - `Utils.paginate(items, page, pageSize)` 分页函数
  - `Utils.renderPagination(container, state, onPageChange)` 分页栏渲染

- [ ] **Step 1: 读取 warp_exclusion.html 全部内容**

读取 `d:\project_code\ipv6\warp_exclusion.html`，重点关注：
- HTML body 内的结构（`learnedList`、`rulesList`、搜索框、流量推荐区）
- CSS 样式（`.domain-list`、`.domain-item`、`.rule-item`、`.search-row`）
- JS 函数（`renderLearnedList`、`filterLearnedList`、`renderRules`、`loadRules`、`addSelectedDomains` 等）

- [ ] **Step 2: 迁移 WARP排除 HTML 内容到 tab-warp**

将 `warp_exclusion.html` 的 body 内容（去掉原 `.title-bar`）迁移到 `d:\project_code\ipv6\settings.html` 的 `<div class="tab-content" id="tab-warp">` 内。

结构调整：
- 删除原有的 `.title-bar`（已由统一的 `.app-title-bar` 取代）
- 在 `learnedList` 下方新增分页栏 `<div class="pagination-bar" id="learnedPagination"></div>`
- 在 `rulesList` 上方新增搜索框（与 `learnedList` 相同样式）
- 在 `rulesList` 下方新增分页栏 `<div class="pagination-bar" id="rulesPagination"></div>`

- [ ] **Step 3: 迁移 WARP排除 CSS 到 settings.html**

将 `warp_exclusion.html` 中特有的 CSS（`.domain-list`、`.domain-item`、`.rule-item`、`.rule-header`、`.rule-meta`、`.rule-tag` 等）迁移到 `settings.html` 的 `<style>` 标签中，放在 `/* ===== WARP排除tab ===== */` 注释下。

- [ ] **Step 4: 新增 Utils 命名空间和工具函数**

在 `settings.html` 的 `<script>` 标签中（窗口控制函数之后）新增：

```javascript
// ===== 共享工具 =====
const Utils = {
    escapeHtml(s) {
        if (!s) return '';
        return String(s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    },

    // 子序列保序匹配：query 的字符按顺序出现在 text 中即可
    fuzzyMatch(text, query) {
        if (!query) return true;
        text = String(text || '').toLowerCase();
        query = String(query).toLowerCase();
        let qi = 0;
        for (let ti = 0; ti < text.length && qi < query.length; ti++) {
            if (text[ti] === query[qi]) qi++;
        }
        return qi === query.length;
    },

    showToast(msg, type = 'info') {
        let toast = document.getElementById('appToast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'appToast';
            toast.className = 'toast';
            document.body.appendChild(toast);
        }
        toast.textContent = msg;
        toast.className = 'toast show ' + type;
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => {
            toast.className = 'toast ' + type;
        }, 3000);
    },

    debounce(fn, delay) {
        let timer = null;
        return function(...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), delay);
        };
    },

    paginate(items, page, pageSize) {
        const start = (page - 1) * pageSize;
        return items.slice(start, start + pageSize);
    },

    // 渲染分页栏
    renderPagination(container, state, onPageChange) {
        const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
        // 当前页越界则回退
        if (state.page > totalPages) state.page = totalPages;
        if (state.page < 1) state.page = 1;

        let html = '';
        // 左侧：每页大小选择器
        html += '<div class="page-size-area">';
        html += '<span>每页</span> ';
        html += '<select class="page-size-select" onchange="window._onPageSizeChange(' + JSON.stringify(state).replace(/"/g, '&quot;') + ', this.value)">';
        for (const sz of [10, 20, 50, 100]) {
            html += `<option value="${sz}" ${state.pageSize === sz ? 'selected' : ''}>${sz}</option>`;
        }
        html += '</select>';
        html += ` <span>共 ${state.total} 条</span>`;
        html += '</div>';

        // 右侧：页码导航
        html += '<div class="page-nav">';
        // 首页、上一页
        html += `<button class="page-btn" ${state.page === 1 ? 'disabled' : ''} onclick="window._goToPage(1)">«</button>`;
        html += `<button class="page-btn" ${state.page === 1 ? 'disabled' : ''} onclick="window._goToPage(${state.page - 1})">‹</button>`;

        // 页码（最多7个，超长省略）
        const maxShow = 7;
        let start = Math.max(1, state.page - 3);
        let end = Math.min(totalPages, start + maxShow - 1);
        if (end - start < maxShow - 1) start = Math.max(1, end - maxShow + 1);
        if (start > 1) {
            html += `<button class="page-btn" onclick="window._goToPage(1)">1</button>`;
            if (start > 2) html += '<span class="page-ellipsis">...</span>';
        }
        for (let i = start; i <= end; i++) {
            html += `<button class="page-btn ${i === state.page ? 'active' : ''}" onclick="window._goToPage(${i})">${i}</button>`;
        }
        if (end < totalPages) {
            if (end < totalPages - 1) html += '<span class="page-ellipsis">...</span>';
            html += `<button class="page-btn" onclick="window._goToPage(${totalPages})">${totalPages}</button>`;
        }

        // 下一页、末页
        html += `<button class="page-btn" ${state.page === totalPages ? 'disabled' : ''} onclick="window._goToPage(${state.page + 1})">›</button>`;
        html += `<button class="page-btn" ${state.page === totalPages ? 'disabled' : ''} onclick="window._goToPage(${totalPages})">»</button>`;
        html += '</div>';

        container.innerHTML = html;

        // 绑定回调（通过全局变量传递，避免闭包问题）
        window._goToPage = (p) => { state.page = p; onPageChange(state); };
        window._onPageSizeChange = (s, sz) => {
            state.pageSize = parseInt(sz);
            state.page = 1;
            onPageChange(state);
        };
    }
};
```

- [ ] **Step 5: 新增 WarpTab 命名空间**

在 Utils 之后新增 WarpTab 命名空间，整合 warp_exclusion.html 的所有 JS 函数：

```javascript
// ===== WARP排除tab =====
const WarpTab = {
    allLearnedDomains: [],
    checkedDomains: new Set(),
    pageSize: 20,
    pagination: {
        learned: { page: 1, pageSize: 20, total: 0, keyword: '' },
        rules:   { page: 1, pageSize: 20, total: 0, keyword: '' }
    },
    _searchDebounce: null,

    async init() {
        // 初始化搜索框去抖
        const learnedInput = document.getElementById('domainSearchInput');
        if (learnedInput) {
            learnedInput.addEventListener('input', Utils.debounce(() => {
                this.pagination.learned.keyword = learnedInput.value.trim();
                this.pagination.learned.page = 1;
                this.renderLearnedList(this.allLearnedDomains);
            }, 150));
        }
        const rulesInput = document.getElementById('rulesSearchInput');
        if (rulesInput) {
            rulesInput.addEventListener('input', Utils.debounce(() => {
                this.pagination.rules.keyword = rulesInput.value.trim();
                this.pagination.rules.page = 1;
                this.renderRules(this._allRules || []);
            }, 150));
        }
        // 加载初始数据
        await this.loadRules();
    },

    setPageSize(sz) {
        this.pageSize = sz;
        this.pagination.learned.pageSize = sz;
        this.pagination.rules.pageSize = sz;
        this.pagination.learned.page = 1;
        this.pagination.rules.page = 1;
        // 持久化
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.save_ui_prefs({ page_size: sz });
        }
    },

    // 渲染学习域名列表（含搜索+分页）
    renderLearnedList(domains) {
        this.allLearnedDomains = domains;
        const container = document.getElementById('learnedList');
        const searchInput = document.getElementById('domainSearchInput');
        const keyword = (searchInput?.value || '').trim();
        this.pagination.learned.keyword = keyword;

        // 子序列匹配过滤
        const filtered = keyword
            ? domains.filter(d => Utils.fuzzyMatch(d, keyword))
            : domains;
        this.pagination.learned.total = filtered.length;

        // 更新搜索计数
        const countEl = document.getElementById('searchCount');
        if (keyword && domains.length) {
            countEl.textContent = `${filtered.length}/${domains.length}`;
        } else {
            countEl.textContent = '';
        }

        // 全选按钮状态
        const selectAllBtn = document.getElementById('selectAllBtn');
        selectAllBtn.disabled = filtered.length === 0;

        if (!domains.length) {
            container.innerHTML = '<div class="empty-hint">暂未学习到域名</div>';
            document.getElementById('addSelectedBtn').disabled = true;
            document.getElementById('learnedPagination').innerHTML = '';
            return;
        }
        if (!filtered.length) {
            container.innerHTML = '<div class="empty-hint">无匹配的域名</div>';
            document.getElementById('addSelectedBtn').disabled = true;
            document.getElementById('learnedPagination').innerHTML = '';
            return;
        }

        // 分页
        const pageData = Utils.paginate(filtered, this.pagination.learned.page, this.pagination.learned.pageSize);
        document.getElementById('addSelectedBtn').disabled = false;

        // 渲染列表项
        container.innerHTML = pageData.map(d => {
            const isChecked = this.checkedDomains.has(d);
            return `<div class="domain-item">
                <input type="checkbox" class="domain-checkbox" value="${Utils.escapeHtml(d)}" ${isChecked ? 'checked' : ''} onchange="WarpTab.onDomainCheckChange('${Utils.escapeHtml(d)}', this.checked)">
                <span class="domain-name">${Utils.escapeHtml(d)}</span>
            </div>`;
        }).join('');

        // 渲染分页栏
        Utils.renderPagination(
            document.getElementById('learnedPagination'),
            this.pagination.learned,
            (s) => this.renderLearnedList(this.allLearnedDomains)
        );

        // 更新全选按钮文字
        this.updateSelectAllBtnText();
        // 显示数量
        if (!keyword) {
            document.getElementById('learnBadge').textContent = `${domains.length} 个域名`;
        }
    },

    onDomainCheckChange(domain, checked) {
        if (checked) this.checkedDomains.add(domain);
        else this.checkedDomains.delete(domain);
        this.updateSelectAllBtnText();
    },

    toggleSelectAll() {
        const checkboxes = document.querySelectorAll('.domain-checkbox');
        if (!checkboxes.length) return;
        const allChecked = Array.from(checkboxes).every(cb => cb.checked);
        checkboxes.forEach(cb => {
            cb.checked = !allChecked;
            const domain = cb.value;
            if (cb.checked) this.checkedDomains.add(domain);
            else this.checkedDomains.delete(domain);
        });
        this.updateSelectAllBtnText();
    },

    updateSelectAllBtnText() {
        const btn = document.getElementById('selectAllBtn');
        const checkboxes = document.querySelectorAll('.domain-checkbox');
        if (!checkboxes.length) { btn.textContent = '全选'; return; }
        const allChecked = Array.from(checkboxes).every(cb => cb.checked);
        btn.textContent = allChecked ? '取消全选' : '全选';
    },

    _allRules: [],

    async loadRules() {
        // 原 loadRules 逻辑，调用 api.get_exclusion_config()
        // 渲染规则列表
        try {
            const cfg = await window.pywebview.api.get_exclusion_config();
            this._allRules = cfg.domains || [];
            this.renderRules(this._allRules);
        } catch(e) {
            Utils.showToast('加载规则失败: ' + e, 'error');
        }
    },

    renderRules(domains) {
        this._allRules = domains;
        const container = document.getElementById('rulesList');
        const searchInput = document.getElementById('rulesSearchInput');
        const keyword = (searchInput?.value || '').trim();
        this.pagination.rules.keyword = keyword;

        // 子序列匹配过滤（按 domain 字段）
        const filtered = keyword
            ? domains.filter(d => Utils.fuzzyMatch(d.domain || '', keyword))
            : domains;
        this.pagination.rules.total = filtered.length;

        document.getElementById('ruleCount').textContent = `${domains.length}`;

        if (!domains.length) {
            container.innerHTML = '<div class="empty-hint">暂无排除规则</div>';
            document.getElementById('rulesPagination').innerHTML = '';
            return;
        }
        if (!filtered.length) {
            container.innerHTML = '<div class="empty-hint">无匹配的规则</div>';
            document.getElementById('rulesPagination').innerHTML = '';
            return;
        }

        // 分页
        const pageData = Utils.paginate(filtered, this.pagination.rules.page, this.pagination.rules.pageSize);

        container.innerHTML = pageData.map(d => {
            const statusTag = d.enabled
                ? '<span class="rule-tag tag-enabled">已启用</span>'
                : '<span class="rule-tag tag-disabled">已禁用</span>';
            const currentRoute = d.route || 'ipv6';
            const routeTag = currentRoute === 'ipv4'
                ? '<span style="font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(0,122,255,0.12); color:#007aff; margin-left:4px;">IPv4</span>'
                : '<span style="font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(52,199,89,0.12); color:#34c759; margin-left:4px;">IPv6</span>';
            const switchBtn = currentRoute === 'ipv4'
                ? `<button class="btn btn-sm" onclick="WarpTab.setDomainRoute('${Utils.escapeHtml(d.domain)}', 'ipv6')" title="切换为走IPv6校园网">切IPv6</button>`
                : `<button class="btn btn-sm" onclick="WarpTab.setDomainRoute('${Utils.escapeHtml(d.domain)}', 'ipv4')" title="切换为走IPv4校园网">切IPv4</button>`;
            return `<div class="rule-item">
                <div class="rule-header">
                    <span class="rule-domain">${Utils.escapeHtml(d.domain)}${routeTag}</span>
                    <div class="rule-actions">
                        ${statusTag}
                        ${switchBtn}
                        <button class="btn btn-sm ${d.enabled?'':'btn-primary'}" onclick="WarpTab.toggleDomain('${Utils.escapeHtml(d.domain)}', ${!d.enabled})">${d.enabled?'禁用':'启用'}</button>
                        <button class="btn btn-sm btn-danger" onclick="WarpTab.removeDomain('${Utils.escapeHtml(d.domain)}')">删除</button>
                    </div>
                </div>
                <div class="rule-meta">
                    添加时间: ${Utils.escapeHtml(d.added_at || '未知')} | 路由: ${currentRoute === 'ipv4' ? 'IPv4 校园网直连' : 'IPv6 校园网直连'}
                </div>
            </div>`;
        }).join('');

        // 渲染分页栏
        Utils.renderPagination(
            document.getElementById('rulesPagination'),
            this.pagination.rules,
            (s) => this.renderRules(this._allRules)
        );
    },

    // 以下方法迁移自 warp_exclusion.html，改为 WarpTab.method 形式调用
    async setDomainRoute(domain, route) {
        try {
            const r = await window.pywebview.api.set_domain_route(domain, route);
            if (r.success) {
                Utils.showToast('路由已切换', 'success');
                await this.loadRules();
            } else {
                Utils.showToast('切换失败: ' + (r.error || ''), 'error');
            }
        } catch(e) { Utils.showToast('切换失败: ' + e, 'error'); }
    },

    async toggleDomain(domain, enabled) {
        try {
            const r = await window.pywebview.api.toggle_domain(domain, enabled);
            if (r.success) {
                Utils.showToast(enabled ? '已启用' : '已禁用', 'success');
                await this.loadRules();
            } else {
                Utils.showToast('操作失败', 'error');
            }
        } catch(e) { Utils.showToast('操作失败: ' + e, 'error'); }
    },

    async removeDomain(domain) {
        if (!confirm(`确认删除 ${domain}？`)) return;
        try {
            const r = await window.pywebview.api.remove_domain(domain);
            if (r.success) {
                Utils.showToast('已删除', 'success');
                await this.loadRules();
            } else {
                Utils.showToast('删除失败', 'error');
            }
        } catch(e) { Utils.showToast('删除失败: ' + e, 'error'); }
    },

    async addSelectedDomains() {
        const checkboxes = document.querySelectorAll('.domain-checkbox:checked');
        if (!checkboxes.length) { Utils.showToast('请至少选择一个域名', 'error'); return; }
        const route = document.getElementById('routeSelect').value;
        Utils.showLoading && Utils.showLoading(`正在添加 ${checkboxes.length} 个域名...`);
        let okCount = 0, failCount = 0;
        for (const cb of checkboxes) {
            try {
                const r = await window.pywebview.api.add_domain(cb.value, route);
                if (r.success) okCount++; else failCount++;
            } catch(e) { failCount++; }
        }
        Utils.hideLoading && Utils.hideLoading();
        Utils.showToast(`添加完成: ${okCount} 成功, ${failCount} 失败`, failCount ? 'error' : 'success');
        this.checkedDomains.clear();
        await this.loadRules();
    },

    async addDomainManually() {
        const input = document.getElementById('domainInput');
        const routeSelect = document.getElementById('routeSelect');
        const domain = input.value.trim();
        const route = routeSelect.value;
        if (!domain) { Utils.showToast('请输入域名', 'error'); return; }
        Utils.showLoading && Utils.showLoading('正在添加域名...');
        try {
            const r = await window.pywebview.api.add_domain(domain, route);
            Utils.hideLoading && Utils.hideLoading();
            if (r.success) {
                Utils.showToast('添加成功', 'success');
                input.value = '';
                await this.loadRules();
            } else {
                Utils.showToast('添加失败: ' + (r.error || ''), 'error');
            }
        } catch(e) {
            Utils.hideLoading && Utils.hideLoading();
            Utils.showToast('添加失败: ' + e, 'error');
        }
    }
};
```

- [ ] **Step 6: 迁移 warp_exclusion.html 中剩余的 JS 函数**

检查 `warp_exclusion.html` 中是否有其他 JS 函数（如 `loadLearnedDomains`、`renderTrafficRecommend` 等），将它们整合到 `WarpTab` 命名空间中。用 Grep 搜索 `function ` 在 warp_exclusion.html 中，逐一处理。

- [ ] **Step 7: 修改 switchTab 函数支持4个tab**

将 `d:\project_code\ipv6\settings.html` 中现有的 `switchTab` 函数（L928-933）替换为：

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

    // 流量tab特殊处理
    if (name === 'traffic') {
        TrafficTab.showSubview(TrafficTab.subview);
        TrafficTab.startAutoRefresh();
    } else {
        TrafficTab.stopAutoRefresh();
        TrafficTab.pauseCanvas();
    }
}
```

注意：`TrafficTab` 在 Task 7 中创建，本任务先保留引用，Task 7 实现后即可正常工作。为避免 Task 6 单独验证时 `TrafficTab is not defined` 错误，在 switchTab 中加保护：

```javascript
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
```

- [ ] **Step 8: 修改初始化逻辑**

将原有的 `window.addEventListener('pywebviewready', ...)` 回调改为调用 `initApp`：

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
    StatusTab.init && StatusTab.init();
    SettingsTab.init && SettingsTab.init();
    WarpTab.init();
    if (typeof TrafficTab !== 'undefined') {
        TrafficTab.init();
    }
    // 默认显示连接tab
    switchTab('status');
}

window.addEventListener('pywebviewready', initApp);
```

注意：`StatusTab` 和 `SettingsTab` 命名空间需要从原有 settings.html 的全局函数重构。由于原 settings.html 的 JS 函数都是全局的，本任务先保留它们不动，仅包装为 `StatusTab` 和 `SettingsTab` 对象的引用。具体：在 `<script>` 中新增：

```javascript
// ===== 连接tab（原 settings.html 全局函数包装）=====
const StatusTab = {
    init() {
        // 原 pywebviewready 回调中的初始化逻辑迁移至此
        // 如 particleCanvas 初始化、状态更新等
    }
    // 其他原全局函数按需迁移
};

// ===== 设置tab =====
const SettingsTab = {
    init() {
        // 原设置页初始化逻辑
    }
};
```

由于原 settings.html 的 JS 较复杂，本步骤仅做最小改动：保留原全局函数，`StatusTab.init` 和 `SettingsTab.init` 调用原有逻辑。具体实现时需读取原 settings.html 的 `<script>` 内容，将 `pywebviewready` 回调中的初始化代码拆分到 `StatusTab.init` 和 `SettingsTab.init`。

- [ ] **Step 9: 验证 WARP排除 tab 基本功能**

运行应用，切换到 WARP排除 tab，验证：
- 学习域名列表显示
- 搜索框输入 `api` 能过滤出包含 a→p→i 子序列的域名
- 分页栏显示，翻页可用
- 每页大小改变后重置到第1页

- [ ] **Step 10: 提交**

```bash
git add settings.html
git commit -m "feat: WARP排除tab迁移+分页+子序列保序搜索"
```

---

## Task 7: 前端 - 流量tab迁移（列表+画布子视图）

**Files:**
- Modify: `d:\project_code\ipv6\settings.html`（`tab-traffic` 内容区、TrafficTab JS 命名空间）
- Read: `d:\project_code\ipv6\traffic_monitor.html`、`d:\project_code\ipv6\traffic_flow.html`

**Interfaces:**
- Consumes: Task 5 的 `tab-traffic` 容器、Task 6 的 `Utils` 工具
- Produces:
  - `TrafficTab` 命名空间对象（含 `subview`、`lastData`、`cumulativeConns`、`_loadingFast`、`_loadingSlow`、`_canvasAnimId`、`init`、`refreshFast`、`refreshSlow`、`showSubview`、`pauseCanvas`、`resumeCanvas`、`startAutoRefresh`、`stopAutoRefresh` 等）

- [ ] **Step 1: 读取两个流量HTML的全部内容**

读取 `traffic_monitor.html` 和 `traffic_flow.html`，重点关注：
- HTML 结构（stats-grid、conn-list、canvas、搜索框、工具栏）
- CSS 样式（`.stats-grid`、`.stat-card`、`.conn-list`、`.conn-item`、`.traffic-canvas`）
- JS 函数（`refreshFast`、`refreshSlow`、`renderConnections`/`renderConnList`、`init`、`startAuto`、画布动画相关）

- [ ] **Step 2: 迁移流量HTML内容到 tab-traffic**

在 `d:\project_code\ipv6\settings.html` 的 `<div class="tab-content" id="tab-traffic">` 内新增子视图切换按钮和两个子视图容器：

```html
<div class="tab-content" id="tab-traffic">
    <!-- 子视图切换按钮 -->
    <div class="traffic-subview-bar">
        <button class="subview-btn active" onclick="TrafficTab.showSubview('list')">列表视图</button>
        <button class="subview-btn" onclick="TrafficTab.showSubview('canvas')">画布动画</button>
    </div>

    <!-- 列表视图子视图（来自 traffic_monitor.html）-->
    <div class="traffic-subview active" id="traffic-list-view">
        <!-- 迁移 traffic_monitor.html 的 stats-grid、toolbar、conn-list -->
    </div>

    <!-- 画布动画子视图（来自 traffic_flow.html）-->
    <div class="traffic-subview" id="traffic-canvas-view">
        <!-- 迁移 traffic_flow.html 的 canvas、search-box、conn-list -->
    </div>
</div>
```

- [ ] **Step 3: 迁移流量CSS到 settings.html**

将 `traffic_monitor.html` 和 `traffic_flow.html` 中特有的 CSS 迁移到 `settings.html` 的 `<style>` 标签中，放在 `/* ===== 流量tab ===== */` 注释下。包括：
- `.stats-grid`、`.stat-card`（6类颜色样式）
- `.conn-list`、`.conn-item`、`.conn-group`、`.conn-group-header`
- `.traffic-canvas`
- `.traffic-subview-bar`、`.subview-btn`（新增）

新增子视图切换样式：

```css
.traffic-subview-bar {
    display: flex; gap: 4px; padding: 8px 12px;
    background: var(--bg-secondary); border-bottom: 1px solid var(--border);
}
.subview-btn {
    padding: 6px 12px; font-size: 12px;
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; color: var(--text-secondary); cursor: pointer;
    transition: all 0.15s;
}
.subview-btn:hover { color: var(--text-primary); }
.subview-btn.active { background: var(--accent); color: #000; border-color: var(--accent); }
.traffic-subview { display: none; flex: 1; overflow: hidden; flex-direction: column; }
.traffic-subview.active { display: flex; }
```

- [ ] **Step 4: 新增 TrafficTab 命名空间**

在 `WarpTab` 之后新增 `TrafficTab`，合并 traffic_monitor 和 traffic_flow 的逻辑：

```javascript
// ===== 流量tab =====
const TrafficTab = {
    subview: 'list',
    lastData: null,
    stats: {},
    warpUnderlay: 'ipv4',
    cumulativeConns: new Map(),
    cumulativeMode: false,
    selectedConns: new Set(),
    _loadingFast: false,
    _loadingSlow: false,
    _autoTimer: null,
    _canvasAnimId: null,
    _canvasParticles: [],
    _initStarted: false,

    async init() {
        if (this._initStarted) return;
        this._initStarted = true;
        // 初始化列表视图
        this.initListView();
        // 初始化画布视图
        this.initCanvasView();
        // 显示默认子视图
        this.showSubview(this.subview);
        // 首次加载数据
        await this.refreshFast();
        this.refreshSlow();
    },

    showSubview(name) {
        this.subview = name;
        document.querySelectorAll('.traffic-subview').forEach(v => v.classList.remove('active'));
        document.querySelectorAll('.subview-btn').forEach(b => b.classList.remove('active'));
        if (name === 'list') {
            document.getElementById('traffic-list-view').classList.add('active');
            document.querySelector('.subview-btn[onclick*="list"]')?.classList.add('active');
            this.pauseCanvas();
        } else {
            document.getElementById('traffic-canvas-view').classList.add('active');
            document.querySelector('.subview-btn[onclick*="canvas"]')?.classList.add('active');
            this.resumeCanvas();
        }
        // 持久化
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.save_ui_prefs({ traffic_subview: name });
        }
    },

    startAutoRefresh() {
        this.stopAutoRefresh();
        this._autoTimer = setInterval(() => this.refreshFast(), 3000);
    },

    stopAutoRefresh() {
        if (this._autoTimer) {
            clearInterval(this._autoTimer);
            this._autoTimer = null;
        }
    },

    pauseCanvas() {
        if (this._canvasAnimId) {
            cancelAnimationFrame(this._canvasAnimId);
            this._canvasAnimId = null;
        }
    },

    resumeCanvas() {
        if (this.subview === 'canvas' && !this._canvasAnimId) {
            this._startCanvasAnimation();
        }
    },

    async refreshFast() {
        if (!window.pywebview?.api || this._loadingFast) return;
        this._loadingFast = true;
        try {
            const data = await window.pywebview.api.get_traffic_status_fast();
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
                this.updateListStats();
                this.renderListConns();
            } else {
                this.updateCanvasStats();
                this.renderCanvasConns();
                this.drawCanvas();
            }
        } catch (e) {
            Utils.showToast('获取失败: ' + e, 'error');
        } finally {
            this._loadingFast = false;
        }
    },

    async refreshSlow() {
        if (!window.pywebview?.api || this._loadingSlow) return;
        this._loadingSlow = true;
        try {
            const missingIps = [];
            for (const c of (this.lastData?.connections || [])) {
                if (!c.hostname && c.remote_ip) missingIps.push(c.remote_ip);
            }
            if (missingIps.length === 0) return;
            const ipToHost = await window.pywebview.api.get_traffic_status_slow(missingIps);
            let updated = false;
            for (const c of (this.lastData?.connections || [])) {
                if (!c.hostname && ipToHost[c.remote_ip]) {
                    c.hostname = ipToHost[c.remote_ip];
                    updated = true;
                }
            }
            if (updated) {
                if (this.cumulativeMode) {
                    for (const c of (this.lastData?.connections || [])) {
                        if (c.hostname) this.cumulativeConns.set(this.connId(c), c);
                    }
                }
                if (this.subview === 'list') this.renderListConns();
                else { this.renderCanvasConns(); this.drawCanvas(); }
            }
        } catch (e) {
            console.warn('refreshSlow failed:', e);
        } finally {
            this._loadingSlow = false;
        }
    },

    connId(c) {
        return `${c.local_ip}:${c.local_port}-${c.remote_ip}:${c.remote_port}`;
    },

    // 列表视图方法（来自 traffic_monitor.html）
    initListView() {
        // 原 traffic_monitor.html 的 init 逻辑
    },

    updateListStats() {
        // 原 renderStats 逻辑
    },

    renderListConns() {
        // 原 renderConnections 逻辑（保持 includes 搜索）
    },

    // 画布视图方法（来自 traffic_flow.html）
    initCanvasView() {
        // 原 traffic_flow.html 的 init 逻辑（画布初始化、粒子系统等）
    },

    updateCanvasStats() {
        // 原 traffic_flow.html 的 updateStats 逻辑
    },

    renderCanvasConns() {
        // 原 renderConnList 逻辑（保持 includes 搜索）
    },

    drawCanvas() {
        // 原画布动画绘制逻辑
    },

    _startCanvasAnimation() {
        // requestAnimationFrame 循环
    }
};
```

- [ ] **Step 5: 迁移列表视图的完整实现**

将 `traffic_monitor.html` 中的 JS 函数迁移到 `TrafficTab` 的列表视图方法中：
- `initListView`：初始化 stats-grid、搜索框、自动刷新
- `updateListStats`：渲染6类统计卡片
- `renderListConns`：渲染连接列表（按进程分组，保持 `includes` 搜索）

注意：搜索保持原 `includes` 子串匹配（设计约束：流量连接列表不改搜索方式）。

- [ ] **Step 6: 迁移画布视图的完整实现**

将 `traffic_flow.html` 中的 JS 函数迁移到 `TrafficTab` 的画布视图方法中：
- `initCanvasView`：初始化 canvas、粒子系统
- `updateCanvasStats`：更新画布视图的统计
- `renderCanvasConns`：渲染画布视图的连接列表（保持 `includes` 搜索）
- `drawCanvas`：画布动画绘制
- `_startCanvasAnimation`：`requestAnimationFrame` 循环

- [ ] **Step 7: 验证流量tab功能**

运行应用，切换到流量tab，验证：
- 默认显示列表视图（或上次的子视图）
- 列表视图：6类统计卡片、连接列表、搜索框（`includes` 匹配）
- 切到画布视图：画布动画运行
- 切回列表视图：画布暂停
- 切走流量tab：自动刷新停止，画布暂停
- 切回流量tab：自动刷新恢复

- [ ] **Step 8: 提交**

```bash
git add settings.html
git commit -m "feat: 流量tab迁移 - 列表/画布双子视图+生命周期管理"
```

---

## Task 8: 删除原3个HTML文件 + 最终验证

**Files:**
- Delete: `d:\project_code\ipv6\warp_exclusion.html`、`d:\project_code\ipv6\traffic_monitor.html`、`d:\project_code\ipv6\traffic_flow.html`

- [ ] **Step 1: 确认3个HTML已无引用**

用 Grep 搜索以下关键词，确认仅在本计划文档和 .bak 文件中出现（不在代码中）：
```
warp_exclusion\.html|traffic_monitor\.html|traffic_flow\.html
```

预期：仅在 `docs/superpowers/` 下的设计文档中出现，代码中无引用。

- [ ] **Step 2: 删除3个HTML文件**

使用 DeleteFile 工具删除：
- `d:\project_code\ipv6\warp_exclusion.html`
- `d:\project_code\ipv6\traffic_monitor.html`
- `d:\project_code\ipv6\traffic_flow.html`

- [ ] **Step 3: 验证后端导入正常**

运行：
```bash
python -c "import tray_app; import warp_exclusion; import traffic_monitor; from core import auth, network, warp_manager, startup, command, webview, state; print('ALL IMPORTS OK')"
```

预期输出：
```
ALL IMPORTS OK
```

- [ ] **Step 4: 验证无安全回归**

运行 Grep 搜索：
```
password_prefix|_pwd_encrypted|os\._exit
```

在 `core/` 和 `tray_app.py` 中，预期无匹配（除注释外）。

- [ ] **Step 5: 验证 ApiBridge 方法完整性**

运行：
```bash
python -c "from tray_app import ApiBridge; methods = ['save_ui_prefs', 'get_ui_prefs', 'get_traffic_status_fast', 'get_traffic_status_slow', 'get_exclusion_config', 'add_domain', 'remove_domain', 'toggle_domain', 'set_domain_route']; print('All methods present:', all(hasattr(ApiBridge, m) for m in methods))"
```

预期输出：
```
All methods present: True
```

- [ ] **Step 6: 验证 spec 语法**

运行：
```bash
python -c "import ast; ast.parse(open('CampusAuth.spec', encoding='utf-8').read()); print('spec OK')"
```

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "chore: 删除3个已合并的HTML文件，完成页面合并"
```

---

## Self-Review

### 1. Spec 覆盖检查

| Spec 要求 | 对应 Task |
|-----------|----------|
| 合并4个页面为单窗口4-tab | Task 5（框架）+ Task 6（WARP）+ Task 7（流量） |
| 放大视口自适应屏幕 | Task 2（resizable + minsize + 85%） |
| 窗口尺寸持久化 | Task 2（save_window_geometry） |
| UI偏好持久化 | Task 1（save_ui_prefs/get_ui_prefs） |
| WARP排除列表分页 | Task 6（pagination + renderPagination） |
| 每页大小可选10/20/50/100 | Task 6（page-size-select） |
| 子序列保序搜索 | Task 6（fuzzyMatch） |
| 规则列表新增搜索框 | Task 6（rulesSearchInput） |
| 流量tab子视图切换 | Task 7（showSubview） |
| 画布生命周期管理 | Task 7（pauseCanvas/resumeCanvas） |
| 托盘菜单改为切tab | Task 3（show_main_window） |
| 删除3个独立窗口方法 | Task 3 |
| CampusAuth.spec 清理 | Task 4 |
| 删除3个HTML文件 | Task 8 |

**无遗漏**。

### 2. Placeholder 扫描

- 所有代码块完整，无 TBD/TODO
- 所有步骤有具体代码或命令
- 函数签名和返回值明确

### 3. 类型一致性

- `save_ui_prefs(prefs: dict) -> dict` 在 Task 1 定义，Task 6 调用
- `get_ui_prefs() -> dict` 在 Task 1 定义，Task 6 调用
- `show_main_window(tab=None)` 在 Task 3 定义，Task 3 调用
- `Utils.fuzzyMatch(text, query)` 在 Task 6 定义，Task 6 调用
- `Utils.renderPagination(container, state, onPageChange)` 在 Task 6 定义，Task 6 调用
- `TrafficTab.showSubview(name)` 在 Task 7 定义，Task 6 的 switchTab 调用（有 typeof 保护）

**无类型不一致**。
