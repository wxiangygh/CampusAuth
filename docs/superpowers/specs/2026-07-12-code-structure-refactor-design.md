# CampusAuth 代码结构重构设计

**日期**：2026-07-12
**范围**：tray_app.py 按职责拆分为 core/ 包 + 顺手修复同文件中的安全问题
**方案**：方案 B（按职责拆分模块）

## 背景

CampusAuth 是 Windows 托盘应用，提供校园网认证、WARP 排除管理、流量监控功能。当前代码存在以下问题：

- `tray_app.py` 膨胀至 2733 行，承担十余种职责
- 命令执行逻辑在 5 处重复实现
- WebView 窗口创建逻辑在 3 处重复
- 密码明文写入日志（3 处）
- 大量 bare `except:` 吞没异常（~15 处）
- `os._exit(0)` 绕过清理逻辑（2 处）
- DPAPI 加密功能名存实亡（git log 声称实现但代码中无实际逻辑）

## 目标

1. 将 `tray_app.py` 拆分为 7 个职责单一的 core 模块，主文件瘦身到 ~500 行
2. 消除命令执行和 WebView 窗口创建的重复代码
3. 顺手修复接触到的安全问题：密码日志、bare except、os._exit
4. 保持 HTML 前端和 ApiBridge 接口完全不变

## 非目标（YAGNI）

以下内容明确排除，避免范围蔓延：

- 不合并 `tray_config.json` 和 `warp_exclusion_config.json`
- 不引入 `Config` 类替代全局 `CONFIG` 变量
- 不添加单元测试
- 不添加 `requirements.txt` / `ruff` 配置
- 不重构 `warp_exclusion.py` 和 `traffic_monitor.py` 内部结构（仅改命令调用来源）
- 不实现 DPAPI 加密
- 不修改任何 HTML 前端文件

## 目标结构

```
d:\project_code\ipv6\
├── core/                       # 新增包
│   ├── __init__.py
│   ├── command.py              # 统一命令执行
│   ├── webview.py              # WebView 窗口辅助
│   ├── network.py              # 网络检测
│   ├── auth.py                 # 认证流程
│   ├── warp_manager.py         # WARP 连接管理
│   ├── startup.py              # 开机自启与 WiFi 事件
│   └── state.py                # 全局状态
├── tray_app.py                 # 瘦身到 ~500 行
├── warp_exclusion.py           # 保持不变（仅改命令调用）
├── traffic_monitor.py          # 保持不变（仅改命令调用）
├── settings.html               # 保持不变
├── warp_exclusion.html         # 保持不变
├── traffic_monitor.html        # 保持不变
├── traffic_flow.html           # 保持不变
└── CampusAuth.spec             # 更新 hiddenimports
```

## 模块职责与依赖

| 模块 | 职责 | 依赖 |
|------|------|------|
| `core/state.py` | 持有全局状态：`_auth_cancelled`、`_auth_lock`、`_tray_app_instance`、`_wifi_event_handle`、`_wifi_monitor_started`、`_conf_json_backup` | 无 |
| `core/command.py` | `run_command`、`run_elevated_powershell`、`run_powershell_simple`；支持取消；统一 `STARTUPINFO` 模式 | `state` |
| `core/webview.py` | `bring_window_to_top(title)`、`create_webview_window(...)`；Win32 常量集中 | 无 |
| `core/network.py` | WiFi 扫描、IP/MAC 获取、IPv6 就绪检测、`is_warp_connected` | `command` |
| `core/warp_manager.py` | `connect_warp`、`disconnect_warp`、MASQUE 配置、`get_warp_cli`、`update_tray_icon` | `command`、`state`、`network` |
| `core/auth.py` | `portal_login`、`portal_logout`、`run_auth_task`、`run_restore_task`、`_push_auth_progress`、IPv4 启用/禁用 | `command`、`state`、`network`、`warp_manager` |
| `core/startup.py` | `setup_startup_task`、`register_wifi_event_task`、`wifi_event_monitor`、`check_single_instance`、`elevate_if_needed` | `command`、`state`、`auth` |
| `tray_app.py` | `TrayApp` 类、`ApiBridge` 类、`main()`、`load_config`/`save_config` | 上述所有 |

### 循环依赖处理

`auth.py` 需要更新托盘图标（`update_tray_icon`），`startup.py` 需要触发认证（`run_auth_task`）。通过 `state.py` 中转：

