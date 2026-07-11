"""启动与事件管理模块。

包含单例控制、开机自启、WiFi 事件监视、提权执行等功能。
"""
import os
import sys
import ctypes
import ctypes.wintypes
import time
import threading
import traceback
import logging

import core.state
from core.state import WIFI_EVENT_NAME, _auth_lock
from core.command import run_command
from core.network import get_current_wifi_ssid, is_warp_connected, get_wifi_interface_name
from core.warp_manager import update_tray_icon, update_tray_icon_restore

logger = logging.getLogger('wifi_tray')


def check_single_instance():
    """检查是否已有实例运行，返回互斥锁句柄"""
    mutex_name = "Global\\WiFiAutoAuth_Mutex"
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    err = ctypes.get_last_error()
    if err == 183:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, 'CampusAuth')
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            logger.info("Another instance already running, bringing window to front")
            sys.exit(0)
        else:
            logger.info("Stale mutex detected (no window found), taking over")
            kernel32.CloseHandle(mutex)
            mutex = kernel32.CreateMutexW(None, True, mutex_name + "_v2")
    return mutex


def _create_event_with_acl(name):
    """创建带有 ACL 的事件对象"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ('nLength', ctypes.wintypes.DWORD),
            ('lpSecurityDescriptor', ctypes.c_void_p),
            ('bInheritHandle', ctypes.wintypes.BOOL),
        ]

    sd_size = 4096
    sd = ctypes.create_string_buffer(sd_size)
    if not advapi32.InitializeSecurityDescriptor(sd, 1):
        logger.error(f"InitializeSecurityDescriptor failed: {ctypes.get_last_error()}")
        return kernel32.CreateEventW(None, False, False, name)

    if not advapi32.SetSecurityDescriptorDacl(sd, True, None, False):
        logger.error(f"SetSecurityDescriptorDacl failed: {ctypes.get_last_error()}")
        return kernel32.CreateEventW(None, False, False, name)

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = ctypes.cast(sd, ctypes.c_void_p).value
    sa.bInheritHandle = False

    event = kernel32.CreateEventW(ctypes.byref(sa), False, False, name)
    if not event:
        err = ctypes.get_last_error()
        logger.error(f"CreateEventW with ACL failed (error={err}), trying default")
        event = kernel32.CreateEventW(None, False, False, name)
    if event:
        logger.info(f"Named event created: {name}")
    return event


def signal_wifi_event():
    """发送 WiFi 事件信号"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    EVENT_MODIFY_STATE = 0x0002
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, WIFI_EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)
        logger.info("WiFi event signal sent to running app")
        return True
    err = ctypes.get_last_error()
    logger.error(f"Failed to open WiFi event (error={err}), tray app may not be running")
    return False


def wifi_event_monitor():
    """WiFi 事件监视线程"""
    try:
        core.state._wifi_event_handle = _create_event_with_acl(WIFI_EVENT_NAME)
        if not core.state._wifi_event_handle:
            logger.error(f"Failed to create WiFi event handle: error={ctypes.get_last_error()}")
            return
        logger.info("WiFi event monitor thread started")
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # 延迟导入以避免循环依赖（放在循环外，避免每次事件触发都执行导入语句）
        from tray_app import load_config
        from core.auth import run_auth_task, run_restore_task
        while True:
            result = kernel32.WaitForSingleObject(core.state._wifi_event_handle, 0xFFFFFFFF)
            if result == 0:
                logger.info("WiFi connection event signal received")
                cfg = load_config()
                auto_auth = cfg.get('auto_auth', False)
                auto_restore = cfg.get('auto_restore', False)
                if not auto_auth and not auto_restore:
                    logger.info("Both auto_auth and auto_restore disabled, skipping")
                    _update_tray_status()
                    continue
                target_wifi = cfg.get('wifi_name', '')
                time.sleep(3)
                current_wifi = get_current_wifi_ssid()
                logger.info(f"Current WiFi: {current_wifi!r}, Target WiFi: {target_wifi!r}")
                if current_wifi == target_wifi and auto_auth:
                    if not target_wifi:
                        logger.info("No target WiFi configured, skipping")
                        continue
                    if is_warp_connected():
                        # 检查 IPv4 是否已禁用
                        interface_name = get_wifi_interface_name()
                        if interface_name:
                            ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
                            code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False)
                            if 'False' in output:
                                logger.info("WARP connected and IPv4 disabled, skipping auto-auth")
                                update_tray_icon(True, 'WARP已连接')
                                continue
                            else:
                                logger.info("WARP connected but IPv4 still enabled, will re-auth to fix")
                        else:
                            logger.info("WARP connected, skipping auto-auth (cannot check IPv4)")
                            update_tray_icon(True, 'WARP已连接')
                            continue
                    if _auth_lock.acquire(blocking=False):
                        try:
                            logger.info("Target WiFi matched, starting auto-auth")
                            success, msg = run_auth_task()
                            logger.info(f"Auto-auth result: {success}, {msg}")
                            update_tray_icon(success, msg)
                        finally:
                            _auth_lock.release()
                    else:
                        logger.info("Auth already in progress, skipping")
                elif current_wifi != target_wifi and auto_restore:
                    logger.info(f"auto_restore: non-target WiFi detected, restoring normal mode")
                    if _auth_lock.acquire(blocking=False):
                        try:
                            success, msg = run_restore_task()
                            logger.info(f"auto_restore result: {success}, {msg}")
                            if success:
                                update_tray_icon_restore(True, msg)
                            else:
                                logger.warning(f"auto_restore failed: {msg}")
                        finally:
                            _auth_lock.release()
                    else:
                        logger.info("Auth lock busy, skipping auto_restore")
                else:
                    _update_tray_status()
    except Exception as e:
        logger.error(f"wifi_event_monitor crashed: {e}\n{traceback.format_exc()}")
        core.state._wifi_monitor_started = False


