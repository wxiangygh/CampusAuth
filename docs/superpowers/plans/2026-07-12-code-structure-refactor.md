# CampusAuth 代码结构重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 2733 行的 tray_app.py 按职责拆分为 7 个 core/ 模块，主文件瘦身到 ~500 行，顺手修复密码日志、bare except、os._exit 等安全问题。

**Architecture:** 自底向上迁移——先创建无依赖的 state.py，再逐层创建依赖前者的模块，每步创建新模块后立即更新 tray_app.py 导入并删除旧代码，保持应用始终可运行。HTML 前端和 ApiBridge 接口完全不变。

**Tech Stack:** Python 3.12, pystray, pywebview, PIL, ctypes, Windows API

## Global Constraints

- 目标平台：Windows 10/11，Python 3.12
- ApiBridge 类的所有方法签名保持完全不变
- HTML 文件（settings.html、warp_exclusion.html、traffic_monitor.html、traffic_flow.html）零修改
- 不添加新依赖，不添加单元测试，不合并配置文件
- 不实现 DPAPI 加密
- 代码注释使用中文
- 命令使用 `python` 执行
- 所有 bare `except:` 改为 `except Exception:`
- 所有 `os._exit(0)` 改为 `sys.exit(0)`
- 验证方式：`python -c "from core.X import Y"` 导入检查 + `python -c "import tray_app"` 集成检查

---

## File Structure

### 新建文件

| 文件 | 职责 | 行数估算 |
|------|------|----------|
| `core/__init__.py` | 包标识，空文件 | 1 |
| `core/state.py` | 全局状态变量 | ~20 |
| `core/command.py` | 统一命令执行（3 个函数） | ~150 |
| `core/webview.py` | WebView 窗口辅助（2 个函数 + Win32 常量） | ~60 |
| `core/network.py` | 网络检测（9 个函数） | ~180 |
| `core/warp_manager.py` | WARP 管理（8 个函数） | ~200 |
| `core/auth.py` | 认证流程（9 个函数，含安全修复） | ~350 |
| `core/startup.py` | 启动与事件（13 个函数，含 os._exit 修复） | ~250 |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `tray_app.py` | 删除迁移的函数，改为从 core 导入；修复密码日志、bare except、os._exit |
| `warp_exclusion.py` | `_run_command` 改为从 core.command 导入；修复 2 处 bare except |
| `traffic_monitor.py` | `_run_ps` 改为从 core.command 导入 |
| `CampusAuth.spec` | hiddenimports 添加 core 模块 |

---

### Task 1: 创建 core/ 包与 state.py

**Files:**
- Create: `core/__init__.py`
- Create: `core/state.py`
- Modify: `tray_app.py:730,903-906,1063,2019`（全局变量改为从 state 导入）

**Interfaces:**
- Consumes: 无
- Produces: `core.state._auth_cancelled`（threading.Event）、`core.state._auth_lock`（threading.Lock）、`core.state._tray_app_instance`（None 或 TrayApp 实例）、`core.state._wifi_event_handle`（None 或句柄）、`core.state._wifi_monitor_started`（bool）、`core.state._conf_json_backup`（None 或 dict）、`core.state.WIFI_EVENT_NAME`（str）、`core.state.TRAY_MUTEX`（None 或 mutex 句柄）

- [ ] **Step 1: 创建 core/__init__.py**

创建空文件 `core/__init__.py`：

```python
# CampusAuth core 模块包
```

- [ ] **Step 2: 创建 core/state.py**

```python
"""全局状态变量集中管理。

所有模块通过 from core.state import X 来访问共享状态，
避免全局变量散落在各文件中导致状态不一致。
"""
import threading

# WiFi 事件名称（用于跨进程事件通知）
WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"

# 认证流程控制
_auth_lock = threading.Lock()
_auth_cancelled = threading.Event()

# WiFi 事件监视
_wifi_event_handle = None
_wifi_monitor_started = False

# WARP 配置备份（_set_warp_endpoint_ipv6 使用）
_conf_json_backup = None

# 托盘应用实例引用（由 tray_app.py 的 main() 设置）
_tray_app_instance = None

# 单例控制互斥锁句柄（由 check_single_instance 设置，on_exit 释放）
TRAY_MUTEX = None
```

- [ ] **Step 3: 验证 state.py 导入**

Run: `python -c "from core.state import _auth_cancelled, _auth_lock, _tray_app_instance, _wifi_event_handle, _wifi_monitor_started, _conf_json_backup, WIFI_EVENT_NAME, TRAY_MUTEX; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 修改 tray_app.py — 删除全局变量定义，改为导入**

**关键规则**：变量分两类处理——

**A. 不会被重新赋值的变量**（Event/Lock/常量）：使用 `from core.state import X` 导入，因为只调用方法（`.set()`、`.is_set()`、`.acquire()`），不重新绑定名称。
- `_auth_cancelled`（Event）
- `_auth_lock`（Lock）
- `WIFI_EVENT_NAME`（str 常量）

**B. 会被重新赋值的变量**（None/bool/dict/对象）：必须通过 `import core.state` 然后用 `core.state.X = value` 赋值，因为 `from core.state import X` 只会复制初始值，后续修改不会同步。
- `_wifi_event_handle`（在 wifi_event_monitor、cleanup_wifi_event 中赋值）
- `_wifi_monitor_started`（在 wifi_event_monitor、start_wifi_event_monitor 中赋值）
- `_conf_json_backup`（在 _set_warp_endpoint_ipv6 中赋值）
- `_tray_app_instance`（在 main、on_exit、TrayApp 中赋值）

在 `tray_app.py` 第 19 行 `from traffic_monitor import get_traffic_status` 之后添加：

```python
import core.state
from core.state import _auth_lock, _auth_cancelled, WIFI_EVENT_NAME
```

然后删除以下行（这些变量现在从 core.state 导入）：
- 第 86 行：`TRAY_MUTEX = None`
- 第 730 行：`_conf_json_backup = None`
- 第 903 行：`WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"`
- 第 904 行：`_auth_lock = threading.Lock()`
- 第 905 行：`_auth_cancelled = threading.Event()`
- 第 906 行：`_wifi_event_handle = None`
- 第 1063 行：`_wifi_monitor_started = False`
- 第 2019 行：`_tray_app_instance = None`