- `state.py` 只持有**引用**，不持有**逻辑**
- `warp_manager.py` 中的 `update_tray_icon` 通过 `state._tray_app_instance` 访问图标
- `startup.py` 中的 `wifi_event_monitor` 通过 `from core.auth import run_auth_task` 直接导入（无循环，因为 auth 不反向导入 startup）

### 配置管理

`load_config` / `save_config_to_file` 保留在 `tray_app.py` 中。其他模块需要配置时调用 `load_config()`（保持现有模式）。

### 全局状态

`core/state.py` 内容：

```python
import threading

# 认证流程控制
_auth_cancelled = threading.Event()
_auth_lock = threading.Lock()

# 托盘应用实例引用（由 tray_app.py 设置）
_tray_app_instance = None

# WiFi 事件监视
_wifi_event_handle = None
_wifi_monitor_started = False

# WARP 配置备份
_conf_json_backup = None
```

## 各模块详细设计

### core/command.py — 统一命令执行

合并 5 处重复实现为 3 个函数：

```python
def run_command(cmd, shell=True, timeout=30):
    """执行命令，返回 (exit_code, stdout, stderr)。
    支持取消（通过 _auth_cancelled 事件）。
    使用临时文件捕获输出，避免管理员权限下的管道问题。
    """

def run_elevated_powershell(ps_command, timeout=30):
    """提权执行 PowerShell 命令，返回 (exit_code, stdout, stderr)"""

def run_powershell_simple(cmd, timeout=15):
    """简单的 PowerShell 命令执行（无取消、无临时文件）。
    用于不需要取消的快速命令。
    合并 traffic_monitor._run_ps 和 warp_exclusion._run_command。
    """
```

**安全修复**：
- 移除所有 `except:` → `except Exception:`
- 临时文件删除失败时记录 warning 而非静默忽略

**消除重复**：
- `run_command_os_system` 合并到 `run_command`
- `traffic_monitor._run_ps` 和 `warp_exclusion._run_command` 改为调用 `run_powershell_simple`

### core/webview.py — WebView 窗口辅助

```python
# Win32 常量集中定义
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9

def bring_window_to_top(title):
    """将指定标题的窗口置顶并激活"""

def create_webview_window(api, title, html_file, width, height,
                          resizable=True, frameless=True,
                          on_closing_callback=None):
    """创建 WebView 窗口。
    若窗口已存在则置顶显示；否则创建新窗口。
    """
```

### core/network.py — 网络检测

迁移以下函数（逻辑保持不变，仅改 import 来源）：
- `scan_wifi_networks()`
- `get_wifi_interface_name()`
- `get_local_ip()` — 保留过滤 `172.16.` 前缀的逻辑
- `get_mac_address()`
- `get_current_wifi_ssid()`
- `wait_for_network_ready(portal_ip, portal_port)`
- `_wait_for_ipv6_ready(max_retries)`
- `is_warp_connected()` — 包含 warp-cli + NetAdapter 双重检测
- `_check_internet()` — 从 ApiBridge 抽出

### core/warp_manager.py — WARP 管理

迁移：
- `get_warp_cli()`
- `connect_warp(force_restart)` / `_connect_warp_inner(warp_cli)`
- `disconnect_warp(full=True)`
- `_set_warp_masque_mode(warp_cli, enable)`
- `_set_warp_endpoint_ipv6(enable)` — `_conf_json_backup` 从 state.py 导入
- `update_tray_icon(success, message)` / `update_tray_icon_restore(success, message)`

### core/auth.py — 认证流程

迁移：
- `portal_login()` / `portal_logout()`
- `run_auth_task()` / `run_restore_task()`
- `_push_auth_progress(step, total, message, status, action)`
- `_check_cancel()` / `_interruptible_sleep(seconds)`
- `disable_ipv4(interface_name)` / `enable_ipv4(interface_name)`

**安全修复**：

```python
# 修复前（tray_app.py:646）
logger.info(f"portal_login: username='{username}', password_len={len(password)}, password_prefix='{password[:20]}'...")

# 修复后
logger.info(f"portal_login: username='{username}', password_len={len(password)}")
```

- 删除记录 URL 中密码参数的日志行（tray_app.py:652）
- 删除记录 `_pwd_encrypted` 状态的日志行（tray_app.py:647）

### core/startup.py — 启动与事件

