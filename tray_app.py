import os
import sys
import shutil
import json
import copy
import ctypes
import ctypes.wintypes
import threading
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import time
import traceback
import tempfile
from pathlib import Path
from ctypes import wintypes
import pystray
from PIL import Image, ImageDraw
import webview
from warp_exclusion import get_exclusion_manager, DnsMonitor
from traffic_monitor import get_traffic_status, get_traffic_status_fast, get_traffic_status_slow
import core.state
from core.state import _auth_lock, _auth_cancelled
from core.command import run_command
from core.webview import bring_window_to_top, create_webview_window
from core.network import (
    scan_wifi_networks, get_wifi_interface_name, get_local_ip,
    get_mac_address, get_current_wifi_ssid, wait_for_network_ready,
    _wait_for_ipv6_ready, is_warp_connected, _check_internet,
    has_public_ipv6,
)
from core.warp_manager import (
    get_warp_cli, connect_warp, disconnect_warp,
    _set_warp_masque_mode,
    update_tray_icon, update_tray_icon_restore,
)
from core.auth import (
    portal_login, portal_logout, disable_ipv4, enable_ipv4,
    _push_auth_progress, _check_cancel, _interruptible_sleep,
    run_auth_task, run_restore_task, _js_escape, _is_cancelled,
)
from core.startup import (
    check_single_instance, setup_startup_task, remove_startup_task,
    check_startup_status, register_wifi_event_task, unregister_wifi_event_task,
    wifi_event_monitor, start_wifi_event_monitor, cleanup_wifi_event,
    signal_wifi_event, _create_event_with_acl, check_startup_wifi_and_auth,
    _update_tray_status, elevate_if_needed, hide_console, _build_schtasks_tr,
)
from core.config import configure_config, get_config_store, DEFAULT_AUTH_WORKFLOW
from core.app_state import app_state
from core.updater import (UpdateDownloader, check_for_update as _check_for_update,
                          cleanup_temp_files, install_update as _install_update,
                          resolve_install_dir, validate_install_dir)
from core.version import __version__
from core.status import network_status
from core.auth_workflow import (
    run_workflow_by_id, validate_auth_workflow, workflow_catalog,
)


def get_resource_path(relative_path):
    """获取资源文件路径（支持开发环境和PyInstaller打包）"""
    if getattr(sys, 'frozen', False):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent
    return str(base_path / relative_path)

SCRIPT_DIR = Path(__file__).parent.resolve()
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent

LOG_FILE = SCRIPT_DIR / 'tray_app.log'
CONFIG_FILE = SCRIPT_DIR / 'tray_config.json'
TASK_NAME_STARTUP = "WiFiAutoAuthStartup"

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        RotatingFileHandler(str(LOG_FILE), maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('wifi_tray')
logging.getLogger('PIL').setLevel(logging.WARNING)
logging.getLogger('pystray').setLevel(logging.WARNING)


def seed_default_configs():
    """首次运行时用打包内置模板播种配置文件。

    模板（config/）预置了作者调好的分流规则与工作流，不含任何
    WiFi 名称、账号、密码等敏感信息——新用户开箱只需在设置页
    填写敏感信息即可使用。已存在的配置文件不会被覆盖。
    """
    try:
        if not CONFIG_FILE.exists():
            tpl = Path(get_resource_path('config/tray_config.json'))
            if tpl.exists():
                shutil.copyfile(tpl, CONFIG_FILE)
                logger.info(f'[seed] tray_config.json seeded from {tpl}')
    except Exception:
        logger.debug('seed tray_config.json failed', exc_info=True)


seed_default_configs()

CONFIG_STORE = configure_config(CONFIG_FILE)
CONFIG = CONFIG_STORE.snapshot()

# 工作流节点计时统计（自动调优数据源），与配置文件同目录
from core.workflow_tuning import configure_tuning  # noqa: E402

TUNING_STORE = configure_tuning(SCRIPT_DIR / 'workflow_tuning.json')


def record_install_dir():
    """记录首次安装目录，后续更新始终覆盖安装到该位置。"""
    try:
        record = CONFIG_STORE.get('install_dir')
        resolved = resolve_install_dir(record)
        if str(record or '') != str(resolved):
            CONFIG_STORE.patch({'install_dir': str(resolved)})
        return resolved
    except Exception:
        logger.debug('record_install_dir failed', exc_info=True)
        return None


INSTALL_DIR = record_install_dir()
# 清理上一次更新遗留的临时文件
cleanup_temp_files()


def _on_config_changed(config, revision, changed):
    """Keep legacy readers compatible while publishing one live revision."""
    global CONFIG
    CONFIG = config
    app_state.set_config_revision(revision)
    logger.info('Config updated: revision=%s, fields=%s', revision, sorted(changed))
    if changed & {'workflows', 'active_workflow_id', 'auth_workflow'} and core.state._tray_app_instance:
        try:
            core.state._tray_app_instance._refresh_tray_menu()
        except Exception:
            logger.exception('Failed to refresh workflow tray menu')


CONFIG_STORE.subscribe(_on_config_changed)
app_state.set_config_revision(CONFIG_STORE.revision)


def load_config():
    """Return a current isolated snapshot; callers cannot mutate shared state."""
    return CONFIG_STORE.snapshot()


def _virtual_screen_metrics():
    user32 = ctypes.windll.user32
    return {
        'x': user32.GetSystemMetrics(76),
        'y': user32.GetSystemMetrics(77),
        'width': user32.GetSystemMetrics(78),
        'height': user32.GetSystemMetrics(79),
    }


def _find_main_hwnd():
    """查找主窗口句柄，找不到返回 0。"""
    try:
        return ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
    except Exception:
        return 0


def _is_window_zoomed():
    """主窗口当前是否处于最大化状态。"""
    hwnd = _find_main_hwnd()
    return bool(hwnd and ctypes.windll.user32.IsZoomed(hwnd))


def _dpi_scale():
    """当前系统 DPI 缩放系数（物理像素 / 逻辑像素）。

    pywebview 6.x 的约定：create_window 的 width/height/x/y/min_size 是
    逻辑像素，WinForms 层内部会乘以 DPI scale 换算为物理像素；
    而 GetWindowRect/GetWindowPlacement/SetWindowPos 全程使用物理像素。
    保存/恢复几何时必须跨越这两个坐标系，此函数负责换算比例。
    """
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ('length', ctypes.c_uint),
        ('flags', ctypes.c_uint),
        ('showCmd', ctypes.c_uint),
        ('ptMinPosition', ctypes.wintypes.POINT),
        ('ptMaxPosition', ctypes.wintypes.POINT),
        ('rcNormalPosition', ctypes.wintypes.RECT),
    ]


SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3


def _primary_workarea_origin():
    """主显示器工作区原点（屏幕坐标）。GetWindowPlacement 的
    rcNormalPosition 使用工作区坐标，原点即主显示器工作区左上角。"""
    try:
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(None, 1)  # MONITOR_DEFAULTTOPRIMARY
        if monitor:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [('cbSize', ctypes.c_uint),
                            ('rcMonitor', ctypes.wintypes.RECT),
                            ('rcWork', ctypes.wintypes.RECT),
                            ('dwFlags', ctypes.c_uint)]
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return mi.rcWork.left, mi.rcWork.top
    except Exception:
        pass
    return 0, 0


def _capture_window_geometry():
    """采集窗口当前几何（物理屏幕坐标 + 最大化标记），任何保存路径共用。

    采用 Win32 规范做法（GetWindowPlacement，各原生应用保存窗口状态的标准方式）：
    - 最大化/最小化时取 rcNormalPosition（还原后的普通态矩形），
      避免把最大化矩形误存为普通尺寸；
    - 普通态直接取 GetWindowRect 实时矩形。
    """
    hwnd = _find_main_hwnd()
    if not hwnd:
        return None
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    has_placement = bool(ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)))
    maximized = bool(has_placement and wp.showCmd == SW_SHOWMAXIMIZED)
    if has_placement and (maximized or wp.showCmd == SW_SHOWMINIMIZED):
        # 最大化/最小化：取还原矩形（工作区坐标 → 屏幕坐标）
        off_x, off_y = _primary_workarea_origin()
        rc = wp.rcNormalPosition
        x, y = rc.left + off_x, rc.top + off_y
        w, h = rc.right - rc.left, rc.bottom - rc.top
    else:
        rect = ctypes.wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        x, y = rect.left, rect.top
        w, h = rect.right - rect.left, rect.bottom - rect.top
    return {'width': int(w), 'height': int(h), 'x': int(x), 'y': int(y),
            'maximized': maximized}


def _enable_dpi_awareness():
    """在创建任何窗口/读取屏幕指标前声明系统级 DPI 感知。

    WinForms/WebView2 运行时会使进程成为 DPI 感知（GetWindowRect 返回物理像素），
    但若不在启动早期设置，calc_initial_window_geometry 里的 GetSystemMetrics
    仍处于 DPI 虚拟化状态（返回逻辑像素），导致保存的物理坐标被按逻辑屏幕
    校验/创建，出现窗口位置与大小"记不住"的问题。这里提前设置，保证全程
    使用同一套物理像素坐标系。与 WinForms 默认行为一致（system-aware）。
    """
    try:
        # PROCESS_SYSTEM_DPI_AWARE = 1（shcore 优先，失败再退 user32）
        try:
            val = ctypes.c_int(1)
            if ctypes.windll.shcore.SetProcessDpiAwareness(val) == 0:
                return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        logger.debug('Failed to set DPI awareness', exc_info=True)