def start_wifi_event_monitor():
    """启动 WiFi 事件监视"""
    if core.state._wifi_monitor_started:
        logger.info("WiFi event monitor already running, skipping")
        return
    core.state._wifi_monitor_started = True
    t = threading.Thread(target=wifi_event_monitor, daemon=True)
    t.start()
    logger.info("WiFi event monitor thread launched")


def check_startup_wifi_and_auth():
    """开机时检查 WiFi 并认证"""
    # 延迟导入以避免循环依赖
    from tray_app import load_config
    from core.auth import run_auth_task
    cfg = load_config()
    if not cfg.get('auto_auth'):
        _update_tray_status()
        return
    target_wifi = cfg.get('wifi_name', '')
    if not target_wifi:
        _update_tray_status()
        return
    # 先快速检测一次WARP状态，避免阻塞启动
    if is_warp_connected():
        logger.info("WARP already connected on startup, skipping auto-auth")
        update_tray_icon(True, 'WARP已连接')
        return

    current_wifi = get_current_wifi_ssid()
    logger.info(f"Startup WiFi check: current={current_wifi!r}, target={target_wifi!r}")
    if current_wifi == target_wifi:
        if _auth_lock.acquire(blocking=False):
            try:
                logger.info("Connected to target WiFi but WARP not connected, starting auto-auth")
                success, msg = run_auth_task()
                logger.info(f"Startup auto-auth result: {success}, {msg}")
                update_tray_icon(success, msg)
            finally:
                _auth_lock.release()
        else:
            logger.info("Auth already in progress on startup check")
    else:
        _update_tray_status()


def _update_tray_status():
    """更新托盘状态"""
    try:
        if not core.state._tray_app_instance or not core.state._tray_app_instance.icon:
            return
        # create_icon 仍由 tray_app.py 持有，延迟导入避免循环依赖
        from tray_app import create_icon
        status = core.state._tray_app_instance.api.check_network_status()
        s = status.get('status', 'disconnected')
        if s == 'connected' or s == 'partial':
            core.state._tray_app_instance.icon.icon = create_icon('orange')
            core.state._tray_app_instance.icon.title = status.get('message', 'WARP已连接')
        elif s == 'normal':
            core.state._tray_app_instance.icon.icon = create_icon('green')
            core.state._tray_app_instance.icon.title = '正常模式'
        else:
            core.state._tray_app_instance.icon.icon = create_icon('gray')
            core.state._tray_app_instance.icon.title = '未连接'
    except Exception as e:
        logger.error(f"_update_tray_status failed: {e}")


def cleanup_wifi_event():
    """清理 WiFi 事件句柄"""
    if core.state._wifi_event_handle:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(core.state._wifi_event_handle)
        core.state._wifi_event_handle = None
        logger.info("WiFi event handle released")


def _build_schtasks_tr(extra_args=''):
    """构建 schtasks /tr 参数"""
    # SCRIPT_DIR 仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import SCRIPT_DIR
    if getattr(sys, 'frozen', False):
        exe_path = sys.executable
        tr = exe_path
        if extra_args:
            tr += f' {extra_args}'
    else:
        python_exe = sys.executable
        script = str(SCRIPT_DIR / 'tray_app.py')
        tr = f'{python_exe} {script}'
        if extra_args:
            tr += f' {extra_args}'
    return tr


def setup_startup_task():
    """设置开机自启任务"""
    # 以下符号仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import is_admin, CONFIG, TASK_NAME_STARTUP
    if is_admin():
        logger.info("Already running as admin, setting up startup task")
        args = '--silent' if CONFIG.get('silent_startup') else ''
        tr_value = _build_schtasks_tr(args)
        cmd_str = f'schtasks /Create /TN "{TASK_NAME_STARTUP}" /TR "{tr_value}" /SC ONLOGON /RL HIGHEST /F'
        logger.info(f"setup_startup_task: cmd={cmd_str}")
        code, output, err = run_command(cmd_str)
        if code == 0:
            logger.info("Startup task created successfully")
            return True
        else:
            logger.error(f"Failed to create startup task: code={code}, output={output}, err={err}")
            return False
    return False