**修改 global 声明和赋值**：

需要处理的 `global` 声明（仅针对留在 tray_app.py 中的函数）：

1. `on_exit`（第 2123 行附近）：`global _tray_app_instance` 和 `global TRAY_MUTEX`
2. `TrayApp` 类中（第 2533 行附近）：`global _tray_app_instance`
3. `main` 函数中：`_tray_app_instance = app` 赋值（如有 `global` 声明也删除）

将所有 `global _tray_app_instance` 和 `global TRAY_MUTEX` 声明删除。
将 `_tray_app_instance` 引用改为 `core.state._tray_app_instance`。
将 `TRAY_MUTEX` 引用改为 `core.state.TRAY_MUTEX`。

例如 `on_exit`：
```python
# 修复前
global _tray_app_instance
if _tray_app_instance:
    _tray_app_instance._should_exit = True
# ...
global TRAY_MUTEX
if TRAY_MUTEX:
    kernel32.CloseHandle(TRAY_MUTEX)
    TRAY_MUTEX = None

# 修复后
if core.state._tray_app_instance:
    core.state._tray_app_instance._should_exit = True
# ...
if core.state.TRAY_MUTEX:
    kernel32.CloseHandle(core.state.TRAY_MUTEX)
    core.state.TRAY_MUTEX = None
```

注意：`global _conf_json_backup`（第 753 行）、`global _wifi_event_handle`（第 989、1125 行）、`global _wifi_monitor_started`（第 1060、1066 行）将在 Task 5 和 Task 7 中随函数迁移到 core 模块时处理，本步不需要处理。

注意：`_auth_cancelled` 和 `_auth_lock` 在 `run_auth_task` 等函数中只调用方法（`.set()`、`.clear()`、`.acquire()`），不需要 `global` 声明。如果存在 `global _auth_cancelled` 或 `global _auth_lock` 声明，直接删除即可。

- [ ] **Step 5: 验证 tray_app.py 仍可导入**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`，无 ImportError

如果报错 `cannot import name '_tray_app_instance' from 'core.state'`，检查 core/state.py 是否正确创建。

- [ ] **Step 6: 提交**

```bash
git add core/__init__.py core/state.py tray_app.py
git commit -m "refactor: 提取全局状态到 core/state.py"
```

---

### Task 2: 创建 core/command.py — 统一命令执行

**Files:**
- Create: `core/command.py`
- Modify: `tray_app.py:139-365`（删除 run_command、run_command_os_system、run_elevated_powershell）
- Modify: `traffic_monitor.py:43-58`（删除 _run_ps）
- Modify: `warp_exclusion.py:32-47`（删除 _run_command）

**Interfaces:**
- Consumes: `core.state._auth_cancelled`
- Produces:
  - `run_command(cmd, shell=True, timeout=30) -> tuple[int, str, str]` — 执行命令，支持取消，用临时文件捕获输出
  - `run_elevated_powershell(ps_command, timeout=30) -> tuple[int, str, str]` — 提权执行 PowerShell
  - `run_powershell_simple(cmd, timeout=15) -> tuple[int, str, str]` — 简单 PowerShell 执行（无取消）

- [ ] **Step 1: 创建 core/command.py**

将 `tray_app.py:139-261`（`run_command`）、`263-311`（`run_command_os_system`）、`313-365`（`run_elevated_powershell`）的逻辑合并。

```python
"""统一命令执行模块。

提供三种命令执行方式：
- run_command: 完整执行，支持取消，用临时文件捕获输出
- run_elevated_powershell: 提权执行 PowerShell
- run_powershell_simple: 简单执行（无取消），供 traffic_monitor 和 warp_exclusion 使用
"""
import os
import sys
import time
import uuid
import ctypes
import logging
import tempfile
import subprocess

from core.state import _auth_cancelled

logger = logging.getLogger('tray_app')


def run_command(cmd, shell=True, timeout=30):
    """执行命令，返回 (exit_code, stdout, stderr)。
    支持通过 _auth_cancelled 事件取消。
    使用临时文件捕获输出，避免管理员权限下的管道问题。
    """
    # 从原 tray_app.py:139-261 移动完整实现
    # 修改：所有 except: 改为 except Exception:
    # 移动 tray_app.py 第 139-261 行的代码到此处


def run_elevated_powershell(ps_command, timeout=30):
    """提权执行 PowerShell 命令，返回 (exit_code, stdout, stderr)"""
    # 从原 tray_app.py:313-365 移动完整实现
    # 修改：所有 except: 改为 except Exception:


def run_powershell_simple(cmd, timeout=15):
    """简单执行 PowerShell 命令（无取消、无临时文件）。
    合并 traffic_monitor._run_ps 和 warp_exclusion._run_command。
    返回 (exit_code, stdout, stderr)。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    try:
        result = subprocess.run(
            ['powershell', '-Command', cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.returncode, result.stdout or '', result.stderr or ''
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'
    except Exception as e:
        return -1, '', str(e)
```

**实现细节**：将 `tray_app.py` 第 139-261 行 `run_command` 的完整代码移动到 `core/command.py`，修改所有 `except:` 为 `except Exception:`。同理移动 `run_elevated_powershell`（313-365 行）。`run_powershell_simple` 直接使用上面代码（合并自 traffic_monitor._run_ps 和 warp_exclusion._run_command）。

注意：`run_command` 中引用了 `logger`，确保 `core/command.py` 顶部有 `logger = logging.getLogger('tray_app')` 以保持日志器名称一致。

注意：`run_command_os_system`（263-311 行）不再单独保留——其调用方改用 `run_command`（参数 `shell=True` 已覆盖其功能）。搜索 tray_app.py 中所有 `run_command_os_system` 的调用点，改为 `run_command`。

- [ ] **Step 2: 验证 command.py 导入**

Run: `python -c "from core.command import run_command, run_elevated_powershell, run_powershell_simple; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 删除旧函数，添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.command import run_command, run_elevated_powershell
```

删除 tray_app.py 中：
- 第 139-261 行：`run_command` 函数定义
- 第 263-311 行：`run_command_os_system` 函数定义
- 第 313-365 行：`run_elevated_powershell` 函数定义

搜索 tray_app.py 中所有 `run_command_os_system(` 调用，替换为 `run_command(`。

- [ ] **Step 4: 修改 traffic_monitor.py — 删除 _run_ps，改用 core.command**

在 traffic_monitor.py 顶部添加：
```python
from core.command import run_powershell_simple as _run_ps
```

删除 traffic_monitor.py 第 43-58 行的 `_run_ps` 函数定义。

注意：traffic_monitor.py 中调用 `_run_ps` 的地方不需要修改（因为导入时用了 `as _run_ps` 别名）。

- [ ] **Step 5: 修改 warp_exclusion.py — 删除 _run_command，改用 core.command**

在 warp_exclusion.py 顶部添加：
```python
from core.command import run_powershell_simple
```

删除 warp_exclusion.py 第 32-47 行的 `_run_command` 函数定义。

搜索 warp_exclusion.py 中所有 `_run_command(` 调用，替换为 `run_powershell_simple(`。

同时修复 warp_exclusion.py 第 311 行和第 368 行的 bare `except:` → `except Exception:`。

- [ ] **Step 6: 验证所有文件导入正常**

Run: `python -c "import tray_app; import warp_exclusion; import traffic_monitor; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 提交**

```bash
git add core/command.py tray_app.py traffic_monitor.py warp_exclusion.py
git commit -m "refactor: 统一命令执行到 core/command.py"
```

---

### Task 3: 创建 core/webview.py — WebView 窗口辅助

**Files:**
- Create: `core/webview.py`
- Modify: `tray_app.py`（TrayApp 类的 show_exclusion、show_traffic_monitor、show_flow_monitor 方法简化）

**Interfaces:**
- Consumes: 无（webview 库）
- Produces:
  - `bring_window_to_top(title: str) -> bool` — 将窗口置顶并激活
  - `create_webview_window(api, title, html_file, width, height, resizable=True, frameless=True, on_closing_callback=None) -> webview.Window | None` — 创建 WebView 窗口

- [ ] **Step 1: 创建 core/webview.py**

```python
"""WebView 窗口创建辅助模块。

集中管理 Win32 窗口置顶逻辑和 WebView 窗口创建，
消除 tray_app.py 中 3 处重复的窗口创建代码。
"""
import ctypes
import logging
import webview

logger = logging.getLogger('tray_app')

# Win32 常量集中定义
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9


def bring_window_to_top(title):
    """将指定标题的窗口置顶并激活。
    
    Args:
        title: 窗口标题字符串
        
    Returns:
        True 如果找到并激活窗口，False 如果未找到窗口
    """
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetForegroundWindow(hwnd)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)
    return True


def create_webview_window(api, title, html_file, width, height,
                          resizable=True, frameless=True,
                          on_closing_callback=None):
    """创建 WebView 窗口。
    
    Args:
        api: ApiBridge 实例，暴露给前端 JS
        title: 窗口标题
        html_file: HTML 文件完整路径
        width: 窗口宽度
        height: 窗口高度
        resizable: 是否可调整大小
        frameless: 是否无边框
        on_closing_callback: 窗口关闭时的回调函数
        
    Returns:
        webview.Window 实例，或 None 如果创建失败
    """
    try:
        win = webview.create_window(
            title, html_file, js_api=api,
            width=width, height=height,
            resizable=resizable, frameless=frameless,
        )
        if on_closing_callback:
            win.events.closing += on_closing_callback
        return win
    except Exception as e:
        logger.error(f"create_webview_window: 创建窗口失败: {e}")
        return None
```

- [ ] **Step 2: 验证 webview.py 导入**

Run: `python -c "from core.webview import bring_window_to_top, create_webview_window; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.webview import bring_window_to_top, create_webview_window
```

- [ ] **Step 4: 修改 TrayApp.show_exclusion 方法**

找到 TrayApp 类中的 `show_exclusion` 方法（约第 2295 行），将其中的窗口创建逻辑改为使用 `create_webview_window`。

修改前（示例模式，实际代码见 tray_app.py）：
```python
def show_exclusion(self):
    # ... 检查窗口是否已存在 ...
    if self._exclusion_window:
        # ... 置顶逻辑（重复代码）...
        return
    # ... 创建窗口的重复代码 ...
    self._exclusion_window = webview.create_window(...)
    self._exclusion_window.events.closing += ...
```

修改后：
```python
def show_exclusion(self):
    if self._exclusion_window:
        if bring_window_to_top('WARP排除管理'):
            return
    self._exclusion_window = create_webview_window(
        self.api, 'WARP排除管理', get_resource_path('warp_exclusion.html'),
        520, 700, on_closing_callback=lambda: setattr(self, '_exclusion_window', None)
    )
```

注意：保留原始方法中的其他逻辑（如 `bring_window_to_top` 调用前的检查）。具体实现时，阅读原 `show_exclusion` 方法完整代码，提取窗口创建部分替换为 `create_webview_window` 调用，保留窗口位置保存等逻辑。

- [ ] **Step 5: 修改 TrayApp.show_traffic_monitor 和 show_flow_monitor 方法**

同理，将这两个方法（约第 2355 行和第 2413 行）中的窗口创建逻辑替换为 `create_webview_window` 调用。

- [ ] **Step 6: 验证 tray_app.py 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 提交**

```bash
git add core/webview.py tray_app.py
git commit -m "refactor: 提取 WebView 窗口辅助到 core/webview.py"
```

---

### Task 4: 创建 core/network.py — 网络检测

**Files:**
- Create: `core/network.py`
- Modify: `tray_app.py`（删除 9 个网络相关函数）

**Interfaces:**
- Consumes: `core.command.run_command`
- Produces:
  - `scan_wifi_networks() -> list[dict]`
  - `get_wifi_interface_name() -> str`
  - `get_local_ip() -> str`
  - `get_mac_address() -> str`
  - `get_current_wifi_ssid() -> str | None`
  - `wait_for_network_ready(portal_ip, portal_port='801', max_retries=5) -> bool`
  - `_wait_for_ipv6_ready(max_retries=8) -> bool`
  - `is_warp_connected() -> bool`
  - `_check_internet() -> bool`（从 ApiBridge 抽出）

- [ ] **Step 1: 创建 core/network.py**

```python
"""网络检测模块。