def _valid_window_geometry(width, height, x, y):
    try:
        width, height, x, y = int(width), int(height), int(x), int(y)
    except (TypeError, ValueError):
        return False
    if width < TrayApp.MIN_W or height < TrayApp.MIN_H:
        return False
    if width > 10000 or height > 10000:
        return False
    screen = _virtual_screen_metrics()
    # Keep at least part of the title bar reachable, including on multi-monitor
    # setups where the secondary monitor has negative virtual coordinates.
    return (screen['x'] - width + 120 <= x <= screen['x'] + screen['width'] - 120 and
            screen['y'] - height + 80 <= y <= screen['y'] + screen['height'] - 80)



def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def create_icon(color='orange'):
    """CAuth 托盘图标：深色圆角方形徽章 + 粗体白色 "C" 字标 + 状态色指示点。

    与前端 LogoMark（C 字标）和黑白单色设计系统保持一致，
    状态通过徽章描边与右下角指示点的颜色表达：
      gray=待机 green=正常 orange=进行中 red=异常
    """
    size = (64, 64)
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        'gray': (155, 155, 161),
        'green': (34, 197, 94),
        'orange': (245, 158, 11),
        'red': (239, 68, 68)
    }
    status = colors.get(color, colors['orange'])

    # 徽章：深色圆角方形 + 状态色描边（浅/深任务栏上都可辨识）
    draw.rounded_rectangle([2, 2, 62, 62], radius=15,
                           fill=(26, 26, 30, 255), outline=status, width=3)
    # 粗体 "C" 字标：白色粗弧线，开口朝右
    draw.arc([17, 17, 47, 47], start=45, end=315, fill=(255, 255, 255), width=9)
    # 状态指示点：右下角，带深色衬圈
    draw.ellipse([45, 45, 58, 58], fill=(26, 26, 30, 255))
    draw.ellipse([48, 48, 55, 55], fill=status)
    return img

def ensure_app_icon():
    icon_path = SCRIPT_DIR / 'app.ico'
    import io
    img = create_icon('orange')
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    imgs = [img.resize(s, Image.LANCZOS) for s in sizes]
    png_bufs = []
    for im in imgs:
        b = io.BytesIO()
        im.save(b, format='PNG')
        png_bufs.append(b.getvalue())
    header = b'\x00\x00\x01\x00'
    count = len(png_bufs)
    header += count.to_bytes(2, 'little')
    offset = 6 + count * 16
    dir_entries = b''
    for s, data in zip(sizes, png_bufs):
        w = s[0] if s[0] < 256 else 0
        h = s[1] if s[1] < 256 else 0
        entry = bytes([w, h, 0, 0, 1, 0, 32, 0]) + len(data).to_bytes(4, 'little') + offset.to_bytes(4, 'little')
        dir_entries += entry
        offset += len(data)
    with open(str(icon_path), 'wb') as f:
        f.write(header + dir_entries + b''.join(png_bufs))
    logger.info(f"App icon saved to {icon_path}")
    return str(icon_path)