def register_wifi_event_task():
    """注册 WiFi 事件任务"""
    # CONFIG 仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import CONFIG
    if not CONFIG.get('wifi_name'):
        return False
    try:
        run_command('schtasks /Delete /TN WiFiAutoAuthEvent /F')
        logger.info("Old WiFi event task deleted (if existed)")
    except Exception:
        pass
    tr_value = _build_schtasks_tr('--wifi-event')
    event_channel = 'Microsoft-Windows-WLAN-AutoConfig/Operational'
    event_filter = "*[System[Provider[@Name='Microsoft-Windows-WLAN-AutoConfig'] and EventID=8001]]"
    cmd_str = f'schtasks /Create /TN "WiFiAutoAuthEvent" /TR "{tr_value}" /SC ONEVENT /EC "{event_channel}" /MO "{event_filter}" /RL HIGHEST /F'
    logger.info(f"register_wifi_event_task: cmd={cmd_str}")
    try:
        code, output, err = run_command(cmd_str)
        if code == 0:
            logger.info("WiFi event task registered")
            try:
                ps_cmd = (
                    '$t = Get-ScheduledTask -TaskName "WiFiAutoAuthEvent"; '
                    '$t.Settings.DisallowStartIfOnBatteries = $false; '
                    '$t.Settings.StopIfGoingOnBatteries = $false; '
                    '$t.Settings.AllowStartOnDemand = $true; '
                    '$t.Settings.ExecutionTimeLimit = [TimeSpan]::Zero; '
                    'Set-ScheduledTask -InputObject $t'
                )
                run_command(['powershell', '-Command', ps_cmd], shell=False)
                logger.info("WiFi event task power+timeout settings updated")
            except Exception as e2:
                logger.warning(f"Failed to update power/timeout settings: {e2}")
            return True
        else:
            logger.error(f"Failed to register WiFi event: code={code}, output={output}, err={err}")
    except Exception as e:
        logger.error(f"Failed to register WiFi event: {e}")
    return False


def unregister_wifi_event_task():
    """注销 WiFi 事件任务"""
    try:
        run_command('schtasks /Delete /TN WiFiAutoAuthEvent /F')
        logger.info("WiFi event task unregistered")
    except Exception:
        pass


def check_startup_status():
    """检查自启任务状态"""
    # TASK_NAME_STARTUP 仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import TASK_NAME_STARTUP
    code, output, _ = run_command(f'schtasks /Query /TN "{TASK_NAME_STARTUP}"')
    enabled = code == 0
    logger.debug(f"check_startup_status: enabled={enabled}")
    return enabled


def remove_startup_task():
    """移除自启任务"""
    # TASK_NAME_STARTUP 仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import TASK_NAME_STARTUP
    code, output, _ = run_command(f'schtasks /Delete /TN "{TASK_NAME_STARTUP}" /F')
    if code == 0:
        logger.info("Startup task removed")
        return True
    logger.error(f"Failed to remove startup task")
    return False


def hide_console():
    """隐藏控制台窗口，避免启动时闪现黑窗口"""
    try:
        # 先尝试通过 ShowWindow 隐藏窗口（比 FreeConsole 更平滑）
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE = 0
            logger.debug("hide_console: console window hidden via ShowWindow")
        # 然后再释放控制台
        ctypes.windll.kernel32.FreeConsole()
        logger.debug("hide_console: console freed")
    except Exception as e:
        logger.debug(f"hide_console: {e}")


def elevate_if_needed():
    """如果需要则提权执行"""
    # is_admin 仍由 tray_app.py 持有，延迟导入避免循环依赖
    from tray_app import is_admin
    if is_admin():
        return
    logger.info("Not admin, elevating...")
    exe_path = sys.executable
    script = sys.argv[0]
    # 构建参数：保留原有参数，不自动添加 --silent
    # 只有原来就是静默模式时才传递 --silent
    args = ' '.join(sys.argv[1:])
    # 使用 pythonw.exe 替代 python.exe 以避免控制台窗口
    if exe_path.lower().endswith('python.exe'):
        pythonw = exe_path[:-10] + 'pythonw.exe'
        if os.path.isfile(pythonw):
            exe_path = pythonw
            logger.debug(f"elevate_if_needed: using pythonw.exe: {exe_path}")
    try:
        # nShowCmd=0 表示隐藏窗口（SW_HIDE）
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, f'"{script}" {args}', None, 0
        )
        logger.debug(f"elevate_if_needed: ShellExecuteW returned {ret}")
        if ret > 32:
            logger.info("Elevated process started, exiting current instance")
            # 在退出前也隐藏当前窗口，避免闪现
            try:
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 0)
            except Exception:
                pass
            # 安全修复：使用 sys.exit 替代原强制退出方式，确保资源正常清理
            sys.exit(0)
        else:
            logger.error(f"elevate_if_needed: ShellExecuteW failed with code {ret}")
    except Exception as e:
        logger.error(f"Elevation failed: {e}")