包含 WiFi 扫描、IP/MAC 获取、IPv6 就绪检测、WARP 连接状态检测等功能。
"""
import logging
import subprocess

from core.command import run_command

logger = logging.getLogger('tray_app')


def scan_wifi_networks():
    """扫描可用的 WiFi 网络，返回网络信息列表"""
    # 从 tray_app.py:441-450 移动


def get_wifi_interface_name():
    """获取 WiFi 接口名称"""
    # 从 tray_app.py:452-458 移动


def get_local_ip():
    """获取本机 IP 地址（过滤 172.16. 前缀）"""
    # 从 tray_app.py:460-488 移动
    # 修复第 487 行的 bare except: → except Exception:


def get_mac_address():
    """获取本机 MAC 地址"""
    # 从 tray_app.py:490-497 移动


def get_current_wifi_ssid():
    """获取当前连接的 WiFi SSID"""
    # 从 tray_app.py:978-986 移动


def wait_for_network_ready(portal_ip, portal_port='801', max_retries=5):
    """等待网络就绪"""
    # 从 tray_app.py:570-592 移动


def _wait_for_ipv6_ready(max_retries=8):
    """等待 IPv6 就绪"""
    # 从 tray_app.py:594-616 移动


def is_warp_connected():
    """检测 WARP 是否已连接（warp-cli + NetAdapter 双重检测）"""
    # 从 tray_app.py:862-877 移动


def _check_internet():
    """检测互联网连接是否正常"""
    # 从 ApiBridge._check_internet 方法移动（约 tray_app.py:1759 附近）
    # 修复 bare except: → except Exception:
```

**实现细节**：将 tray_app.py 中上述函数的完整代码移动到 `core/network.py`。修改第 487 行和 ApiBridge._check_internet 中的 bare `except:` 为 `except Exception:`。

注意：`get_local_ip` 中的 `import socket` 等局部导入保留在函数内部。`is_warp_connected` 中引用的 `get_warp_cli` 需要从 `core.warp_manager` 导入（但为避免循环依赖，在函数内部使用延迟导入：`from core.warp_manager import get_warp_cli`）。

- [ ] **Step 2: 验证 network.py 导入**

Run: `python -c "from core.network import scan_wifi_networks, get_local_ip, is_warp_connected; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 删除旧函数，添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.network import (
    scan_wifi_networks, get_wifi_interface_name, get_local_ip,
    get_mac_address, get_current_wifi_ssid, wait_for_network_ready,
    _wait_for_ipv6_ready, is_warp_connected, _check_internet,
)
```

删除 tray_app.py 中：
- 第 441-450 行：`scan_wifi_networks`
- 第 452-458 行：`get_wifi_interface_name`
- 第 460-488 行：`get_local_ip`
- 第 490-497 行：`get_mac_address`
- 第 570-592 行：`wait_for_network_ready`
- 第 594-616 行：`_wait_for_ipv6_ready`
- 第 862-877 行：`is_warp_connected`
- 第 978-986 行：`get_current_wifi_ssid`
- ApiBridge 中的 `_check_internet` 方法

注意：ApiBridge 中调用 `self._check_internet()` 的地方需要改为调用模块级 `_check_internet()`。

- [ ] **Step 4: 验证 tray_app.py 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 提交**

```bash
git add core/network.py tray_app.py
git commit -m "refactor: 提取网络检测到 core/network.py"
```

---

### Task 5: 创建 core/warp_manager.py — WARP 管理

**Files:**
- Create: `core/warp_manager.py`
- Modify: `tray_app.py`（删除 8 个 WARP 相关函数）

**Interfaces:**
- Consumes: `core.command.run_command`、`core.command.run_elevated_powershell`、`core.state._conf_json_backup`、`core.state._tray_app_instance`
- Produces:
  - `get_warp_cli() -> str | None`
  - `connect_warp(force_restart=False) -> bool`
  - `disconnect_warp(full=True) -> tuple[bool, str]`
  - `_set_warp_masque_mode(warp_cli, enable) -> bool`
  - `_set_warp_endpoint_ipv6(enable) -> bool`
  - `update_tray_icon(success, message='') -> None`
  - `update_tray_icon_restore(success, message='') -> None`

- [ ] **Step 1: 创建 core/warp_manager.py**

```python
"""WARP 连接管理模块。