class ApiBridge:
    # 应用级单例：一次只进行一个下载任务
    _update_downloader = UpdateDownloader()
    _update_state = {'available': False, 'checked': False}
    _update_lock = threading.Lock()

    def load_config(self):
        return CONFIG_STORE.snapshot(include_revision=True)

    # ===== 应用更新（GitHub Releases）=====
    def get_app_info(self):
        """应用版本与安装位置（设置页展示用）。"""
        return {
            'version': __version__,
            'install_dir': str(INSTALL_DIR or resolve_install_dir(
                CONFIG_STORE.get('install_dir'))),
            'exe': str(Path(INSTALL_DIR or resolve_install_dir(
                CONFIG_STORE.get('install_dir'))) / 'CampusAuth.exe'),
        }

    def check_for_update(self):
        """检查 GitHub Releases 是否有新版本；失败静默（available=False）。"""
        try:
            result = _check_for_update()
            with self._update_lock:
                self._update_state['available'] = bool(result.get('available'))
                self._update_state['checked'] = True
                latest = result.get('latest') or {}
                if latest.get('version'):
                    self._update_state['version'] = latest['version']
            return result
        except Exception as exc:
            logger.info('[updater] check_for_update failed silently: %s', exc)
            return {'available': False, 'current': __version__, 'latest': None,
                    'reason': 'error'}

    def get_update_progress(self):
        """轮询更新下载/安装进度。"""
        with self._update_lock:
            available = self._update_state['available']
        progress = self._update_downloader.progress()
        progress['available'] = available
        return progress

    def install_update(self):
        """下载并安装最新版本，成功后自动退出应用由更新脚本完成覆盖安装。

        只替换 exe 本身，同目录的 tray_config.json / warp_exclusion_config.json 不受影响。
        """
        try:
            with self._update_lock:
                if not self._update_state['available']:
                    # 未检测过（或缓存丢失）时先检测一次，避免直接安装失败
                    result = _check_for_update()
                    if not result.get('available'):
                        return {'success': False, 'message': '未检测到新版本'}
                    self._update_state['available'] = True
            latest = _check_for_update().get('latest') or {}
            url = latest.get('download_url')
            if not url:
                return {'success': False, 'message': '新版本缺少可下载的安装包'}
            if self._update_downloader.busy:
                return {'success': True, 'message': '更新任务进行中',
                        'progress': self._update_downloader.progress()}
            self._update_downloader.start(url, latest.get('size', 0),
                                          on_done=self._on_update_downloaded)
            return {'success': True, 'message': '开始下载更新',
                    'progress': self._update_downloader.progress()}
        except Exception as exc:
            logger.exception('[updater] install_update failed')
            return {'success': False, 'message': str(exc)}

    def _on_update_downloaded(self, result):
        """下载完成后：启动更新脚本 → 退出应用 → 由脚本覆盖安装并重启。"""
        if not result or not result.get('success'):
            logger.warning('[updater] download failed, update aborted: %s',
                           (result or {}).get('error'))
            return
        try:
            self._update_downloader._set(status='installing', pct=100,
                                         message='正在安装更新…')
        except Exception:
            pass
        install_dir = resolve_install_dir(CONFIG_STORE.get('install_dir'))
        with self._update_lock:
            pending_version = self._update_state.get('version') or ''
        # 更新后一律重启。此前这里写成"静默启动模式不重启"是错的：silent_startup
        # 只决定开机自启时是否显示窗口，并不表示用户希望更新后应用不再起来——
        # 那样会让静默模式用户更新完就卡在没有进程、也没有界面的状态。
        # 重启时不带 --silent，因此会像普通启动一样弹出主窗口，便于确认更新结果。
        outcome = _install_update(result['file'], install_dir,
                                  restart=True, version=pending_version)
        if not outcome.get('success'):
            self._update_downloader._set(status='error', pct=0,
                                         message=f"安装失败：{outcome.get('message')}",
                                         error=outcome.get('message', 'install_failed'))
            logger.error('[updater] 覆盖安装启动失败: %s', outcome.get('message'))
            return
        # 稍作延迟让前端收到状态更新，然后退出应用，交棒给更新脚本
        def _exit_later():
            time.sleep(1.2)
            try:
                app = core.state._tray_app_instance
                if app:
                    app.request_exit()
                else:
                    os._exit(0)
            except Exception:
                logger.exception('[updater] exit after update failed')
                os._exit(0)

        threading.Thread(target=_exit_later, daemon=True).start()

    def start_resize(self, direction):
        """frameless 窗口已改用 JS mousemove + resize_move_window 实现，此方法保留兼容。"""
        pass

    def get_window_rect(self):
        """返回当前窗口位置和尺寸 {x, y, width, height}。"""
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
            if hwnd:
                rect = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                return {'x': rect.left, 'y': rect.top,
                        'width': rect.right - rect.left, 'height': rect.bottom - rect.top}
        except Exception as e:
            logger.error(f"get_window_rect failed: {e}")
        return {'x': 0, 'y': 0, 'width': 1210, 'height': 770}

    def resize_move_window(self, width, height, x, y):
        """调整窗口大小并移动到指定位置（用于 frameless 窗口自定义拖拽调整大小）。"""
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
            if hwnd:
                # SWP_NOZORDER=0x0004 | SWP_NOACTIVATE=0x0010
                ctypes.windll.user32.SetWindowPos(hwnd, 0, int(x), int(y), int(width), int(height), 0x0014)
        except Exception as e:
            logger.error(f"resize_move_window failed: {e}")

    def save_window_geometry(self, rect=None):
        """即时保存窗口位置和尺寸；前端拖拽/缩放结束时调用。

        统一走 _capture_window_geometry()（GetWindowPlacement 规范做法）：
        普通态存实时矩形；最大化/最小化只更新 maximized 标记并保存
        还原后的普通态矩形，避免把最大化矩形当普通尺寸保存。
        rect 参数仅为兼容前端签名，几何一律以 Win32 实时数据为准。
        """
        try:
            captured = _capture_window_geometry()
            if captured is None:
                if rect is None:
                    return {'success': True}
                width, height = rect.get('width'), rect.get('height')
                x, y = rect.get('x'), rect.get('y')
                if not _valid_window_geometry(width, height, x, y):
                    return {'success': False, 'message': '窗口位置或尺寸异常，已忽略'}
                captured = {'width': int(width), 'height': int(height),
                            'x': int(x), 'y': int(y), 'maximized': False}
            if not _valid_window_geometry(captured['width'], captured['height'],
                                          captured['x'], captured['y']):
                logger.warning(f"[save_window_geometry] Ignored abnormal: "
                               f"{captured['width']}x{captured['height']} at ({captured['x']},{captured['y']})")
                return {'success': False, 'message': '窗口位置或尺寸异常，已忽略'}
            saved = CONFIG_STORE.patch({'window': captured})
            state = 'maximized' if captured['maximized'] else 'normal'
            logger.info(f"[save_window_geometry] Saved ({state}): "
                        f"{captured['width']}x{captured['height']} at ({captured['x']},{captured['y']})")
            return {'success': True, 'revision': saved.get('_revision')}
        except Exception as exc:
            logger.exception('save_window_geometry failed')
            return {'success': False, 'message': str(exc)}

    def minimize_window(self):
        try:
            if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                core.state._tray_app_instance.settings_window.minimize()
                logger.info("Window minimized via title bar button")
        except Exception as e:
            logger.error(f"minimize_window failed: {e}")

    def close_window(self):
        try:
            if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                core.state._tray_app_instance.save_window_geometry()
                core.state._tray_app_instance.settings_window.hide()
                logger.info("Window hidden via title bar button")
        except Exception as e:
            logger.error(f"close_window failed: {e}")

    def scan_wifi(self):
        return scan_wifi_networks()

    def _sync_monitor_state(self, old_config, new_config):
        old_needed = bool(old_config.get('auto_auth') or old_config.get('auto_restore'))
        needed = bool(new_config.get('auto_auth') or new_config.get('auto_restore'))
        task_changed = any(old_config.get(key) != new_config.get(key) for key in
                           ('wifi_name', 'auto_auth', 'auto_restore'))
        if needed:
            start_wifi_event_monitor()
            if task_changed and not register_wifi_event_task():
                logger.warning('WiFi event task could not be registered; in-process monitor remains active')
        elif old_needed:
            cleanup_wifi_event()
            unregister_wifi_event_task()
    def save_config(self, config):
        changes = dict(config or {})
        expected_revision = changes.pop('_revision', None)
        if changes.get('auto_auth') and not str(changes.get('wifi_name', '')).strip():
            return {'success': False, 'message': '请先选择或输入 WiFi 名称'}
        if 'auth_workflow' in changes:
            try:
                validate_auth_workflow(changes['auth_workflow'])
            except ValueError as exc:
                return {'success': False, 'message': f'工作流配置无效：{exc}'}
        old_config = CONFIG_STORE.snapshot()
        try:
            saved = CONFIG_STORE.patch(changes, expected_revision=expected_revision)
        except (TypeError, ValueError, OSError) as exc:
            return {'success': False, 'message': f'保存失败：{exc}'}
        self._sync_monitor_state(old_config, saved)
        return {'success': True, 'message': '设置已保存',
                'revision': saved.get('_revision')}
    def cancel_operation(self):
        logger.info("cancel_operation called")
        if _auth_lock.locked():
            _auth_cancelled.set()
            app_state.update_operation(status='cancelled', message='已取消')
            logger.info("Cancel flag set, notifying frontend immediately")
            if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                js_code = f"onAuthProgress({{step:0, total:1, message:{_js_escape('已取消')}, status:{_js_escape('cancelled')}}})"
                core.state._tray_app_instance.settings_window.evaluate_js(js_code)
            return {'success': True, 'message': '已取消'}
        return {'success': True, 'message': '没有正在进行的操作'}

    def test_auth(self):
        # 分配新操作纪元：旧操作滞后的进度/终态事件将被前端整体忽略
        app_state.start_operation('auth')

        def _do_auth():
            if not _auth_lock.acquire(blocking=False):
                # 操作抢占：取消当前操作并接管，确保执行用户的最新操作
                logger.info("test_auth: auth lock busy, cancelling current operation for new request")
                _auth_cancelled.set()
                app_state.update_operation(status='cancelled', message='已被新的认证请求取代')
                if not _auth_lock.acquire(timeout=3):
                    logger.error("test_auth: could not acquire lock after cancel")
                    js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape('无法取消当前操作，请稍后重试')}, status:{_js_escape('error')}}})"
                    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                        core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                    return
            try:
                # 按配置绑定的工作流执行；绑定无效时回退到激活工作流
                auth_wf_id = CONFIG_STORE.get('auth_button_workflow') or ''
                if auth_wf_id and (CONFIG_STORE.get('workflows') or {}).get(auth_wf_id):
                    logger.info(f"test_auth: running bound workflow {auth_wf_id}")
                    success, msg = run_workflow_by_id(auth_wf_id)
                else:
                    success, msg = run_auth_task()
                if _auth_cancelled.is_set():
                    logger.info("test_auth: operation was cancelled, skipping final notification")
                else:
                    status = "success" if success else "error"
                    js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(msg)}, status:{_js_escape(status)}}})"
                    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                        core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                    update_tray_icon(success, msg)
            except Exception as e:
                logger.error(f"test_auth thread error: {e}")
                if not _auth_cancelled.is_set():
                    js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(str(e))}, status:{_js_escape('error')}}})"
                    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                        core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                    update_tray_icon(False, str(e))
            finally:
                _auth_cancelled.clear()
                _auth_lock.release()
        threading.Thread(target=_do_auth, daemon=True).start()
        return {'success': True, 'message': '认证已启动'}

    def auto_save_form(self, form_data):
        allowed = {
            'wifi_name', 'username', 'password', 'auto_auth', 'auto_restore',
            'warp_cli_path', 'silent_startup', 'portal_ip', 'portal_port',
            'auto_enable_ipv4', 'auth_total_timeout', 'auth_workflow',
            'auth_button_workflow', 'restore_button_workflow',
            'auto_check_update', 'auto_tune_workflow',
        }
        changes = {key: value for key, value in (form_data or {}).items() if key in allowed}
        old_config = CONFIG_STORE.snapshot()
        try:
            if 'auth_workflow' in changes:
                validate_auth_workflow(changes['auth_workflow'])
            saved = CONFIG_STORE.patch(changes)
            self._sync_monitor_state(old_config, saved)
            return {'success': True, 'revision': saved.get('_revision')}
        except Exception as exc:
            logger.error('Auto-save failed: %s', exc)
            return {'success': False, 'message': str(exc),
                    'revision': CONFIG_STORE.revision}
    def check_network_status(self):
        return network_status.refresh()

    def get_app_state(self):
        return app_state.snapshot()

    def _workflow_snapshot(self, config=None):
        config = config or CONFIG_STORE.snapshot()
        workflows = list((config.get('workflows') or {}).values())
        workflows.sort(key=lambda item: (not item.get('built_in', False), item.get('id', '')))
        return workflows

    def get_workflow_catalog(self):
        config = CONFIG_STORE.snapshot(include_revision=True)
        active_id = config.get('active_workflow_id', 'default_auth')
        workflow = (config.get('workflows') or {}).get(active_id, {})
        return {
            'steps': workflow_catalog(),
            'workflows': self._workflow_snapshot(config),
            'active_workflow_id': active_id,
            'workflow': workflow.get('steps', DEFAULT_AUTH_WORKFLOW),
            'revision': config.get('_revision'),
        }

    def get_workflow_stats(self, workflow_id=None):
        """节点运行统计与调优建议（工作流页稳定度可视化用）。"""
        config = CONFIG_STORE.snapshot()
        workflow_id = workflow_id or config.get('active_workflow_id', 'default_auth')
        return {
            'workflow_id': workflow_id,
            'auto_tune': bool(config.get('auto_tune_workflow')),
            'steps': TUNING_STORE.workflow_stats(workflow_id),
        }

    def set_workflow_auto_tune(self, enabled):
        """开关「自动调优」：按运行数据自动调整各节点的超时与重试。"""
        try:
            enabled = bool(enabled)
            saved = CONFIG_STORE.patch({'auto_tune_workflow': enabled})
            logger.info('[tuning] 自动调优已%s', '开启' if enabled else '关闭')
            return {'success': True, 'auto_tune': enabled,
                    'revision': saved.get('_revision')}
        except Exception as exc:
            logger.exception('set_workflow_auto_tune failed')
            return {'success': False, 'message': str(exc)}

    def list_workflows(self):
        return {'workflows': self._workflow_snapshot(),
                'active_workflow_id': CONFIG_STORE.get('active_workflow_id', 'default_auth')}

    def _validate_workflow_steps(self, steps):
        validate_auth_workflow(steps)
        return [dict(item) for item in steps]

    def save_workflow(self, workflow, workflow_id=None, name=None):
        config = CONFIG_STORE.snapshot()
        target_id = workflow_id or config.get('active_workflow_id', 'default_auth')
        workflows = config.get('workflows') or {}
        target = workflows.get(target_id)
        if not target:
            return {'success': False, 'message': '工作流不存在'}
        try:
            steps = self._validate_workflow_steps(workflow)
            updated = copy.deepcopy(workflows)
            # 名称与步骤一起保存：修复"改名后点保存名称不生效"的问题
            clean_name = None
            if name is not None:
                clean_name = str(name).strip()[:60]
                if not clean_name:
                    return {'success': False, 'message': '工作流名称不能为空'}
                if clean_name != target.get('name') and target.get('built_in'):
                    return {'success': False, 'message': '内置工作流名称不可修改，可另存为自定义工作流'}
            updated[target_id] = {**target,
                                  'steps': steps,
                                  'name': clean_name or target.get('name'),
                                  'customized': bool(target.get('built_in', False))}
            patch = {'workflows': updated}
            if config.get('active_workflow_id') == target_id:
                patch['auth_workflow'] = steps
            saved = CONFIG_STORE.patch(patch)
            return {'success': True, 'message': '工作流已保存',
                    'workflows': self._workflow_snapshot(saved),
                    'workflow': saved['workflows'][target_id],
                    'active_workflow_id': saved['active_workflow_id'],
                    'revision': saved['_revision']}
        except Exception as exc:
            logger.exception('save_workflow failed')
            return {'success': False, 'message': str(exc)}

    def save_workflow_as(self, name, workflow, tray_menu=True):
        clean_name = str(name or '').strip()[:60]
        if not clean_name:
            return {'success': False, 'message': '请输入工作流名称'}
        try:
            steps = self._validate_workflow_steps(workflow)
        except Exception as exc:
            return {'success': False, 'message': str(exc)}
        config = CONFIG_STORE.snapshot()
        workflows = copy.deepcopy(config.get('workflows') or {})
        from core.config import workflow_id_from_name
        base_id = workflow_id_from_name(clean_name)
        workflow_id = base_id
        suffix = 2
        while workflow_id in workflows:
            workflow_id = f'{base_id}_{suffix}'
            suffix += 1
        workflows[workflow_id] = {
            'id': workflow_id,
            'name': clean_name,
            'description': '用户自定义工作流',
            'built_in': False,
            'tray_menu': bool(tray_menu),
            'steps': steps,
        }
        saved = CONFIG_STORE.patch({'workflows': workflows,
                                    'active_workflow_id': workflow_id,
                                    'auth_workflow': steps})
        return {'success': True, 'message': f'已保存为独立工作流：{clean_name}',
                'workflows': self._workflow_snapshot(saved),
                'workflow': saved['workflows'][workflow_id],
                'active_workflow_id': workflow_id,
                'revision': saved['_revision']}

    def update_workflow_meta(self, workflow_id, name=None, tray_menu=None):
        config = CONFIG_STORE.snapshot()
        workflows = copy.deepcopy(config.get('workflows') or {})
        target = workflows.get(workflow_id)
        if not target:
            return {'success': False, 'message': '工作流不存在'}
        if name is not None:
            clean_name = str(name).strip()[:60]
            if not clean_name:
                return {'success': False, 'message': '工作流名称不能为空'}
            target['name'] = clean_name
        if tray_menu is not None:
            target['tray_menu'] = bool(tray_menu)
        saved = CONFIG_STORE.patch({'workflows': workflows})
        return {'success': True, 'workflows': self._workflow_snapshot(saved),
                'workflow': saved['workflows'][workflow_id],
                'revision': saved['_revision']}

    def select_workflow(self, workflow_id):
        config = CONFIG_STORE.snapshot()
        workflows = config.get('workflows') or {}
        if workflow_id not in workflows:
            return {'success': False, 'message': '工作流不存在'}
        steps = workflows[workflow_id].get('steps', [])
        saved = CONFIG_STORE.patch({'active_workflow_id': workflow_id,
                                    'auth_workflow': steps})
        return {'success': True, 'workflows': self._workflow_snapshot(saved),
                'workflow': saved['workflows'][workflow_id],
                'active_workflow_id': workflow_id,
                'revision': saved['_revision']}

    def delete_workflow(self, workflow_id):
        config = CONFIG_STORE.snapshot()
        workflows = copy.deepcopy(config.get('workflows') or {})
        target = workflows.get(workflow_id)
        if not target:
            return {'success': False, 'message': '工作流不存在'}
        if target.get('built_in'):
            return {'success': False, 'message': '内置工作流不能删除，可取消托盘显示或另存为自定义工作流'}
        del workflows[workflow_id]
        active_id = config.get('active_workflow_id')
        patch = {'workflows': workflows}
        if active_id == workflow_id:
            patch['active_workflow_id'] = 'default_auth'
            patch['auth_workflow'] = workflows['default_auth']['steps']
        saved = CONFIG_STORE.patch(patch)
        return {'success': True, 'message': '工作流已删除',
                'workflows': self._workflow_snapshot(saved),
                'active_workflow_id': saved['active_workflow_id'],
                'revision': saved['_revision']}

    def reset_workflow(self, workflow_id=None):
        config = CONFIG_STORE.snapshot()
        target_id = workflow_id or config.get('active_workflow_id', 'default_auth')
        workflows = copy.deepcopy(config.get('workflows') or {})
        target = workflows.get(target_id)
        if not target or not target.get('built_in'):
            return {'success': False, 'message': '仅内置工作流支持一键恢复默认'}
        from core.config import _builtin_workflows
        workflows[target_id] = copy.deepcopy(_builtin_workflows()[target_id])
        patch = {'workflows': workflows}
        if config.get('active_workflow_id') == target_id:
            patch['auth_workflow'] = workflows[target_id]['steps']
        saved = CONFIG_STORE.patch(patch)
        return {'success': True, 'message': '已恢复默认工作流',
                'workflows': self._workflow_snapshot(saved),
                'workflow': saved['workflows'][target_id],
                'active_workflow_id': saved['active_workflow_id'],
                'revision': saved['_revision']}

    def run_workflow(self, workflow_id):
        workflow = (CONFIG_STORE.get('workflows') or {}).get(workflow_id)
        if not workflow:
            return {'success': False, 'message': '工作流不存在'}
        # 分配新操作纪元，保证进度事件归属于本次启动的工作流
        app_state.start_operation('auth')

        def _do_run():
            if not _auth_lock.acquire(blocking=False):
                js_code = f"onAuthProgress({{step:1,total:1,message:{_js_escape('工作流正在进行中，请稍候')},status:{_js_escape('error')}}})"
                if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                    core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                return
            try:
                success, message = run_workflow_by_id(workflow_id)
                if not _auth_cancelled.is_set() and core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                    status = 'success' if success else 'error'
                    js_code = f"onAuthProgress({{step:1,total:1,message:{_js_escape(message)},status:{_js_escape(status)}}})"
                    core.state._tray_app_instance.settings_window.evaluate_js(js_code)
            finally:
                _auth_cancelled.clear()
                _auth_lock.release()
        threading.Thread(target=_do_run, daemon=True).start()
        return {'success': True, 'message': f'工作流 {workflow.get("name", workflow_id)} 已启动'}
    def restore_network(self):
        logger.info("restore_network called")
        # 分配新操作纪元：旧操作滞后的进度/终态事件将被前端整体忽略
        app_state.start_operation('restore')
        def _do_restore():
            if not _auth_lock.acquire(blocking=False):
                logger.warning("restore_network: auth lock busy, cancelling current operation...")
                _auth_cancelled.set()
                if not _interruptible_sleep(1):
                    pass
                if not _auth_lock.acquire(timeout=3):
                    logger.error("restore_network: could not acquire lock after cancel")
                    js_code = f"onAuthProgress({{step:3, total:3, message:{_js_escape('无法取消当前操作')}, status:{_js_escape('error')}}})"
                    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                        core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                    return
            try:
                # 按配置绑定的工作流执行；未绑定（空串）时使用内置恢复逻辑
                restore_wf_id = CONFIG_STORE.get('restore_button_workflow') or ''
                if restore_wf_id and (CONFIG_STORE.get('workflows') or {}).get(restore_wf_id):
                    logger.info(f"restore_network: running bound workflow {restore_wf_id}")
                    success, msg = run_workflow_by_id(restore_wf_id)
                else:
                    success, msg = run_restore_task()
                app_state.update_operation(kind='restore', status='success' if success else 'error', message=msg)
                network_status.request_refresh()
                if _auth_cancelled.is_set():
                    logger.info("restore_network: operation was cancelled, skipping final notification")
                else:
                    status = "success" if success else "error"
                    js_code = f"onAuthProgress({{step:3, total:3, message:{_js_escape(msg)}, status:{_js_escape(status)}, action:'restore'}})"
                    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                        core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                    update_tray_icon_restore(success, msg)
            except Exception as e:
                logger.error(f"restore_network thread error: {e}")
                app_state.update_operation(kind='restore', status='error', message=str(e))
                network_status.request_refresh()
                if not _auth_cancelled.is_set():
                    update_tray_icon_restore(False, str(e))
            finally:
                _auth_cancelled.clear()
                _auth_lock.release()
        threading.Thread(target=_do_restore, daemon=True).start()
        return {'success': True, 'message': '恢复已启动'}

    def get_startup_status(self):
        enabled = CONFIG.get('auto_startup', False)
        return {'enabled': enabled}

    def set_startup(self, enabled):
        logger.info(f"set_startup called: enabled={enabled}")
        if enabled:
            if not is_admin():
                return {'success': False, 'message': '需要管理员权限'}
            if setup_startup_task():
                CONFIG_STORE.patch({'auto_startup': True})
                if core.state._tray_app_instance:
                    core.state._tray_app_instance._refresh_tray_menu()
                return {'success': True, 'message': '开机自启已开启'}
            return {'success': False, 'message': '设置失败'}
        else:
            remove_startup_task()
            CONFIG_STORE.patch({'auto_startup': False})
            if core.state._tray_app_instance:
                core.state._tray_app_instance._refresh_tray_menu()
            return {'success': True, 'message': '开机自启已关闭'}

    def browse_folder(self, title='选择文件'):
        logger.info(f"browse_folder called: title={title}")
        try:
            escaped_title = title.replace("'", "''")
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$d.Title = '{escaped_title}'; "
                "$d.Filter = '可执行文件 (*.exe)|*.exe|所有文件 (*.*)|*.*'; "
                "$d.FilterIndex = 1; "
                "$d.CheckFileExists = $true; "
                "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ Write-Output $d.FileName } else { Write-Output '' }"
            )
            tmp_ps = os.path.join(tempfile.gettempdir(), f'wifi_browse_{os.getpid()}.ps1')
            with open(tmp_ps, 'w', encoding='utf-8') as f:
                f.write(ps_script)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            result = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass', '-File', tmp_ps],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                timeout=120, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
            )
            try:
                os.remove(tmp_ps)
            except Exception:
                pass
            path = result.stdout.strip() if result.returncode == 0 else ''
            logger.info(f"browse_folder: selected={path!r}, rc={result.returncode}, stderr={result.stderr[:200]!r}")
            return path
        except subprocess.TimeoutExpired:
            logger.info("browse_folder: timed out")
            return ''
        except Exception as e:
            logger.error(f"browse_folder failed: {e}")
            return ''

    def browse_directory(self, title='选择目录', initial_dir=''):
        """打开 Windows 资源管理器文件夹选择对话框（WinForms FolderBrowserDialog）。

        与 browse_folder（文件选择）互补：这里返回用户选中的目录路径，取消返回空串。
        """
        logger.info(f"browse_directory called: title={title}")
        try:
            escaped_title = str(title).replace("'", "''")
            escaped_initial = str(initial_dir or '').replace("'", "''")
            initial_clause = f"$d.SelectedPath = '{escaped_initial}'; " if escaped_initial else ''
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$d.Description = '{escaped_title}'; "
                "$d.ShowNewFolderButton = $true; "
                f"{initial_clause}"
                "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ Write-Output $d.SelectedPath } else { Write-Output '' }"
            )
            tmp_ps = os.path.join(tempfile.gettempdir(),
                                  f'cauth_browse_dir_{os.getpid()}.ps1')
            with open(tmp_ps, 'w', encoding='utf-8') as f:
                f.write(ps_script)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            result = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass', '-File', tmp_ps],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                timeout=180, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
            )
            try:
                os.remove(tmp_ps)
            except Exception:
                pass
            path = result.stdout.strip() if result.returncode == 0 else ''
            logger.info(f"browse_directory: selected={path!r}, rc={result.returncode}")
            return path
        except subprocess.TimeoutExpired:
            logger.info("browse_directory: timed out")
            return ''
        except Exception as e:
            logger.error(f"browse_directory failed: {e}")
            return ''

    def set_install_dir(self, path):
        """更改后续更新的安装目录（只记录位置，不移动当前 exe）。"""
        try:
            ok, message = validate_install_dir(path)
            if not ok:
                logger.warning(f"set_install_dir rejected: {message}")
                return {'success': False, 'message': message}
            global INSTALL_DIR
            CONFIG_STORE.patch({'install_dir': message})
            INSTALL_DIR = Path(message)
            logger.info(f"[updater] 安装目录已更新为 {message}")
            return {'success': True, 'message': message, 'install_dir': message}
        except Exception as exc:
            logger.exception('set_install_dir failed')
            return {'success': False, 'message': str(exc)}

    def refresh_startup_task(self):
        logger.info("refresh_startup_task called")
        if check_startup_status():
            if is_admin():
                setup_startup_task()
                return {'success': True, 'message': '自启任务已更新'}
            return {'success': False, 'message': '需要管理员权限'}
        return {'success': True, 'message': '无需更新'}

    # ------------------------------------------------------------------
    # WARP 排除管理 API（供 settings.html WARP排除tab 调用）
    # ------------------------------------------------------------------
    def _get_mgr(self):
        """获取 ExclusionManager 单例"""
        return get_exclusion_manager()

    def get_exclusion_config(self):
        return self._get_mgr().get_config()

    def add_domain(self, domain, route='ipv6'):
        ok, msg, info = self._get_mgr().add_domain(domain, route=route)
        return {'success': ok, 'message': msg, 'info': info}

    def remove_domain(self, domain):
        ok, msg = self._get_mgr().remove_domain(domain)
        return {'success': ok, 'message': msg}

    def toggle_domain(self, domain, enabled):
        ok, msg = self._get_mgr().toggle_domain(domain, enabled)
        return {'success': ok, 'message': msg}

    def set_domain_route(self, domain, route):
        ok, msg = self._get_mgr().set_domain_route(domain, route)
        return {'success': ok, 'message': msg}

    def add_ip_range(self, cidr, route='ipv4'):
        ok, msg, info = self._get_mgr().add_ip_range(cidr, route=route)
        return {'success': ok, 'message': msg, 'info': info}

    def remove_ip_range(self, cidr):
        ok, msg = self._get_mgr().remove_ip_range(cidr)
        return {'success': ok, 'message': msg}

    def toggle_ip_range(self, cidr, enabled):
        ok, msg = self._get_mgr().toggle_ip_range(cidr, enabled)
        return {'success': ok, 'message': msg}

    def set_ip_range_route(self, cidr, route):
        ok, msg = self._get_mgr().set_ip_range_route(cidr, route)
        return {'success': ok, 'message': msg}

    def check_ipv6_support(self):
        """检测所有 IPv6 路由域名是否真的支持 IPv6，不支持则降级为 IPv4"""
        ok, msg, details = self._get_mgr().check_ipv6_support()
        return {'success': ok, 'message': msg, 'details': details}

    def set_connections_route(self, connections, route):
        """批量设置连接的路由类型。
        connections: [{hostname, remote_ip}, ...]
        route: 'ipv4' | 'ipv6' | 'warp'（warp=不直连，走WARP）
        有域名的用域名排除，无域名的用 IP 排除。
        修改后刷新 DNS 缓存，确保排除规则对新连接立即生效。
        """
        from warp_exclusion import warp_add_ip, warp_remove_ip
        mgr = self._get_mgr()
        results = []
        need_flush_dns = False  # 是否需要刷新 DNS 缓存
        for conn in connections:
            hostname = (conn.get('hostname') or '').strip()
            remote_ip = (conn.get('remote_ip') or '').strip()
            if not hostname and not remote_ip:
                results.append({'hostname': hostname, 'remote_ip': remote_ip,
                                'success': False, 'message': '无域名和IP'})
                continue
            try:
                if route == 'warp':
                    # 不直连：移除排除规则，让流量走 WARP
                    if hostname:
                        ok, msg = mgr.remove_domain(hostname)
                    else:
                        ok, msg = warp_remove_ip(remote_ip)
                    if ok:
                        need_flush_dns = True
                else:
                    # 直连：添加排除规则
                    if hostname:
                        # 域名可能已存在（之前已排除），先移除旧规则再添加，确保 route 类型正确切换
                        mgr.remove_domain(hostname)
                        ok, msg, _ = mgr.add_domain(hostname, route=route)
                    else:
                        ok, msg = warp_add_ip(remote_ip)
                    if ok:
                        need_flush_dns = True
            except Exception as e:
                ok, msg = False, str(e)
            results.append({'hostname': hostname, 'remote_ip': remote_ip,
                            'success': ok, 'message': msg})
        # 刷新系统 DNS 缓存，让排除规则对新连接立即生效
        # WARP 的 tunnel host add 只对新 DNS 查询生效，旧缓存会导致流量仍走 WARP
        if need_flush_dns:
            try:
                import subprocess
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                subprocess.Popen('ipconfig /flushdns', shell=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
                logger.info('DNS cache flushed after route change')
            except Exception as e:
                logger.warning(f'Failed to flush DNS cache: {e}')
        success_count = sum(1 for r in results if r['success'])
        total = len(results)
        return {
            'success': success_count == total,
            'message': f'成功 {success_count}/{total}',
            'results': results,
        }

    def start_learning(self):
        ok, msg = self._get_mgr().dns_monitor.start_learning()
        return {'success': ok, 'message': msg}

    def stop_learning(self):
        ok, msg = self._get_mgr().dns_monitor.stop_learning()
        return {'success': ok, 'message': msg}

    def get_learned_domains(self):
        return self._get_mgr().dns_monitor.get_learned_domains()

    def apply_to_warp(self, domain=None):
        ok, msg, details = self._get_mgr().apply_to_warp(domain)
        return {'success': ok, 'message': msg, 'details': details}

    def sync_from_warp(self):
        ok, msg, details = self._get_mgr().sync_from_warp()
        return {'success': ok, 'message': msg, 'details': details}

    def get_warp_ranges(self):
        return self._get_mgr().get_warp_ranges()

    def get_cli_ip_ranges(self):
        """获取 CLI 添加的 IP 规则，区分使用中和残留"""
        from warp_exclusion import warp_list_ip_ranges, load_exclusion_config, _resolve_ipv6_prefixes
        cli_ranges, _ = warp_list_ip_ranges()
        cfg = load_exclusion_config()
        # 收集域名规则自动生成的 IPv6 CIDR
        active_ipv6 = set()
        for entry in cfg.get('domains', []):
            if entry.get('enabled', True) and entry.get('route', 'ipv6') == 'ipv6':
                prefixes = _resolve_ipv6_prefixes(entry['domain'])
                active_ipv6.update(prefixes)
        # 收集 IP 范围管理中启用的 CIDR
        active_ip_ranges = set()
        for entry in cfg.get('ip_ranges', []):
            if entry.get('enabled', True):
                active_ip_ranges.add(entry['cidr'])
        # 分类：使用中 vs 残留
        active = [r for r in cli_ranges if r in active_ipv6 or r in active_ip_ranges]
        legacy = [r for r in cli_ranges if r not in active_ipv6 and r not in active_ip_ranges]
        return {'active_ipv6': sorted(active), 'legacy': sorted(legacy)}

    def cleanup_legacy_config(self):
        from warp_exclusion import warp_cleanup_cli_ip_ranges
        ok, msg, details = warp_cleanup_cli_ip_ranges()
        return {'success': ok, 'message': msg, 'details': details}

    def add_dns_fallback(self, domain):
        ok, msg, info = self._get_mgr().add_dns_fallback(domain)
        return {'success': ok, 'message': msg, 'info': info}

    def remove_dns_fallback(self, domain):
        ok, msg = self._get_mgr().remove_dns_fallback(domain)
        return {'success': ok, 'message': msg}

    def toggle_dns_fallback(self, domain, enabled):
        ok, msg = self._get_mgr().toggle_dns_fallback(domain, enabled)
        return {'success': ok, 'message': msg}

    def apply_dns_fallback_to_warp(self):
        ok, msg, details = self._get_mgr().apply_dns_fallback_to_warp()
        return {'success': ok, 'message': msg, 'details': details}

    def get_dns_fallback_list(self):
        return self._get_mgr().get_dns_fallback_list()

    def is_ipv4_enabled(self):
        return self._get_mgr().is_ipv4_enabled()

    def set_ipv4_enabled(self, enabled):
        ok, msg = self._get_mgr().set_ipv4_enabled(enabled)
        return {'success': ok, 'message': msg}

    def get_auto_enable_ipv4(self):
        return bool(CONFIG_STORE.get('auto_enable_ipv4', True))

    def set_auto_enable_ipv4(self, enabled):
        from warp_exclusion import load_exclusion_config, save_exclusion_config
        value = bool(enabled)
        CONFIG_STORE.patch({'auto_enable_ipv4': value})
        # Keep the exclusion manager's legacy field compatible for upgrades.
        cfg = load_exclusion_config()
        cfg['auto_enable_ipv4'] = value
        save_exclusion_config(cfg)
        return {'success': True, 'message': '已更新'}

    # ------------------------------------------------------------------
    # 流量监控 API（供 settings.html 流量tab 调用）
    # ------------------------------------------------------------------
    def get_traffic_status(self):
        """获取当前网络流量走向统计和连接详情"""
        _t0 = time.time()
        try:
            result = get_traffic_status()
            _elapsed = time.time() - _t0
            logger.info(f"[get_traffic_status] OK, elapsed={_elapsed:.2f}s, total={result.get('total', 0)}")
            return result
        except Exception as e:
            logger.error(f"[get_traffic_status] FAILED: {e}\n{traceback.format_exc()}")
            raise

    def get_traffic_status_fast(self):
        """快速获取流量统计（不含域名），供前端首屏展示。"""
        _t0 = time.time()
        try:
            result = get_traffic_status_fast()
            _elapsed = time.time() - _t0
            logger.info(f"[get_traffic_status_fast] OK, elapsed={_elapsed:.2f}s, total={result.get('total', 0)}")
            return result
        except Exception as e:
            logger.error(f"[get_traffic_status_fast] FAILED: {e}\n{traceback.format_exc()}")
            raise

    def get_traffic_status_slow(self, missing_ips):
        """获取 IP→域名映射，供前端增量更新域名显示。"""
        _t0 = time.time()
        try:
            result = get_traffic_status_slow(missing_ips)
            _elapsed = time.time() - _t0
            logger.info(f"[get_traffic_status_slow] OK, elapsed={_elapsed:.2f}s, resolved={len(result)}")
            return result
        except Exception as e:
            logger.error(f"[get_traffic_status_slow] FAILED: {e}\n{traceback.format_exc()}")
            raise

    def save_ui_prefs(self, prefs):
        """保存界面偏好，包括分页、视图模式、详情折叠状态、当前标签页和主题。"""
        try:
            allowed = {'page_size', 'traffic_subview', 'network_detail_collapsed', 'active_tab', 'theme'}
            clean = {key: value for key, value in (prefs or {}).items() if key in allowed}
            if clean.get('theme') not in ('light', 'dark', 'system'):
                clean.pop('theme', None)
            current = CONFIG_STORE.get('ui_prefs') or {}
            current.update(clean)
            saved = CONFIG_STORE.patch({'ui_prefs': current})
            logger.info(f"[save_ui_prefs] Saved: {clean}, merged: {current}")
            return {'success': True, 'revision': saved.get('_revision')}
        except Exception as e:
            logger.error(f"[save_ui_prefs] FAILED: {e}\n{traceback.format_exc()}")
            return {'success': False}

    def get_ui_prefs(self):
        """读取界面偏好，供前端初始化。"""
        fallback = {'page_size': 20, 'traffic_subview': 'list',
                    'network_detail_collapsed': False, 'active_tab': 'home',
                    'theme': 'system'}
        try:
            prefs = load_config().get('ui_prefs') or {}
            result = {
                'page_size': int(prefs.get('page_size', 20)),
                'traffic_subview': prefs.get('traffic_subview', 'list'),
                'network_detail_collapsed': bool(prefs.get('network_detail_collapsed', False)),
                'active_tab': prefs.get('active_tab', 'home'),
                'theme': prefs.get('theme', 'system'),
            }
            if result['page_size'] not in (10, 20, 50, 100):
                result['page_size'] = 20
            if result['traffic_subview'] not in ('list', 'canvas'):
                result['traffic_subview'] = 'list'
            if result['active_tab'] not in ('home', 'workflow', 'warp', 'traffic', 'settings'):
                result['active_tab'] = 'home'
            if result['theme'] not in ('light', 'dark', 'system'):
                result['theme'] = 'system'
            return result
        except Exception as e:
            logger.error(f"[get_ui_prefs] FAILED: {e}\n{traceback.format_exc()}")
            return fallback

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

icon_instance = None

def on_settings(icon, item):
    logger.info("User clicked: Settings")

def on_auth(icon, item):
    _start_tray_workflow('default_auth', icon)


def _start_tray_workflow(workflow_id, icon=None):
    definition = (load_config().get('workflows') or {}).get(workflow_id, {})
    name = definition.get('name', workflow_id)
    if icon:
        icon.icon = create_icon('orange')
        icon.title = f'正在执行：{name}'
        icon.notify(f'正在执行工作流：{name}', '校园网助手')
    threading.Thread(target=_run_tray_workflow, args=(workflow_id, icon, name), daemon=True).start()


def _run_tray_workflow(workflow_id, icon, name):
    if not _auth_lock.acquire(blocking=False):
        if icon:
            icon.notify('工作流正在进行中，请稍候', '校园网助手')
            icon.icon = create_icon('green')
            icon.title = '校园网助手'
        return
    try:
        success, msg = run_workflow_by_id(workflow_id)
        if icon:
            if success:
                icon.icon = create_icon('orange')
                icon.title = name
                icon.notify(msg, '校园网助手')
            else:
                icon.icon = create_icon('red')
                icon.title = '工作流失败'
                icon.notify(f'失败: {msg}', '校园网助手')
    except Exception as e:
        logger.exception("Workflow %s error", workflow_id)
        if icon:
            icon.icon = create_icon('red')
            icon.title = '错误'
            icon.notify(f'错误: {e}', '校园网助手')
    finally:
        _auth_lock.release()

def on_restore(icon, item):
    logger.info("User clicked: Restore Normal")
    icon.icon = create_icon('green')
    icon.title = '正在恢复...'
    icon.notify('正在恢复网络到正常模式...', '校园网助手')
    threading.Thread(target=_run_restore, args=(icon,), daemon=True).start()

def _run_restore(icon):
    if not _auth_lock.acquire(blocking=False):
        icon.notify('操作正在进行中，请稍候', '校园网助手')
        return
    try:
        success, msg = run_restore_task()
        if success:
            icon.icon = create_icon('green')
            icon.title = '已恢复正常'
            icon.notify(msg, '校园网助手')
        else:
            icon.icon = create_icon('red')
            icon.title = '恢复失败'
            icon.notify(f'失败: {msg}', '校园网助手')
    except Exception as e:
        logger.error(f"Restore error: {e}")
        icon.icon = create_icon('red')
        icon.title = '错误'
        icon.notify(f'错误: {e}', '校园网助手')
    finally:
        _auth_lock.release()

def on_reauth(icon, item):
    _start_tray_workflow('portal_reauth', icon)

def on_exit(icon, item):
    logger.info("on_exit: user clicked Exit")
    if core.state._tray_app_instance:
        core.state._tray_app_instance.request_exit()
    else:
        cleanup_wifi_event()
        icon.stop()
        if core.state.TRAY_MUTEX:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle(core.state.TRAY_MUTEX)
            core.state.TRAY_MUTEX = None
    logger.info("on_exit: application exiting")
    # 退出应用，允许 atexit 和 finally 执行清理
    sys.exit(0)

def on_show_log(icon, item):
    logger.info("User clicked: Show Log")
    if LOG_FILE.exists():
        os.startfile(str(LOG_FILE))
    else:
        icon.notify('日志文件不存在', '校园网助手')

def on_setup_admin(icon, item):
    logger.info("User clicked: Setup Admin Startup")
    if not is_admin():
        icon.notify('请先以管理员身份运行', '校园网助手')
        return
    if setup_startup_task():
        icon.notify('开机自启动已设置', '校园网助手')
    else:
        icon.notify('设置开机自启动失败', '校园网助手')

class TrayApp:
    # 首次启动默认尺寸（屏幕85%），实际从配置读取
    MIN_W = 1210
    MIN_H = 770

    def __init__(self, silent=False):
        self.icon = None
        self.api = ApiBridge()
        self.settings_window = None
        self._geometry_save_timer = None
        self._should_exit = False
        self._silent = silent
        self._webview_started = False
        self._webview_start_event = threading.Event()
        self._init_done = False
        self._state_unsubscribe = None

    def calc_initial_window_geometry(self):
        """计算初始窗口几何（含最大化标记），支持多显示器坐标并在屏幕拔出时安全回退。

        返回 (width, height, x, y, maximized)。
        """
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        default_w, default_h = screen_w * 85 // 100, screen_h * 85 // 100
        default_x, default_y = (screen_w - default_w) // 2, (screen_h - default_h) // 2
        cfg = load_config()
        saved = cfg.get('window')
        if saved and isinstance(saved, dict):
            try:
                w = int(saved.get('width', default_w))
                h = int(saved.get('height', default_h))
                x = int(saved.get('x', default_x))
                y = int(saved.get('y', default_y))
            except (TypeError, ValueError):
                w, h, x, y = default_w, default_h, default_x, default_y
            if _valid_window_geometry(w, h, x, y):
                maximized = bool(saved.get('maximized'))
                logger.info(f"[window_geometry] From config: {w}x{h} at ({x},{y}), maximized={maximized}")
                return w, h, x, y, maximized
            logger.warning('[window_geometry] Saved geometry unavailable, using primary screen center')
        logger.info(f"[window_geometry] Default 85%: {default_w}x{default_h} at ({default_x},{default_y})")
        return default_w, default_h, default_x, default_y, False

    def save_window_geometry(self):
        """保存当前窗口尺寸和位置到配置（GetWindowPlacement 规范做法）。"""
        try:
            if not self.settings_window:
                return
            captured = _capture_window_geometry()
            if captured is None:
                x = self.settings_window.x
                y = self.settings_window.y
                w = self.settings_window.width
                h = self.settings_window.height
                captured = {'width': int(w), 'height': int(h),
                            'x': int(x), 'y': int(y), 'maximized': False}
            if _valid_window_geometry(captured['width'], captured['height'],
                                      captured['x'], captured['y']):
                CONFIG_STORE.patch({'window': captured})
                state = 'maximized' if captured['maximized'] else 'normal'
                logger.info(f"[save_window_geometry] Saved ({state}): "
                            f"{captured['width']}x{captured['height']} at ({captured['x']},{captured['y']})")
            else:
                logger.warning(f"[save_window_geometry] Ignored abnormal: "
                               f"{captured['width']}x{captured['height']} at ({captured['x']},{captured['y']})")
        except Exception as e:
            logger.error(f"[save_window_geometry] FAILED: {e}")

    def _schedule_geometry_save(self, delay=0.4):
        """防抖保存窗口几何：move/resize 事件高频触发，仅保留最后一次。

        pywebview 的 Window.x/y/width/height 属性更新滞后，
        save_window_geometry 内部始终用 Win32 实时矩形，保证保存的是最新几何。
        """
        try:
            if self._geometry_save_timer is not None:
                self._geometry_save_timer.cancel()
            self._geometry_save_timer = threading.Timer(delay, self.save_window_geometry)
            self._geometry_save_timer.daemon = True
            self._geometry_save_timer.start()
        except Exception as e:
            logger.debug(f"_schedule_geometry_save failed: {e}")

    def request_exit(self):
        """统一的退出流程（托盘菜单"退出"与更新安装共用）。"""
        logger.info("request_exit: shutting down")
        self._should_exit = True
        if not self._webview_started:
            self._webview_start_event.set()
        # 先保存窗口几何，再销毁窗口
        try:
            self.save_window_geometry()
        except Exception as e:
            logger.debug(f"request_exit: save geometry failed: {e}")
        for win_attr in ('settings_window',):
            win = getattr(self, win_attr, None)
            if win:
                try:
                    win.destroy()
                except Exception as e:
                    logger.debug(f"request_exit: destroy {win_attr} failed: {e}")
                setattr(self, win_attr, None)
        cleanup_wifi_event()
        if self.icon:
            try:
                self.icon.stop()
            except Exception as e:
                logger.debug(f"request_exit: icon.stop failed: {e}")
        if core.state.TRAY_MUTEX:
            try:
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.CloseHandle(core.state.TRAY_MUTEX)
                core.state.TRAY_MUTEX = None
                logger.debug("request_exit: mutex released")
            except Exception as e:
                logger.debug(f"request_exit: mutex release failed: {e}")
        logger.info("request_exit: application exiting")

    def _apply_app_state(self, state):
        """Apply one state snapshot to both tray and the visible window."""
        try:
            operation = state.get('operation', {})
            network = state.get('network', {})
            if operation.get('status') == 'running':
                color = 'orange'
                title = operation.get('message') or '正在处理'
            else:
                status = network.get('status', 'unknown')
                color = {
                    'connected': 'orange', 'partial': 'orange', 'normal': 'green',
                    'broken': 'red', 'disconnected': 'gray', 'unknown': 'gray',
                }.get(status, 'gray')
                title = network.get('message') or '校园网助手'
            if self.icon:
                self.icon.icon = create_icon(color)
                self.icon.title = str(title)[:120]
            if self.settings_window:
                payload = json.dumps(state, ensure_ascii=False)
                window = self.settings_window
                def push_to_window():
                    try:
                        window.evaluate_js(f'onAppState({payload})')
                    except Exception as exc:
                        logger.debug('Failed to push app state to window: %s', exc)
                threading.Thread(target=push_to_window, name='state-to-webview', daemon=True).start()
        except Exception as exc:
            logger.debug('Failed to apply app state: %s', exc)

    def _start_state_sync(self):
        if self._state_unsubscribe is None:
            self._state_unsubscribe = app_state.subscribe(self._apply_app_state)
        network_status.start()

    def _stop_state_sync(self):
        network_status.stop()
        if self._state_unsubscribe:
            self._state_unsubscribe()
            self._state_unsubscribe = None

    @staticmethod
    def _make_workflow_handler(workflow_id):
        """pystray 菜单回调签名只允许 (icon, item) 两个位置参数，
        用工厂闭包绑定 workflow_id，避免第三个参数触发 ValueError。"""
        def handler(icon, item):
            _start_tray_workflow(workflow_id, icon)
        return handler

    def _workflow_menu_items(self):
        items = []
        workflows = list((load_config().get('workflows') or {}).values())
        workflows.sort(key=lambda item: (not item.get('built_in', False), item.get('id', '')))
        for workflow in workflows:
            if not workflow.get('tray_menu', True):
                continue
            steps = workflow.get('steps') or []
            if not any(step.get('enabled', True) for step in steps if isinstance(step, dict)):
                continue
            workflow_id = workflow['id']

            items.append(pystray.MenuItem(
                str(workflow.get('name') or workflow_id),
                self._make_workflow_handler(workflow_id)))
        return items

    def _build_menu_items(self, startup_enabled):
        menu_items = [
            pystray.MenuItem('显示主窗口', lambda i, item: self.show_settings()),
            pystray.Menu.SEPARATOR,
            *self._workflow_menu_items(),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('恢复正常模式', on_restore),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('WARP排除', lambda i, item: self.show_main_window('warp')),
            pystray.MenuItem('流量', lambda i, item: self.show_main_window('traffic')),
            pystray.MenuItem('打开主页', lambda i, item: self.show_main_window('home')),
            pystray.Menu.SEPARATOR,
        ]
        if not is_admin():
            menu_items.append(pystray.MenuItem('以管理员身份运行', lambda i, item: elevate_if_needed()))
            menu_items.append(pystray.Menu.SEPARATOR)
        startup_label = '取消开机自启' if startup_enabled else '设置开机自启'
        menu_items.extend([
            pystray.MenuItem(startup_label, self._toggle_startup),
            pystray.MenuItem('查看日志', on_show_log),
            pystray.MenuItem('退出', on_exit),
        ])
        return menu_items

    def create_tray(self):

        self.icon = pystray.Icon('wifi_auto_auth')
        self.icon.icon = create_icon('gray')
        self.icon.title = '校园网助手'
        startup_enabled = load_config().get('auto_startup', False)
        menu_items = self._build_menu_items(startup_enabled)
        self.icon.menu = pystray.Menu(*menu_items)
        self.icon.on_activate = self._on_tray_activate
        # Monkey-patch pystray 的消息处理器，让左键单击也触发 on_activate
        self._patch_pystray_click()

    def _on_tray_activate(self, icon):
        """处理托盘图标激活事件"""
        logger.info("[tray_activate] Tray icon activated, calling show_settings()")
        try:
            self.show_settings()
            logger.info("[tray_activate] show_settings completed")
        except Exception as e:
            logger.error(f"[tray_activate] ERROR: {e}\n{traceback.format_exc()}")

    def _patch_pystray_click(self):
        """修改 pystray 实例的 _message_handlers，让左键单击直接显示窗口而不是显示菜单"""
        try:
            from pystray._win32 import win32
            WM_NOTIFY = win32.WM_NOTIFY
            original_on_notify = self.icon._message_handlers[WM_NOTIFY]

            app_ref = self

            def patched_on_notify(wparam, lparam):
                if lparam == win32.WM_LBUTTONUP:
                    logger.info("[pystray_patch] Left click detected, showing window")
                    app_ref.show_settings()
                    return
                original_on_notify(wparam, lparam)

            self.icon._message_handlers[WM_NOTIFY] = patched_on_notify
            logger.info("[pystray_patch] Successfully patched pystray _on_notify on instance")
        except Exception as e:
            logger.warning(f"[pystray_patch] Failed to patch pystray: {e}")

    def _toggle_startup(self, icon, item):
        enabled = check_startup_status()
        if enabled:
            logger.info("User clicked: Cancel Startup")
            if remove_startup_task():
                icon.notify('开机自启已取消', '校园网助手')
                self._refresh_tray_menu()
            else:
                icon.notify('取消开机自启失败', '校园网助手')
        else:
            logger.info("User clicked: Setup Startup")
            if not is_admin():
                icon.notify('请先以管理员身份运行', '校园网助手')
                return
            if setup_startup_task():
                icon.notify('开机自启已设置', '校园网助手')
                self._refresh_tray_menu()
            else:
                icon.notify('设置开机自启失败', '校园网助手')

    def _refresh_tray_menu(self):
        try:
            startup_enabled = load_config().get('auto_startup', False)
            menu_items = self._build_menu_items(startup_enabled)
            self.icon.menu = pystray.Menu(*menu_items)
            logger.debug("Tray menu refreshed, startup=%s, workflows=%s",
                         'enabled' if startup_enabled else 'disabled',
                         len(self._workflow_menu_items()))
        except Exception as e:
            logger.error(f"_refresh_tray_menu failed: {e}")

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

    def show_settings(self, tab=None):
        """显示应用窗口。tab参数保留但不再使用，窗口保持上次的状态。"""
        logger.info(f"[show_settings] Called, webview_started={self._webview_started}, window={self.settings_window}")
        if not self._webview_started:
            logger.info("[show_settings] WebView2 not started yet, triggering lazy init...")
            self._webview_start_event.set()
            return
        if self.settings_window:
            try:
                self.settings_window.show()
                # 最大化状态下不要 restore（会把最大化窗口还原成普通大小）
                if not _is_window_zoomed():
                    self.settings_window.restore()
                logger.info("[show_settings] Window shown via pywebview")

                hwnd = _find_main_hwnd()
                if hwnd:
                    logger.info(f"[show_settings] Found window hwnd={hwnd}")
                    SW_RESTORE = 9
                    SW_SHOW = 5
                    HWND_TOPMOST = -1
                    HWND_NOTOPMOST = -2
                    SWP_NOMOVE = 0x0002
                    SWP_NOSIZE = 0x0001
                    SWP_SHOWWINDOW = 0x0040
                    # 已最大化的窗口用 SW_SHOW，避免 SW_RESTORE 撤销最大化
                    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if _is_window_zoomed() else SW_RESTORE)
                    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    ctypes.windll.user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                    logger.info("[show_settings] Window brought to front via Win32 API")
                else:
                    logger.warning("[show_settings] Window not found via FindWindowW")
                logger.info("[show_settings] Window shown successfully")
            except Exception as e:
                logger.error(f"[show_settings] FAILED: {e}\n{traceback.format_exc()}")
        else:
            logger.error("[show_settings] settings_window is None, cannot show!")

    def run(self):
        core.state._tray_app_instance = self
        cfg = load_config()
        self.create_tray()
        self._start_state_sync()
        logger.info(f"Tray started (admin: {is_admin()})")

        tray_thread = threading.Thread(target=self.icon.run, daemon=True)
        tray_thread.start()

        def delayed_init():
            cfg = load_config()
            
            if cfg.get('auto_startup'):
                if is_admin():
                    if not check_startup_status():
                        logger.info("auto_startup=True but task missing, re-registering")
                        setup_startup_task()
                else:
                    logger.info("auto_startup=True but not admin, cannot verify/register startup task")
            else:
                if check_startup_status():
                    logger.info("auto_startup=False but task exists, removing")
                    remove_startup_task()

            if cfg.get('auto_auth') or cfg.get('auto_restore'):
                start_wifi_event_monitor()
                if is_admin():
                    if register_wifi_event_task():
                        logger.info("WiFi event task registered on startup")
                    else:
                        logger.warning("Failed to register WiFi event task on startup")
                else:
                    logger.info("Not admin, skipping WiFi event task registration")
                if cfg.get('auto_auth'):
                    check_startup_wifi_and_auth()
                else:
                    _update_tray_status()
            else:
                _update_tray_status()
            self._init_done = True

        if self._silent:
            logger.info("Silent mode: starting delayed_init first, WebView2 will load on demand")
            init_thread = threading.Thread(target=delayed_init, daemon=True)
            init_thread.start()

            self._webview_start_event.wait()

            logger.info("Silent mode: WebView2 init triggered, starting now...")

        # 优先加载 Vue3 前端构建产物，回退到旧版 settings.html
        html_file = get_resource_path('settings.html')
        dist_index = get_resource_path('frontend/dist/index.html')
        if os.path.isfile(dist_index):
            html_file = dist_index
            logger.info(f"run: using Vue frontend at {dist_index}")
        logger.debug(f"run: html_file={html_file}")

        # 从配置读取窗口几何，否则按屏幕85%居中
        win_w, win_h, wx, wy, win_maximized = self.calc_initial_window_geometry()
        self._restore_maximized = win_maximized

        # pywebview 6.x 约定：create_window 的 width/height/x/y 为逻辑像素，
        # WinForms 层内部会乘以 DPI 缩放转为物理像素。保存的几何是物理像素
        # （GetWindowRect/GetWindowPlacement），必须先除以缩放比例，
        # 否则高 DPI 屏上每次重启窗口都会放大 25%（缩放 1.25 时）。
        scale = _dpi_scale()
        logical_w = round(win_w / scale)
        logical_h = round(win_h / scale)
        logical_x = round(wx / scale)
        logical_y = round(wy / scale)

        try:
            html_url = f'file:///{html_file.replace(chr(92), "/")}'
            self.settings_window = webview.create_window(
                'CampusAuth',
                url=html_url,
                js_api=self.api,
                width=logical_w,
                height=logical_h,
                x=logical_x,
                y=logical_y,
                resizable=True,
                min_size=(self.MIN_W, self.MIN_H),
                background_color='#0D0D0D',
                easy_drag=False,  # 关闭全局拖动，改用 pywebview-drag-region 类精确控制可拖动区域
                frameless=True,
                hidden=self._silent
            )
            logger.info(f"Window created: physical {win_w}x{win_h} at ({wx},{wy}) -> "
                        f"logical {logical_w}x{logical_h} at ({logical_x},{logical_y}) "
                        f"(dpi scale {scale:g}), url={html_url}")
        except Exception as e:
            logger.error(f"run: create_window failed: {e}\n{traceback.format_exc()}")
            return

        def on_closing():
            logger.info("[on_closing] Window closing event triggered")
            try:
                self.save_window_geometry()
            except Exception as e:
                logger.error(f"[on_closing] save_window_geometry failed: {e}")
            if self._should_exit:
                logger.info("[on_closing] Real exit requested, allowing close")
                return None
            logger.info("[on_closing] Hiding window to tray (not closing)")
            try:
                self.settings_window.hide()
                logger.info("[on_closing] Window hidden successfully")
            except Exception as e:
                logger.error(f"[on_closing] Hide failed: {e}")
            return False
        
        self.settings_window.events.closing += on_closing

        # 窗口移动/缩放/最小化/还原时防抖保存几何（读取 Win32 实时矩形），
        # 解决 pywebview 属性滞后与前端轮询间隔导致的"位置大小没记住"问题
        self.settings_window.events.moved += lambda *args: self._schedule_geometry_save()
        self.settings_window.events.resized += lambda *args: self._schedule_geometry_save()
        self.settings_window.events.restored += lambda *args: self._schedule_geometry_save()
        self.settings_window.events.maximized += lambda *args: self._schedule_geometry_save()
        self.settings_window.events.minimized += lambda *args: self._schedule_geometry_save()

        _icon_handles = []

        def set_window_icon():
            try:
                ico_path = ensure_app_icon()
                hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if not hwnd:
                    time.sleep(0.3)
                    hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if hwnd and os.path.isfile(ico_path):
                    WM_SETICON = 0x0080
                    ICON_BIG = 1
                    ICON_SMALL = 0
                    LR_LOADFROMFILE = 0x00000010
                    IMAGE_ICON = 1
                    hicon_small = ctypes.windll.user32.LoadImageW(
                        None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
                    )
                    hicon_big = ctypes.windll.user32.LoadImageW(
                        None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
                    )
                    _icon_handles.extend([hicon_small, hicon_big])
                    if hicon_small:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                    if hicon_big:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                    child = ctypes.windll.user32.FindWindowExW(hwnd, None, None, None)
                    while child:
                        if hicon_small:
                            ctypes.windll.user32.SendMessageW(child, WM_SETICON, ICON_SMALL, hicon_small)
                        if hicon_big:
                            ctypes.windll.user32.SendMessageW(child, WM_SETICON, ICON_BIG, hicon_big)
                        child = ctypes.windll.user32.FindWindowExW(hwnd, child, None, None)
                    if hicon_small or hicon_big:
                        logger.debug(f"set_window_icon: icon set, hwnd={hwnd}, small={hicon_small}, big={hicon_big}")
                    else:
                        logger.debug("set_window_icon: LoadImageW returned None for both sizes")
                else:
                    logger.debug(f"set_window_icon: hwnd={hwnd}, ico_exists={os.path.isfile(ico_path)}")
            except Exception as e:
                logger.debug(f"set_window_icon: {e}")

        self.settings_window.events.shown += set_window_icon

        # 恢复上次的最大化状态（在窗口显示后应用，普通态几何已在创建时恢复）
        def restore_maximized_state():
            if not getattr(self, '_restore_maximized', False):
                return
            self._restore_maximized = False
            hwnd = _find_main_hwnd()
            if hwnd:
                SW_MAXIMIZE = 3
                ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
                logger.info("[restore_maximized_state] Window maximized per saved state")

        self.settings_window.events.shown += restore_maximized_state

        if not self._silent:
            def ensure_visible():
                try:
                    time.sleep(0.5)
                    if self.settings_window:
                        self.settings_window.show()
                        # 最大化状态下不要 restore（会撤销 restore_maximized_state）
                        if not _is_window_zoomed():
                            self.settings_window.restore()
                        logger.info("Non-silent mode: window ensured visible")
                except Exception as e:
                    logger.error(f"Non-silent mode ensure visible failed: {e}")
            threading.Thread(target=ensure_visible, daemon=True).start()

            threading.Thread(target=delayed_init, daemon=True).start()

        self._webview_started = True
        webview.start(debug=False)
        self._stop_state_sync()
        
        if self.icon:
            self.icon.stop()