迁移：
- `check_single_instance()`
- `setup_startup_task()` / `remove_startup_task()` / `check_startup_status()`
- `register_wifi_event_task()` / `unregister_wifi_event_task()`
- `wifi_event_monitor()` / `start_wifi_event_monitor()` / `cleanup_wifi_event()`
- `signal_wifi_event()`
- `_create_event_with_acl(name)`
- `elevate_if_needed()` — **修复 os._exit(0)**

**修复 os._exit**：

```python
def elevate_if_needed():
    # ...
    if ret > 32:
        import sys
        sys.exit(0)  # 替代 os._exit(0)
```

`on_exit` 中的 `os._exit(0)` 同样改为 `sys.exit(0)`，配合清理所有窗口和停止托盘。

### tray_app.py 瘦身后

保留内容（~500 行）：
- `get_resource_path(relative_path)`
- `load_config()` / `save_config_to_file(cfg)` / `CONFIG` 全局
- `create_icon(color)` / `ensure_app_icon()`
- `ApiBridge` 类（保持接口不变）
- `TrayApp` 类（瘦身，窗口创建调用 `core.webview`）
- `on_auth` / `on_restore` / `on_reauth` / `on_exit` / `on_show_log` 等回调
- `main()`

**ApiBridge 接口保持完全不变** — HTML 前端无需任何修改。

## 迁移策略

### 迁移顺序（自底向上）

按依赖关系从底层到顶层迁移，每步可独立验证：

1. 创建 `core/state.py`（无依赖）
2. 创建 `core/command.py`（依赖 state）
3. 创建 `core/webview.py`（无依赖）
4. 创建 `core/network.py`（依赖 command）
5. 创建 `core/warp_manager.py`（依赖 command, state, network）
6. 创建 `core/auth.py`（依赖 command, state, network, warp_manager）+ 安全修复
7. 创建 `core/startup.py`（依赖 command, state, auth）+ os._exit 修复
8. 瘦身 `tray_app.py`（改为从 core 导入）+ 修复 on_exit 中的 os._exit
9. 更新 `warp_exclusion.py` 和 `traffic_monitor.py` 的命令调用
10. 更新 `CampusAuth.spec` 的 hiddenimports

### 每步验证标准

1. **语法检查**：`python -c "import core.command"` 确认无导入错误
2. **功能等价**：运行 `python tray_app.py` 启动应用验证
3. **日志确认**：查看 `tray_app.log`，确认无异常报错

## 风险点与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 模块循环导入 | 应用无法启动 | 严格按依赖图分层；`state.py` 作为中介；必要时用延迟导入 |
| 全局状态丢失同步 | 取消功能失效 | `state.py` 中变量由 `tray_app.py` 初始化时设置；其他模块只读取 |
| PyInstaller 打包缺模块 | EXE 运行报错 | 更新 `.spec` 的 `hiddenimports`；打包后完整功能测试 |
| ApiBridge 接口变化 | HTML 前端调用失败 | **保持 ApiBridge 所有方法签名不变**；HTML 文件零修改 |
| `sys.exit(0)` 替换 `os._exit(0)` 后线程阻塞 | 应用无法退出 | 保留 `icon.stop()` 清理托盘；销毁所有 WebView 窗口；daemon 线程会随主线程退出 |

## 顺手修复清单

| 文件位置 | 问题 | 修复方式 |
|----------|------|----------|
| `tray_app.py:646` | 记录密码前缀 | 删除 `password_prefix=...` 部分 |
| `tray_app.py:652` | 记录 URL 中密码参数 | 整行删除 |
| `tray_app.py:647` | 记录 `_pwd_encrypted` 状态 | 删除（功能未实现） |
| `tray_app.py` 多处 | bare `except:` | 改为 `except Exception:` |
| `tray_app.py:1491,2149` | `os._exit(0)` | 改为 `sys.exit(0)` |
| `warp_exclusion.py` 多处 | bare `except:` | 改为 `except Exception:` |

## 预期结果

| 指标 | 重构前 | 重构后 |
|------|--------|--------|
| `tray_app.py` 行数 | 2733 | ~500 |
| 最大单文件行数 | 2733 | ~600（auth.py） |
| 命令执行实现数 | 5 处重复 | 1 处（core/command.py） |
| WebView 窗口创建重复 | 3 处 | 1 处（core/webview.py） |
| 密码日志泄露点 | 3 处 | 0 |
| bare `except:` 数量 | ~15 处 | 0 |
| `os._exit(0)` 数量 | 2 处 | 0 |
| HTML 前端改动 | - | 0 |
| ApiBridge 接口改动 | - | 0 |