包含 WARP 客户端的连接、断开、MASQUE 模式配置、IPv6 端点设置等功能。
"""
import json
import logging

import core.state
from core.command import run_command, run_elevated_powershell

logger = logging.getLogger('tray_app')


def get_warp_cli():
    """查找 warp-cli 可执行文件路径"""
    # 从 tray_app.py:367-392 移动


def disconnect_warp(full=True):
    """断开 WARP 连接"""
    # 从 tray_app.py:499-532 移动


def _set_warp_masque_mode(warp_cli, enable):
    """设置 WARP MASQUE 模式"""
    # 从 tray_app.py:732-750 移动


def _set_warp_endpoint_ipv6(enable):
    """设置 WARP IPv6 端点"""
    # 从 tray_app.py:752-786 移动
    # 注意：删除 global _conf_json_backup 声明
    # 读取时用 core.state._conf_json_backup
    # 赋值时用 core.state._conf_json_backup = ...
    # 例如：global _conf_json_backup 删除
    #       _conf_json_backup = data 改为 core.state._conf_json_backup = data
    #       if _conf_json_backup: 改为 if core.state._conf_json_backup:


def connect_warp(force_restart=False):
    """连接 WARP"""
    # 从 tray_app.py:788-801 移动


def _connect_warp_inner(warp_cli):
    """WARP 连接内部实现"""
    # 从 tray_app.py:803-860 移动


def update_tray_icon(success, message=''):
    """更新托盘图标状态"""
    # 从 tray_app.py:879-889 移动
    # 注意：引用 _tray_app_instance 改为 core.state._tray_app_instance


def update_tray_icon_restore(success, message=''):
    """更新托盘图标状态（恢复模式）"""
    # 从 tray_app.py:891-901 移动
    # 注意：引用 _tray_app_instance 改为 core.state._tray_app_instance
```

**实现细节**：
- 将 tray_app.py 中上述函数的完整代码移动到 `core/warp_manager.py`
- `_set_warp_endpoint_ipv6`（第 753 行）：删除 `global _conf_json_backup` 声明，将所有 `_conf_json_backup` 引用改为 `core.state._conf_json_backup`
- `update_tray_icon` 和 `update_tray_icon_restore`：将所有 `_tray_app_instance` 引用改为 `core.state._tray_app_instance`

- [ ] **Step 2: 验证 warp_manager.py 导入**

Run: `python -c "from core.warp_manager import get_warp_cli, connect_warp, disconnect_warp, update_tray_icon; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 删除旧函数，添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.warp_manager import (
    get_warp_cli, connect_warp, disconnect_warp,
    _set_warp_masque_mode, _set_warp_endpoint_ipv6,
    update_tray_icon, update_tray_icon_restore,
)
```

删除 tray_app.py 中：
- 第 367-392 行：`get_warp_cli`
- 第 499-532 行：`disconnect_warp`
- 第 732-750 行：`_set_warp_masque_mode`
- 第 752-786 行：`_set_warp_endpoint_ipv6`
- 第 788-801 行：`connect_warp`
- 第 803-860 行：`_connect_warp_inner`
- 第 879-889 行：`update_tray_icon`
- 第 891-901 行：`update_tray_icon_restore`

- [ ] **Step 4: 验证 tray_app.py 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 提交**

```bash
git add core/warp_manager.py tray_app.py
git commit -m "refactor: 提取 WARP 管理到 core/warp_manager.py"
```

---

### Task 6: 创建 core/auth.py — 认证流程（含安全修复）

**Files:**
- Create: `core/auth.py`
- Modify: `tray_app.py`（删除 9 个认证相关函数）

**Interfaces:**
- Consumes: `core.command.run_command`、`core.state._auth_cancelled`、`core.state._auth_lock`、`core.state._tray_app_instance`、`core.network.get_local_ip`、`core.network.get_mac_address`、`core.network.wait_for_network_ready`、`core.network._wait_for_ipv6_ready`、`core.warp_manager.connect_warp`、`core.warp_manager.disconnect_warp`、`core.warp_manager.update_tray_icon`
- Produces:
  - `portal_login() -> tuple[bool, str]`
  - `portal_logout() -> tuple[bool, str]`
  - `disable_ipv4(interface_name) -> None`
  - `enable_ipv4(interface_name) -> None`
  - `_push_auth_progress(step, total, message, status='running', action='auth') -> None`
  - `_check_cancel() -> bool`
  - `_interruptible_sleep(seconds, check_interval=0.5) -> bool`
  - `run_auth_task() -> None`
  - `run_restore_task() -> None`

- [ ] **Step 1: 创建 core/auth.py**

```python
"""认证流程模块。