def main():
    # 必须在任何窗口创建与屏幕指标读取之前声明 DPI 感知，
    # 保证窗口几何的保存/校验/恢复全程使用同一套物理像素坐标
    _enable_dpi_awareness()
    logger.info("=" * 50)
    logger.info("WiFi Auto-Auth App Starting")
    logger.info(f"SCRIPT_DIR: {SCRIPT_DIR}")
    logger.info(f"CONFIG_FILE: {CONFIG_FILE}")
    logger.info(f"LOG_FILE: {LOG_FILE}")
    logger.info(f"Running as admin: {is_admin()}")
    logger.info(f"sys.frozen: {getattr(sys, 'frozen', False)}")
    logger.info("=" * 50)
    if not is_admin():
        logger.info("Not running as admin, elevating...")
        elevate_if_needed()
        return
    core.state.TRAY_MUTEX = check_single_instance()
    hide_console()
    silent = '--silent' in sys.argv
    if silent:
        logger.info("Silent startup mode enabled")
    app = TrayApp(silent=silent)
    app.run()

if __name__ == '__main__':
    if '--wifi-event' in sys.argv:
        logger.info("WiFi connection event triggered by system")
        if signal_wifi_event():
            logger.info("Signal sent to running app, exiting")
        else:
            logger.warning("Tray app not running, cannot signal, exiting")
    else:
        main()