包含校园网门户认证、IPv4 禁用/启用、认证任务编排等功能。
"""
import time
import logging

import core.state
from core.state import _auth_cancelled, _auth_lock
from core.command import run_command
from core.network import get_local_ip, get_mac_address, wait_for_network_ready, _wait_for_ipv6_ready
from core.warp_manager import connect_warp, disconnect_warp, update_tray_icon, update_tray_icon_restore

logger = logging.getLogger('tray_app')


def _js_escape(s):
    """JS 字符串转义"""
    # 从 tray_app.py:908-909 移动


def _is_cancelled():
    """检查认证是否已取消"""
    # 从 tray_app.py:911-912 移动


def _check_cancel():
    """检查取消状态，如果已取消则抛出 ValueError"""
    # 从 tray_app.py:914-918 移动


def _interruptible_sleep(seconds, check_interval=0.5):
    """可中断的睡眠"""
    # 从 tray_app.py:920-928 移动


def disable_ipv4(interface_name):
    """禁用指定接口的 IPv4"""
    # 从 tray_app.py:535-550 移动


def enable_ipv4(interface_name):
    """启用指定接口的 IPv4"""
    # 从 tray_app.py:553-568 移动


def portal_login():
    """执行门户认证"""
    # 从 tray_app.py:618-681 移动
    # ⚠️ 安全修复：
    # 1. 第 646 行：删除 password_prefix 部分，只保留 password_len
    # 2. 第 647 行：删除 _pwd_encrypted 日志行
    # 3. 第 652 行：删除记录 URL 中密码参数的日志行


def portal_logout():
    """执行门户注销"""
    # 从 tray_app.py:683-728 移动


def _push_auth_progress(step, total, message, status='running', action='auth'):
    """推送认证进度到前端"""
    # 从 tray_app.py:1132-1139 移动
    # 注意：引用 _tray_app_instance 改为 core.state._tray_app_instance


def run_auth_task():
    """执行认证任务（完整流程）"""
    # 从 tray_app.py:1141-1301 移动
    # 注意：CONFIG 需要从 tray_app 导入（延迟导入）


def run_restore_task():
    """执行恢复任务"""
    # 从 tray_app.py:1303-1357 移动
```

**安全修复（关键）**：在移动 `portal_login` 时，修改日志行：

修复前（tray_app.py:646-652）：
```python
logger.info(f"portal_login: username='{username}', password_len={len(password)}, password_prefix='{password[:20]}...' if len(password)>20 else password")
logger.info(f"portal_login: _pwd_encrypted={CONFIG.get('_pwd_encrypted')}")
query_string = urllib.parse.urlencode(params)
full_url = f"{url}?{query_string}"
logger.info(f"Login URL length: {len(full_url)}")
logger.info(f"Login URL: {full_url[:200]}...")
logger.info(f"Login URL (password param): user_password={params['user_password'][:30]}{'...' if len(params['user_password'])>30 else ''}")
```

修复后：
```python
logger.info(f"portal_login: username='{username}', password_len={len(password)}")
query_string = urllib.parse.urlencode(params)
full_url = f"{url}?{query_string}"
logger.info(f"Login URL length: {len(full_url)}")
```

删除的日志行：
1. `password_prefix=...` 部分（从第 646 行移除）
2. 第 647 行整行（`_pwd_encrypted` 日志）
3. 第 651 行整行（`Login URL: {full_url[:200]}...`，URL 中含密码参数）
4. 第 652 行整行（`user_password=` 日志）

**CONFIG 访问**：`portal_login`、`portal_logout`、`run_auth_task`、`run_restore_task` 中都引用了全局 `CONFIG`。为避免循环导入（tray_app.py 导入 core.auth，core.auth 又导入 tray_app 的 CONFIG），使用延迟导入：

```python
def portal_login():
    from tray_app import CONFIG
    username = CONFIG.get('username', '')
    # ...
```

- [ ] **Step 2: 验证 auth.py 导入**

Run: `python -c "from core.auth import portal_login, run_auth_task, run_restore_task; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 删除旧函数，添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.auth import (
    portal_login, portal_logout, disable_ipv4, enable_ipv4,
    _push_auth_progress, _check_cancel, _interruptible_sleep,
    run_auth_task, run_restore_task, _js_escape, _is_cancelled,
)
```

删除 tray_app.py 中：
- 第 535-550 行：`disable_ipv4`
- 第 553-568 行：`enable_ipv4`
- 第 618-681 行：`portal_login`
- 第 683-728 行：`portal_logout`
- 第 908-909 行：`_js_escape`
- 第 911-912 行：`_is_cancelled`
- 第 914-918 行：`_check_cancel`
- 第 920-928 行：`_interruptible_sleep`
- 第 1132-1139 行：`_push_auth_progress`
- 第 1141-1301 行：`run_auth_task`
- 第 1303-1357 行：`run_restore_task`

- [ ] **Step 4: 验证 tray_app.py 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 验证密码日志已清除**

Run: `python -c "from core.auth import portal_login; import inspect; src = inspect.getsource(portal_login); assert 'password_prefix' not in src, '密码前缀日志未清除'; assert '_pwd_encrypted' not in src, '_pwd_encrypted日志未清除'; assert 'user_password=' not in src, 'URL密码日志未清除'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 提交**

```bash
git add core/auth.py tray_app.py
git commit -m "refactor: 提取认证流程到 core/auth.py，修复密码日志泄露"
```

---

### Task 7: 创建 core/startup.py — 启动与事件（含 os._exit 修复）

**Files:**
- Create: `core/startup.py`
- Modify: `tray_app.py`（删除 13 个启动/事件相关函数）

**Interfaces:**
- Consumes: `core.command.run_command`、`core.command.run_elevated_powershell`、`core.state`（多个变量）、`core.auth.run_auth_task`
- Produces:
  - `check_single_instance() -> bool`
  - `setup_startup_task() -> None`
  - `remove_startup_task() -> None`
  - `check_startup_status() -> bool`
  - `register_wifi_event_task() -> None`
  - `unregister_wifi_event_task() -> None`
  - `wifi_event_monitor() -> None`
  - `start_wifi_event_monitor() -> None`
  - `cleanup_wifi_event() -> None`
  - `signal_wifi_event() -> None`
  - `_create_event_with_acl(name) -> ctypes handle`
  - `check_startup_wifi_and_auth() -> None`
  - `_update_tray_status() -> None`
  - `elevate_if_needed() -> None`
  - `hide_console() -> None`
  - `_build_schtasks_tr(extra_args='') -> str`

- [ ] **Step 1: 创建 core/startup.py**

```python
"""启动与事件管理模块。

包含单例控制、开机自启、WiFi 事件监视、提权执行等功能。
"""
import sys
import ctypes
import logging

import core.state
from core.state import _auth_cancelled, WIFI_EVENT_NAME
from core.command import run_command, run_elevated_powershell

logger = logging.getLogger('tray_app')


def _create_event_with_acl(name):
    """创建带有 ACL 的事件对象"""
    # 从 tray_app.py:930-963 移动


def signal_wifi_event():
    """发送 WiFi 事件信号"""
    # 从 tray_app.py:965-976 移动
    # 注意：引用 _wifi_event_handle 改为 core.state._wifi_event_handle


def wifi_event_monitor():
    """WiFi 事件监视线程"""
    # 从 tray_app.py:988-1061 移动
    # 注意：删除 global _wifi_event_handle 和 global _wifi_monitor_started
    # 引用改为 core.state._wifi_event_handle 和 core.state._wifi_monitor_started
    # 赋值改为 core.state._wifi_event_handle = ... 和 core.state._wifi_monitor_started = ...
    # 调用 run_auth_task 使用延迟导入：from core.auth import run_auth_task


def start_wifi_event_monitor():
    """启动 WiFi 事件监视"""
    # 从 tray_app.py:1065-1073 移动
    # 注意：删除 global _wifi_monitor_started
    # 赋值改为 core.state._wifi_monitor_started = ...


def check_startup_wifi_and_auth():
    """开机时检查 WiFi 并认证"""
    # 从 tray_app.py:1075-1104 移动


def _update_tray_status():
    """更新托盘状态"""
    # 从 tray_app.py:1106-1122 移动


def cleanup_wifi_event():
    """清理 WiFi 事件"""
    # 从 tray_app.py:1124-1130 移动


def _build_schtasks_tr(extra_args=''):
    """构建 schtasks /tr 参数"""
    # 从 tray_app.py:1359-1371 移动


def setup_startup_task():
    """设置开机自启任务"""
    # 从 tray_app.py:1373-1387 移动


def register_wifi_event_task():
    """注册 WiFi 事件任务"""
    # 从 tray_app.py:1389-1424 移动


def unregister_wifi_event_task():
    """注销 WiFi 事件任务"""
    # 从 tray_app.py:1426-1431 移动
    # 修复第 1430 行 bare except: → except Exception:


def check_startup_status():
    """检查自启状态"""
    # 从 tray_app.py:1433-1437 移动


def remove_startup_task():
    """移除自启任务"""
    # 从 tray_app.py:1439-1445 移动


def hide_console():
    """隐藏控制台窗口"""
    # 从 tray_app.py:1447-1459 移动


def elevate_if_needed():
    """如果需要则提权执行"""
    # 从 tray_app.py:1461-1495 移动
    # ⚠️ 修复：第 1491 行 os._exit(0) → sys.exit(0)
    # 修复第 1489 行 bare except: → except Exception:


def check_single_instance():
    """检查是否已有实例运行"""
    # 从 tray_app.py:67-85 移动
    # 注意：删除 global TRAY_MUTEX 声明
    # 赋值改为 core.state.TRAY_MUTEX = handle
    # 读取改为 core.state.TRAY_MUTEX
```

**安全修复（关键）**：在移动 `elevate_if_needed` 时，修改退出逻辑：

修复前（tray_app.py:1491）：
```python
            os._exit(0)
```

修复后：
```python
            sys.exit(0)
```

确保 `core/startup.py` 顶部已导入 `sys`。

- [ ] **Step 2: 验证 startup.py 导入**

Run: `python -c "from core.startup import check_single_instance, setup_startup_task, elevate_if_needed, wifi_event_monitor; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 修改 tray_app.py — 删除旧函数，添加导入**

在 tray_app.py 顶部导入区添加：
```python
from core.startup import (
    check_single_instance, setup_startup_task, remove_startup_task,
    check_startup_status, register_wifi_event_task, unregister_wifi_event_task,
    wifi_event_monitor, start_wifi_event_monitor, cleanup_wifi_event,
    signal_wifi_event, _create_event_with_acl, check_startup_wifi_and_auth,
    _update_tray_status, elevate_if_needed, hide_console, _build_schtasks_tr,
)
```

删除 tray_app.py 中：
- 第 67-85 行：`check_single_instance`
- 第 930-963 行：`_create_event_with_acl`
- 第 965-976 行：`signal_wifi_event`
- 第 988-1061 行：`wifi_event_monitor`
- 第 1065-1073 行：`start_wifi_event_monitor`
- 第 1075-1104 行：`check_startup_wifi_and_auth`
- 第 1106-1122 行：`_update_tray_status`
- 第 1124-1130 行：`cleanup_wifi_event`
- 第 1359-1371 行：`_build_schtasks_tr`
- 第 1373-1387 行：`setup_startup_task`
- 第 1389-1424 行：`register_wifi_event_task`
- 第 1426-1431 行：`unregister_wifi_event_task`
- 第 1433-1437 行：`check_startup_status`
- 第 1439-1445 行：`remove_startup_task`
- 第 1447-1459 行：`hide_console`
- 第 1461-1495 行：`elevate_if_needed`

- [ ] **Step 4: 验证 tray_app.py 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 验证 os._exit 已从 startup.py 清除**

Run: `python -c "import core.startup; import inspect; src = inspect.getsource(core.startup); assert 'os._exit' not in src, 'os._exit 仍存在于 startup.py'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 提交**

```bash
git add core/startup.py tray_app.py
git commit -m "refactor: 提取启动与事件到 core/startup.py，修复 os._exit"
```

---

### Task 8: 瘦身 tray_app.py — 修复残留问题

**Files:**
- Modify: `tray_app.py`（修复 on_exit 中的 os._exit、残留 bare except、清理无用导入）

**Interfaces:**
- Consumes: 所有 core 模块
- Produces: 瘦身后的 tray_app.py（~500 行）

- [ ] **Step 1: 修复 on_exit 中的 os._exit(0)**

注意：`global _tray_app_instance` 和 `global TRAY_MUTEX` 声明已在 Task 1 中修复为 `core.state._tray_app_instance` 和 `core.state.TRAY_MUTEX`。本步只修复 `os._exit(0)`。

在 tray_app.py 的 `on_exit` 函数末尾（约第 2149 行），将：
```python
    # 强制退出，防止残留线程阻止进程退出
    os._exit(0)
```
改为：
```python
    # 退出应用，允许 atexit 和 finally 执行清理
    sys.exit(0)
```

确保 tray_app.py 顶部已导入 `sys`（第 2 行已有 `import sys`，无需额外添加）。

- [ ] **Step 1b: 验证 global 声明已清除**

Run: `python -c "src = open('tray_app.py', encoding='utf-8').read(); assert 'global _tray_app_instance' not in src, '仍有 global _tray_app_instance'; assert 'global TRAY_MUTEX' not in src, '仍有 global TRAY_MUTEX'; print('OK')"`
Expected: 输出 `OK`

如果失败，说明 Task 1 中的 global 声明修复有遗漏，需在此补充修复。

- [ ] **Step 2: 修复残留的 bare except**

搜索 tray_app.py 中所有剩余的 `except:`（之前 grep 发现的行号可能因前序任务的删除而变化，需重新搜索）。

Run: `python -c "import re; src = open('tray_app.py', encoding='utf-8').read(); lines = src.split('\n'); [print(f'{i+1}: {lines[i]}') for i,l in enumerate(lines) if re.match(r'\s*except\s*:', l)]"`

将所有 `except:` 改为 `except Exception:`。

已知的位置（行号可能已变化）：
- 原 136 行（log_func_call 中）
- 原 204 行（run_command 中，已迁移到 core/command.py，应已修复）
- 原 302、309 行（已迁移）
- 原 333、344、352、358、364 行（已迁移）
- 原 487 行（已迁移到 core/network.py）
- 原 1430 行（已迁移到 core/startup.py）
- 原 1489 行（已迁移到 core/startup.py）
- 原 1759 行（ApiBridge 中，可能仍残留）

重新搜索后修复剩余的 bare except。

- [ ] **Step 3: 验证 tray_app.py 无 bare except**

Run: `python -c "import re; src = open('tray_app.py', encoding='utf-8').read(); assert not re.search(r'except\s*:', src), '仍有 bare except'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 验证 tray_app.py 无 os._exit**

Run: `python -c "src = open('tray_app.py', encoding='utf-8').read(); assert 'os._exit' not in src, '仍有 os._exit'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 验证 tray_app.py 无密码日志**

Run: `python -c "src = open('tray_app.py', encoding='utf-8').read(); assert 'password_prefix' not in src, '仍有密码前缀日志'; assert '_pwd_encrypted' not in src, '仍有 _pwd_encrypted 日志'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 清理无用导入**

检查 tray_app.py 顶部的 import 语句，删除不再使用的导入。例如：
- `import tempfile` — 如果 run_command 已迁移，可能不再需要
- `import uuid` — 同上
- `import functools` — 如果 log_func_call 已迁移或不再使用

Run: `python -c "import tray_app; print('OK')"` 确认导入仍正常。

- [ ] **Step 7: 验证最终导入**

Run: `python -c "import tray_app; import warp_exclusion; import traffic_monitor; from core.state import _auth_cancelled; from core.command import run_command; from core.webview import create_webview_window; from core.network import get_local_ip; from core.warp_manager import connect_warp; from core.auth import portal_login; from core.startup import check_single_instance; print('ALL OK')"`
Expected: 输出 `ALL OK`

- [ ] **Step 8: 提交**

```bash
git add tray_app.py
git commit -m "refactor: 修复 tray_app.py 残留的 os._exit 和 bare except"
```

---

### Task 9: 更新 CampusAuth.spec

**Files:**
- Modify: `CampusAuth.spec:9`

**Interfaces:**
- Consumes: 无
- Produces: 更新后的 PyInstaller 打包配置

- [ ] **Step 1: 更新 hiddenimports**

在 `CampusAuth.spec` 第 9 行的 `hiddenimports` 列表中添加 core 模块：

修改前：
```python
    hiddenimports=['warp_exclusion', 'traffic_monitor', 'dns', 'dns.resolver', 'dns.exception', 'dns.rdatatype', 'dns.rdataclass', 'dns.rcode'],
```

修改后：
```python
    hiddenimports=[
        'warp_exclusion', 'traffic_monitor',
        'dns', 'dns.resolver', 'dns.exception', 'dns.rdatatype', 'dns.rdataclass', 'dns.rcode',
        'core', 'core.state', 'core.command', 'core.webview', 'core.network',
        'core.warp_manager', 'core.auth', 'core.startup',
    ],
```

- [ ] **Step 2: 验证 spec 文件语法**

Run: `python -c "import ast; ast.parse(open('CampusAuth.spec', encoding='utf-8').read()); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 提交**

```bash
git add CampusAuth.spec
git commit -m "build: 更新 CampusAuth.spec 的 hiddenimports"
```

---

### Task 10: 最终验证与行数统计

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 验证所有模块导入**

Run: `python -c "import tray_app; import warp_exclusion; import traffic_monitor; import core.state; import core.command; import core.webview; import core.network; import core.warp_manager; import core.auth; import core.startup; print('ALL IMPORTS OK')"`
Expected: 输出 `ALL IMPORTS OK`

- [ ] **Step 2: 验证安全修复**

Run: `python -c "import inspect, core.auth, core.startup, tray_app; \
auth_src = inspect.getsource(core.auth); \
startup_src = inspect.getsource(core.startup); \
tray_src = inspect.getsource(tray_app); \
assert 'password_prefix' not in auth_src, 'auth.py 仍有密码前缀日志'; \
assert '_pwd_encrypted' not in auth_src, 'auth.py 仍有 _pwd_encrypted 日志'; \
assert 'user_password=' not in auth_src, 'auth.py 仍有 URL 密码日志'; \
assert 'os._exit' not in startup_src, 'startup.py 仍有 os._exit'; \
assert 'os._exit' not in tray_src, 'tray_app.py 仍有 os._exit'; \
import re; \
assert not re.search(r'except\s*:', tray_src), 'tray_app.py 仍有 bare except'; \
assert not re.search(r'except\s*:', inspect.getsource(warp_exclusion)), 'warp_exclusion.py 仍有 bare except'; \
print('ALL SECURITY CHECKS PASSED')"`
Expected: 输出 `ALL SECURITY CHECKS PASSED`

- [ ] **Step 3: 统计行数**

Run: `bash "wc -l tray_app.py core/*.py warp_exclusion.py traffic_monitor.py"`
Expected: tray_app.py 约 500 行，core/ 各模块行数合理

- [ ] **Step 4: 最终提交（如有清理）**

如果前面步骤中有未提交的清理，在此提交。否则跳过。

```bash
git add -A
git commit -m "refactor: 代码结构重构完成" || echo "无需提交"
```

---

## 验证总结

### 功能验证清单

完成所有任务后，手动验证以下功能（需要 Windows 环境和管理员权限）：

- [ ] `python tray_app.py` 能正常启动，托盘图标显示
- [ ] 右键托盘菜单，点击"设置"，settings.html 窗口正常打开
- [ ] 右键托盘菜单，点击"WARP排除管理"，warp_exclusion.html 窗口正常打开
- [ ] 右键托盘菜单，点击"流量监控"，traffic_monitor.html 窗口正常打开
- [ ] 右键托盘菜单，点击"认证"，认证流程正常执行
- [ ] 点击"退出"，应用正常退出，无残留进程

### 预期结果对比

| 指标 | 重构前 | 重构后 |
|------|--------|--------|
| tray_app.py 行数 | 2733 | ~500 |
| 最大单文件行数 | 2733 | ~350（auth.py） |
| 命令执行实现数 | 5 处重复 | 1 处（core/command.py） |
| WebView 窗口创建重复 | 3 处 | 1 处（core/webview.py） |
| 密码日志泄露点 | 3 处 | 0 |
| bare except 数量 | ~15 处 | 0 |
| os._exit(0) 数量 | 2 处 | 0 |
| HTML 前端改动 | - | 0 |
| ApiBridge 接口改动 | - | 0 |
