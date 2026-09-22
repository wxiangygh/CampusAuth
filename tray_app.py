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
from core.newest_first_log import NewestFirstFileHandler
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
    has_public_ipv6, resolve_active_interface,
)
from core.warp_manager import (
    get_warp_cli, connect_warp, disconnect_warp,
    _set_warp_masque_mode,
    update_tray_icon, update_tray_icon_restore,
)
from core.auth import (
    preempt_auth_lock,
    portal_login, portal_logout, disable_ipv4, enable_ipv4,
    _push_auth_progress, _check_cancel, _interruptible_sleep,
    run_auth_task, run_restore_task, _js_escape, _is_cancelled,
)
from core.startup import (
    check_single_instance, setup_startup_task, remove_startup_task,
    check_startup_status, register_wifi_event_task, unregister_wifi_event_task,
    wifi_event_monitor, start_wifi_event_monitor, cleanup_wifi_event,
    signal_wifi_event, _create_event_with_acl, check_startup_wifi_and_auth,
    should_run_boot_auth, _update_tray_status, elevate_if_needed, hide_console,
    _build_schtasks_tr,
)
from core.config import configure_config, get_config_store, DEFAULT_AUTH_WORKFLOW
from core.app_state import app_state
from core.updater import (UpdateDownloader, check_for_update as _check_for_update,
                          cleanup_temp_files, install_update as _install_update,
                          resolve_install_dir, validate_install_dir)
from core.version import __version__
from core.status import network_status
from core.native_theme import (set_menu_dark_mode, theme_popup_menu_window,
                               destroy_menu_window)
from core.native_window import enable_native_window_behaviors_with_retry
from core.reconnect_watchdog import start_watchdog, stop_watchdog
from core.auth_workflow import (
    apply_auto_tune, run_workflow_by_id, validate_auth_workflow, workflow_catalog,
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

# 退出 hook（exit_hook_workflow 绑定的工作流）的整体执行上限（秒）。
# 各节点自带超时，这里只是兜底：hook 卡死时不能让用户永远退不出程序。
EXIT_HOOK_TIMEOUT = 120
# 等待在途认证/恢复操作让出 _auth_lock 的时间（秒）。
# 退出 hook 会改网络状态，与在途操作并发执行会互相踩踏。
EXIT_HOOK_LOCK_WAIT = 5

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        NewestFirstFileHandler(LOG_FILE, max_bytes=2*1024*1024),
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


def _wf_label(workflow_id):
    """日志里工作流的可读名：优先用户保存的名字，id 仅作补充。

    配置里 id 和 name 可以完全不同（如 id=注销并重新认证_副本、
    name=注销并校园网认证（有线）），只打 id 时用户在日志里对不上号。
    """
    try:
        definition = (CONFIG_STORE.get('workflows') or {}).get(workflow_id) or {}
        name = str(definition.get('name') or workflow_id)
        return name if name == str(workflow_id) else f'{name}（{workflow_id}）'
    except Exception:
        return str(workflow_id)

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

# 托盘主题状态：dark 跟随前端保存的主题（ui_prefs.theme_dark），
# last_color 记录最近一次的状态色，主题切换时按原状态色重绘
_TRAY_THEME = {'dark': True, 'last_color': 'gray'}


def apply_tray_theme(dark=None):
    """同步托盘原生菜单配色跟随应用主题（图标固定黑底白字，不随主题）。"""
    if dark is not None:
        _TRAY_THEME['dark'] = bool(dark)
        # 进程级菜单深色偏好（SetPreferredAppMode，老系统静默降级）
        set_menu_dark_mode(_TRAY_THEME['dark'])
        # 立即刷新已存在的菜单窗口，不等下一次右键显示
        theme_popup_menu_window(_TRAY_THEME['dark'])
        logger.info('[tray] 托盘菜单已按主题切换（dark=%s）', _TRAY_THEME['dark'])


def create_icon(color='orange'):
    """CAuth 托盘图标：满幅圆角方形徽章 + 粗体 "C" 字标，固定黑底白字。

    参考现代开发工具（ZCode 等）的图标风格：满幅圆角徽章、粗壮几何字标、
    无彩色描边与多余彩色元素——小尺寸（16px 任务栏）下饱满、简洁、大方。
    图标不随应用主题变动（深浅任务栏上均清晰可辨）；
    color 参数仅为兼容旧调用保留，运行状态由托盘提示文字（title/notify）表达。
    """
    size = (64, 64)
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    badge = (26, 26, 30, 255)
    glyph = (255, 255, 255)

    # 徽章：满幅圆角方形，无描边
    draw.rounded_rectangle([1, 1, 63, 63], radius=16, fill=badge)
    # 粗体 "C" 字标：占徽章约 2/3、笔画加粗，开口朝右。
    # 相对徽章整体右移 3px：开口在右侧会让可见笔画的重心偏左，
    # 右移后视觉上才是居中的（光学居中，而非几何居中）。
    draw.arc([13, 10, 55, 52], start=40, end=320, fill=glyph, width=12)
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


# ===== WiFi 持续扫描会话 =====
# 设置页「扫描」改为持续扫描：后台线程逐轮触发 WlanScan 并把增量结果
# 通过 evaluate_js 推给前端（onWifiScanUpdate），列表随之动态填充，
# 直到用户点「停止」或达到时限。旧的一次性 scan_wifi API 保留兼容。
WIFI_SCAN_MAX_SECONDS = 120   # 持续扫描时限，防止忘记停止长期占用无线网卡
WIFI_SCAN_ROUND_PAUSE = 1.5   # 每轮主动扫描之间的间隔（秒）

_WIFI_SCAN = {'thread': None, 'stop': None}
_WIFI_SCAN_LOCK = threading.Lock()


def _tray_workflow_ids(config):
    """按托盘显示顺序返回工作流 id 列表（含托盘可见性与启用节点过滤）。

    排序：tray_order 升序（未设置/0 视为排在最后）→ 内置优先 → id。
    用户在设置页「托盘工作流顺序」弹窗中调整后，tray_order 会被整体
    重排为 1..n；托盘原生菜单据此加序号展示。
    """
    tray = []
    for workflow in (config.get('workflows') or {}).values():
        if not workflow.get('tray_menu', True):
            continue
        steps = workflow.get('steps') or []
        if not any(step.get('enabled', True) for step in steps if isinstance(step, dict)):
            continue
        tray.append(workflow)
    tray.sort(key=lambda w: (int(w.get('tray_order') or 0) or 10 ** 9,
                             not w.get('built_in', False), str(w.get('id', ''))))
    return [w['id'] for w in tray]


def _push_wifi_scan_event(payload: dict):
    try:
        instance = core.state._tray_app_instance
        if instance and instance.settings_window:
            js_code = f"onWifiScanUpdate({json.dumps(payload, ensure_ascii=False)})"
            instance.settings_window.evaluate_js(js_code)
    except Exception as e:
        logger.debug(f"push wifi scan event failed: {e}")


def _wifi_scan_loop(stop_event):
    """持续扫描主循环：每轮触发一次真实扫描并推送合并后的去重列表。"""
    seen = {}

    def _current():
        return list(seen.keys())

    # 先把系统缓存中已有的网络立刻推给前端，用户不用等第一轮扫描
    try:
        for ssid in scan_wifi_networks(force_scan=False) or []:
            seen.setdefault(ssid, True)
    except Exception as exc:
        logger.warning(f'wifi scan bootstrap failed: {exc}')
    _push_wifi_scan_event({'status': 'scanning', 'networks': _current(),
                           'new': _current(), 'count': len(seen)})

    deadline = time.monotonic() + WIFI_SCAN_MAX_SECONDS
    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            found = scan_wifi_networks(force_scan=True) or []
        except Exception as exc:
            logger.warning(f'wifi scan round failed: {exc}')
            found = []
        added = []
        for ssid in found:
            if ssid and ssid not in seen:
                seen[ssid] = True
                added.append(ssid)
        if stop_event.is_set():
            break
        if found or added:
            # 只在有新发现时推送；没有新网络时静默进入下一轮，避免事件轰炸
            _push_wifi_scan_event({'status': 'scanning', 'networks': _current(),
                                   'new': added, 'count': len(seen)})
        stop_event.wait(WIFI_SCAN_ROUND_PAUSE)
    reason = 'stopped' if stop_event.is_set() else 'timeout'
    _push_wifi_scan_event({'status': 'done', 'networks': _current(), 'new': [],
                           'count': len(seen), 'reason': reason})
    with _WIFI_SCAN_LOCK:
        _WIFI_SCAN['thread'] = None
        _WIFI_SCAN['stop'] = None


class ApiBridge:
    # 应用级单例：一次只进行一个下载任务
    _update_downloader = UpdateDownloader()
    _update_state = {'available': False, 'checked': False}
    _update_lock = threading.Lock()

    def load_config(self):
        return CONFIG_STORE.snapshot(include_revision=True)

    def get_dns_settings(self):
        from core.dns_settings import DNS_PRESETS, dns_settings
        return {**dns_settings(CONFIG_STORE.snapshot()), 'presets': DNS_PRESETS}

    def save_dns_settings(self, settings):
        from core.dns_settings import DNS_PRESETS, validate_servers
        try:
            if not isinstance(settings, dict):
                raise ValueError('DNS 设置格式无效')
            preset = settings.get('preset', 'custom')
            if preset not in {p['id'] for p in DNS_PRESETS} | {'custom'}:
                raise ValueError('未知的 DNS 预选项')
            saved = CONFIG_STORE.patch({
                'dns_preset': preset,
                'dns_servers': validate_servers(settings.get('servers')),
                'dns_ipv6_servers': validate_servers(settings.get('ipv6_servers'), ipv6_only=True),
            })
            return {'success': True, 'message': 'DNS 设置已保存',
                    'revision': saved.get('_revision')}
        except (TypeError, ValueError, OSError) as exc:
            return {'success': False, 'message': f'DNS 设置保存失败：{exc}'}

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
        # dark：安装器窗体与应用主题保持一致（托盘主题状态由前端 save_ui_prefs 同步）
        outcome = _install_update(result['file'], install_dir,
                                  restart=True, version=pending_version,
                                  dark=_TRAY_THEME.get('dark', True))
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
        """最小化主窗口：直接调用 Win32 ShowWindow 走系统原生路径（含动画）。"""
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
            if hwnd:
                SW_MINIMIZE = 6
                ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
                logger.info("Window minimized via native ShowWindow")
        except Exception as e:
            logger.error(f"minimize_window failed: {e}")

    def maximize_window(self):
        """切换主窗口最大化/还原（标题栏最大化按钮），返回切换后的状态。"""
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
            if not hwnd:
                return {'success': False, 'message': '未找到主窗口'}
            WM_SYSCOMMAND = 0x0112
            SC_MAXIMIZE = 0xF030
            SC_RESTORE = 0xF120
            command = SC_RESTORE if _is_window_zoomed() else SC_MAXIMIZE
            ctypes.windll.user32.SendMessageW(hwnd, WM_SYSCOMMAND, command, 0)
            time.sleep(0.15)
            return {'success': True, 'maximized': bool(_is_window_zoomed())}
        except Exception as e:
            logger.error(f"maximize_window failed: {e}")
            return {'success': False, 'message': str(e)}

    def get_window_state(self):
        """读取主窗口状态（最大化标记），供标题栏按钮回显。"""
        try:
            return {'success': True, 'maximized': bool(_is_window_zoomed())}
        except Exception as e:
            return {'success': False, 'maximized': False, 'message': str(e)}

    def close_window(self):
        try:
            if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                core.state._tray_app_instance.save_window_geometry()
                # 主窗口关闭（隐藏到托盘）时联动关闭"当前分流配置"悬浮窗
                core.state._tray_app_instance.close_config_viewer()
                core.state._tray_app_instance.settings_window.hide()
                # 隐藏到托盘：全局进入低功耗模式（状态探测/看门狗降频、前端暂停轮询）
                network_status.set_ui_visible(False)
                logger.info("Window hidden via title bar button")
        except Exception as e:
            logger.error(f"close_window failed: {e}")

    def open_traffic_config_window(self):
        """打开"当前分流配置"悬浮窗（分流规则 tab 前端按钮调用）。"""
        # Windows 10 的部分 WebView2 环境无法可靠创建第二个窗口。
        # 复用主窗口已工作的渲染器，避免用户点击后没有响应。
        if sys.platform == 'win32' and sys.getwindowsversion().build < 22000:
            return {'success': True, 'embedded': True}
        app = core.state._tray_app_instance
        if not app:
            return {'success': False, 'message': '应用实例不可用'}
        return app.open_config_viewer()

    def close_traffic_config_window(self):
        """关闭"当前分流配置"悬浮窗（悬浮窗自身的关闭按钮调用）。"""
        app = core.state._tray_app_instance
        if app:
            app.close_config_viewer()
        return {'success': True}

    def open_workflow_runner(self, workflow_id):
        """打开「测试工作流」悬浮窗并开始测试（工作流 tab 按钮调用）。"""
        app = core.state._tray_app_instance
        if not app:
            return {'success': False, 'message': '应用实例不可用'}
        return app.open_workflow_runner(workflow_id)

    def scan_wifi(self):
        return scan_wifi_networks()

    def start_wifi_scan(self):
        """启动持续扫描会话：结果通过 onWifiScanUpdate 事件增量推送前端。"""
        with _WIFI_SCAN_LOCK:
            thread = _WIFI_SCAN.get('thread')
            if thread and thread.is_alive():
                return {'success': True, 'already_running': True}
            stop_event = threading.Event()
            _WIFI_SCAN['stop'] = stop_event
            thread = threading.Thread(target=_wifi_scan_loop, args=(stop_event,),
                                      name='wifi-scan', daemon=True)
            _WIFI_SCAN['thread'] = thread
            thread.start()
        logger.info('持续 WiFi 扫描已启动')
        return {'success': True, 'message': '持续扫描已启动'}

    def stop_wifi_scan(self):
        with _WIFI_SCAN_LOCK:
            stop_event = _WIFI_SCAN.get('stop')
            if stop_event:
                stop_event.set()
        return {'success': True, 'message': '已停止扫描'}

    def _sync_monitor_state(self, old_config, new_config):
        old_needed = bool(old_config.get('auto_auth') or old_config.get('auto_restore'))
        needed = bool(new_config.get('auto_auth') or new_config.get('auto_restore'))
        task_changed = any(old_config.get(key) != new_config.get(key) for key in
                           ('wifi_name', 'auto_auth', 'auto_restore'))
        if needed:
            start_wifi_event_monitor()
            if task_changed and not new_config.get('wifi_name'):
                # 纯有线场景：开机自动认证无需 WiFi，事件任务也不需要
                unregister_wifi_event_task()
            elif task_changed and not register_wifi_event_task():
                logger.warning('WiFi event task could not be registered; in-process monitor remains active')
        elif old_needed:
            cleanup_wifi_event()
            unregister_wifi_event_task()
        # WARP 自动重连看门狗：随开关启停
        if new_config.get('warp_auto_reconnect'):
            start_watchdog()
        else:
            stop_watchdog()
        # 静默启动只影响开机自启（任务命令行是否带 --silent），改动后需重建任务
        if new_config.get('auto_startup') and \
                old_config.get('silent_startup') != new_config.get('silent_startup') \
                and is_admin() and setup_startup_task():
            logger.info('startup task re-registered for silent_startup change')
    def save_config(self, config):
        changes = dict(config or {})
        expected_revision = changes.pop('_revision', None)
        # 开机自动认证不再强制要求 WiFi 名称：有线环境直接认证，
        # 无线环境才会用到所配置的 WiFi（未配置时开机认证会跳过并提示）
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
                    logger.info(f"test_auth: running bound workflow {_wf_label(auth_wf_id)}")
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
            'auth_button_workflow', 'restore_button_workflow', 'exit_hook_workflow',
            'auto_check_update', 'auto_tune_workflow',
            'warp_auto_reconnect', 'warp_reconnect_delay', 'reconnect_workflow',
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

    def apply_workflow_tuning(self, workflow_id=None):
        """一键应用：把当前已有的调优建议立即写回该工作流配置，返回变更明细。"""
        config = CONFIG_STORE.snapshot()
        workflow_id = workflow_id or config.get('active_workflow_id', 'default_auth')
        try:
            changes = apply_auto_tune(workflow_id)
        except Exception as exc:
            logger.exception('apply_workflow_tuning failed')
            return {'success': False, 'message': str(exc)}
        return {'success': True, 'workflow_id': workflow_id,
                'changed': bool(changes), 'changes': changes,
                'revision': CONFIG_STORE.snapshot().get('_revision')}

    def reset_workflow_tuning(self, scope='workflow', workflow_id=None, step_ids=None):
        """重置调优记录（workflow_tuning.json 里的运行统计），分三级。

        scope='all'       重置所有工作流的全部调优记录；
        scope='workflow'  重置单个工作流的调优记录；
        scope='step'      重置指定节点（step_ids 列表）的调优记录。

        只清统计样本：建议停止产生、稳定度归零；已写回工作流配置的
        超时/重试参数不动（可用「恢复内置」或手动改回）。
        """
        try:
            if scope == 'all':
                wf_count, step_count = TUNING_STORE.clear_all()
                logger.info('[tuning] 已重置全部调优记录：%s 工作流 / %s 节点',
                            wf_count, step_count)
                return {'success': True, 'scope': 'all',
                        'cleared_workflows': wf_count, 'cleared_steps': step_count,
                        'message': f'已重置 {wf_count} 个工作流共 {step_count} 条节点调优记录'}
            workflow_id = workflow_id or CONFIG_STORE.get('active_workflow_id',
                                                           'default_auth')
            if scope == 'step':
                ids = [str(s).strip() for s in (step_ids or []) if str(s).strip()]
                if not ids:
                    return {'success': False, 'message': '未指定要重置的节点'}
                cleared = [sid for sid in ids
                           if TUNING_STORE.clear_step(workflow_id, sid)]
                return {'success': True, 'scope': 'step', 'workflow_id': workflow_id,
                        'steps': cleared, 'cleared_steps': len(cleared),
                        'message': f'已重置 {len(cleared)} 个节点的调优记录'}
            count = TUNING_STORE.clear_workflow(workflow_id)
            logger.info('[tuning] 已重置工作流 %s 的调优记录（%s 节点）', _wf_label(workflow_id), count)
            return {'success': True, 'scope': 'workflow', 'workflow_id': workflow_id,
                    'cleared_steps': count,
                    'message': f'已重置该工作流 {count} 个节点的调优记录'}
        except Exception as exc:
            logger.exception('reset_workflow_tuning failed')
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

    def update_workflow_meta(self, workflow_id, name=None, tray_menu=None, shared=None):
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
        if shared is not None:
            target['shared'] = bool(shared)
        saved = CONFIG_STORE.patch({'workflows': workflows})
        return {'success': True, 'workflows': self._workflow_snapshot(saved),
                'workflow': saved['workflows'][workflow_id],
                'revision': saved['_revision']}

    # ===== 工作流分享（导出 JSON / 导入） =====
    # 分享只带当前各节点配置（steps，含参数/超时/重试），不带调优统计
    # 记录（workflow_tuning.json 的运行数据按 workflow_id 本地存储，不随分享走）。
    WORKFLOW_SHARE_TYPE = 'campusauth_workflow'
    WORKFLOW_SHARE_VERSION = 1
    # 分享文件允许携带的字段：多余字段（如本机状态）一律剥掉
    _SHARE_STEP_KEYS = ('id', 'enabled', 'retries', 'timeout', 'retry_delay',
                        'continue_on_error', 'params')

    def _workflow_share_payload(self, workflow_id=None, scope='current'):
        """构造分享 JSON 文本，返回 (json文本, 名称提示, 错误消息)。

        scope='current' 只分享指定工作流；scope='all' 分享全部工作流
        （内置 + 自定义，按托盘列表同序）。两种都只带节点配置，
        不带调优统计记录（workflow_tuning.json 的运行数据不随分享走）。
        """
        if scope == 'all':
            definitions = self._workflow_snapshot(CONFIG_STORE.snapshot())
            if not definitions:
                return None, None, '当前没有任何工作流可分享'
            payload = {
                'type': self.WORKFLOW_SHARE_TYPE,
                'version': self.WORKFLOW_SHARE_VERSION,
                'scope': 'all',
                'exported_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'workflows': [
                    {
                        'name': str(item.get('name') or item.get('id') or '').strip(),
                        'description': str(item.get('description') or '')[:200],
                        'steps': copy.deepcopy(item.get('steps') or []),
                    }
                    for item in definitions
                ],
            }
            count = len(payload['workflows'])
            return json.dumps(payload, ensure_ascii=False, indent=2), f'{count} 个工作流', None
        definition = (CONFIG_STORE.get('workflows') or {}).get(workflow_id)
        if not definition:
            return None, None, '工作流不存在'
        name = str(definition.get('name') or workflow_id).strip() or workflow_id
        payload = {
            'type': self.WORKFLOW_SHARE_TYPE,
            'version': self.WORKFLOW_SHARE_VERSION,
            'scope': 'current',
            'exported_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'workflow': {
                'name': name,
                'description': str(definition.get('description') or '')[:200],
                'steps': copy.deepcopy(definition.get('steps') or []),
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), name, None

    def _run_file_dialog(self, ps_script, timeout=180):
        """运行 WinForms 文件对话框脚本，返回所选路径（取消返回空串）。

        脚本以 utf-8-sig（带 BOM）写入：Windows PowerShell 5.1 对无 BOM
        的 .ps1 按 ANSI 解码，中文标题/过滤串会变乱码甚至破坏语法，
        导致对话框根本弹不出来（用户实测「从文件导入没有选择器」）。
        """
        try:
            tmp_ps = os.path.join(tempfile.gettempdir(),
                                  f'cauth_share_dlg_{os.getpid()}.ps1')
            with open(tmp_ps, 'w', encoding='utf-8-sig') as f:
                f.write(ps_script)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            result = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass', '-STA', '-File', tmp_ps],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                timeout=timeout, startupinfo=si,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                os.remove(tmp_ps)
            except Exception:
                pass
            return result.stdout.strip() if result.returncode == 0 else ''
        except subprocess.TimeoutExpired:
            return ''
        except Exception as e:
            logger.error(f"file dialog failed: {e}")
            return ''

    def _save_file_dialog(self, title, default_name):
        escaped_title = str(title).replace("'", "''")
        escaped_name = str(default_name).replace("'", "''").replace('"', '')
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; "
            "$owner.Size = New-Object System.Drawing.Size(1, 1); "
            "$owner.StartPosition = 'Manual'; "
            "$owner.Location = New-Object System.Drawing.Point(-2000, -2000); "
            "$d = New-Object System.Windows.Forms.SaveFileDialog; "
            f"$d.Title = '{escaped_title}'; "
            f"$d.FileName = '{escaped_name}'; "
            "$d.Filter = '工作流分享文件 (*.json)|*.json|所有文件 (*.*)|*.*'; "
            "$d.FilterIndex = 1; "
            "$r = $d.ShowDialog($owner); $owner.Dispose(); "
            "if ($r -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ Write-Output $d.FileName } else { Write-Output '' }"
        )
        return self._run_file_dialog(ps_script)

    def _open_file_dialog(self, title):
        escaped_title = str(title).replace("'", "''")
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; "
            "$owner.Size = New-Object System.Drawing.Size(1, 1); "
            "$owner.StartPosition = 'Manual'; "
            "$owner.Location = New-Object System.Drawing.Point(-2000, -2000); "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$d.Title = '{escaped_title}'; "
            "$d.Filter = '工作流分享文件 (*.json)|*.json|所有文件 (*.*)|*.*'; "
            "$d.FilterIndex = 1; $d.CheckFileExists = $true; "
            "$r = $d.ShowDialog($owner); $owner.Dispose(); "
            "if ($r -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ Write-Output $d.FileName } else { Write-Output '' }"
        )
        return self._run_file_dialog(ps_script)

    def set_clipboard_text(self, text):
        """把纯文本写入系统剪贴板（分享 JSON 复制用）。"""
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               f'cauth_clip_{os.getpid()}.txt')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(str(text))
            # 经临时文件传递，避免引号/换行转义问题
            ps = ("Get-Content -Raw -Encoding UTF8 "
                  f"'{tmp.replace(chr(39), chr(39) * 2)}' | Set-Clipboard")
            code, _, err = run_command(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-Command', ps], shell=False, timeout=10)
            try:
                os.remove(tmp)
            except Exception:
                pass
            if code != 0:
                logger.warning(f"set_clipboard_text failed: {err[:160]}")
            return code == 0
        except Exception as e:
            logger.error(f"set_clipboard_text failed: {e}")
            return False

    def get_clipboard_text(self):
        """读取系统剪贴板纯文本（分享 JSON 导入用），失败返回空串。"""
        code, output, err = run_command(
            ['powershell', '-NoProfile', '-Command', 'Get-Clipboard -Raw'],
            shell=False, timeout=10)
        if code != 0:
            logger.warning(f"get_clipboard_text failed: {err[:160]}")
            return ''
        return output

    def export_workflow_shared(self, workflow_id=None, mode='clipboard', scope='current'):
        """导出工作流分享 JSON。

        mode='clipboard' 复制到剪贴板；'file' 另存为文件。
        scope='current' 只分享指定工作流；'all' 分享全部工作流。
        """
        text, name, error = self._workflow_share_payload(workflow_id, scope=scope)
        if error:
            return {'success': False, 'message': error}
        if mode == 'file':
            if scope == 'all':
                safe_name = 'CampusAuth_全部工作流分享'
            else:
                safe_name = ''.join(c for c in (name or '') if c not in '\\/:*?"<>|').strip() or 'workflow'
            path = self._save_file_dialog('导出工作流分享文件', f'{safe_name}.json')
            if not path:
                return {'success': False, 'cancelled': True, 'message': '已取消导出'}
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
            except OSError as exc:
                return {'success': False, 'message': f'写入文件失败：{exc}'}
            logger.info('工作流分享（scope=%s）已导出到 %s', scope, path)
            return {'success': True, 'mode': 'file', 'path': path,
                    'message': f'已导出到 {path}'}
        if not self.set_clipboard_text(text):
            return {'success': False, 'message': '复制到剪贴板失败，可改用「导出到文件」'}
        return {'success': True, 'mode': 'clipboard',
                'message': '分享 JSON 已复制到剪贴板'}

    @staticmethod
    def _sanitize_shared_steps(raw_steps):
        """只保留节点配置白名单字段，过滤分享方本地的未知/敏感数据。"""
        if not isinstance(raw_steps, list):
            return []
        steps = []
        for item in raw_steps:
            if not isinstance(item, dict):
                continue
            clean = {key: copy.deepcopy(item.get(key))
                     for key in ApiBridge._SHARE_STEP_KEYS if key in item}
            if clean.get('id'):
                steps.append(clean)
        return steps

    def import_workflow_shared(self, text):
        """从分享 JSON 导入为新的自定义工作流（剪贴板与文件导入共用）。

        支持两种格式：单工作流 {workflow: {...}} 与
        「分享所有工作流」导出的 {workflows: [{...}, ...]}。
        """
        raw = str(text or '').strip()
        if not raw:
            return {'success': False, 'message': '内容为空，未找到可导入的工作流'}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {'success': False,
                    'message': f'不是有效的工作流 JSON：{exc}'}
        entries = []
        if isinstance(payload, dict) and payload.get('type') == self.WORKFLOW_SHARE_TYPE:
            if isinstance(payload.get('workflows'), list):
                entries = payload['workflows']
            elif isinstance(payload.get('workflow'), dict):
                entries = [payload['workflow']]
        elif isinstance(payload, dict):
            # 宽松兼容直接导出的 {name, steps} 结构
            if isinstance(payload.get('steps'), list):
                entries = [payload]
            elif isinstance(payload.get('workflows'), list):
                entries = payload['workflows']
            elif isinstance(payload.get('workflow'), dict):
                entries = [payload['workflow']]
        if not entries:
            return {'success': False,
                    'message': '未识别到工作流数据（需要 CampusAuth 分享格式）'}
        config = CONFIG_STORE.snapshot()
        workflows = copy.deepcopy(config.get('workflows') or {})
        from core.config import workflow_id_from_name
        imported_names, skipped = [], []
        imported_id = None
        for shared in entries:
            if not isinstance(shared, dict):
                continue
            name = str(shared.get('name') or '').strip()[:60] or '导入的工作流'
            steps = self._sanitize_shared_steps(shared.get('steps'))
            if not steps:
                skipped.append(f'{name}（无有效节点）')
                continue
            try:
                steps = self._validate_workflow_steps(steps)
            except Exception as exc:
                skipped.append(f'{name}（{exc}）')
                continue
            base_id = workflow_id_from_name(name)
            workflow_id = base_id
            suffix = 2
            while workflow_id in workflows:
                workflow_id = f'{base_id}_{suffix}'
                suffix += 1
            workflows[workflow_id] = {
                'id': workflow_id,
                'name': name,
                'description': str(shared.get('description') or '从分享导入的工作流')[:200],
                'built_in': False,
                'tray_menu': True,
                'steps': steps,
            }
            imported_names.append(name)
            imported_id = workflow_id
        if not imported_names:
            detail = '；'.join(skipped[:3]) if skipped else '分享内容为空'
            return {'success': False, 'message': f'导入失败：{detail}'}
        saved = CONFIG_STORE.patch({'workflows': workflows})
        message = f'已导入工作流：{"、".join(imported_names)}'
        if skipped:
            message += f'（跳过 {"、".join(skipped)}）'
        logger.info('已从分享导入工作流：%s', imported_names)
        result = {'success': True, 'message': message,
                  'workflow_names': imported_names,
                  'workflows': self._workflow_snapshot(saved),
                  'revision': saved['_revision']}
        # 单个导入时把该工作流带回给前端直接选中
        if len(imported_names) == 1 and imported_id:
            result['workflow_id'] = imported_id
            result['workflow'] = saved['workflows'][imported_id]
            result['workflow_name'] = imported_names[0]
        return result

    def import_workflow_shared_clipboard(self):
        text = self.get_clipboard_text()
        return self.import_workflow_shared(text)

    def import_workflow_shared_file(self):
        path = self._open_file_dialog('选择工作流分享文件')
        if not path:
            return {'success': False, 'cancelled': True, 'message': '已取消导入'}
        try:
            # utf-8-sig 兼容带 BOM 的导出文件
            text = Path(path).read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError) as exc:
            return {'success': False, 'message': f'读取文件失败：{exc}'}
        result = self.import_workflow_shared(text)
        if result.get('success'):
            result['path'] = path
        return result

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

    # ===== 节点全局配置（全局/独立双模式） =====
    _NODE_GLOBAL_KEYS = ('retries', 'timeout', 'retry_delay', 'continue_on_error', 'params')

    @staticmethod
    def _sanitize_node_global_config(config):
        clean = {key: copy.deepcopy(config.get(key))
                 for key in ApiBridge._NODE_GLOBAL_KEYS
                 if key in (config or {})}
        if 'timeout' in clean:
            clean['timeout'] = max(1.0, min(180.0, float(clean['timeout'])))
        if 'retries' in clean:
            clean['retries'] = max(0, min(5, int(clean['retries'])))
        if 'retry_delay' in clean:
            clean['retry_delay'] = max(0.0, min(30.0, float(clean['retry_delay'])))
        if 'continue_on_error' in clean:
            clean['continue_on_error'] = bool(clean['continue_on_error'])
        if 'params' in clean and not isinstance(clean['params'], dict):
            clean.pop('params', None)
        return clean

    def get_node_globals(self):
        """全部节点类型全局配置：{step_id: {'config','source'}}。"""
        return {'success': True, 'node_globals': CONFIG_STORE.get('node_globals') or {}}

    def set_node_global(self, step_id, config, workflow_id=None):
        """设置/覆盖某节点类型的全局配置（覆盖确认由前端完成）。

        source 记录该全局配置当前来自哪个工作流的节点，用于 UI 提示；
        新节点被设为全局时即「接任」全局配置来源。
        """
        step_id = str(step_id or '').strip()
        if not step_id:
            return {'success': False, 'message': '缺少节点类型'}
        clean = self._sanitize_node_global_config(config)
        source = {}
        if workflow_id:
            definition = (CONFIG_STORE.get('workflows') or {}).get(workflow_id)
            source = {'workflow_id': str(workflow_id),
                      'workflow_name': str(definition.get('name') or workflow_id)}
        node_globals = copy.deepcopy(CONFIG_STORE.get('node_globals') or {})
        node_globals[step_id] = {'config': clean, 'source': source}
        saved = CONFIG_STORE.patch({'node_globals': node_globals})
        logger.info('[node-global] 已设置 %s 的全局配置（来源 %s）', step_id, source)
        return {'success': True, 'node_globals': node_globals,
                'revision': saved.get('_revision')}

    def clear_node_global(self, step_id):
        """移除某节点类型的全局配置（其来源节点切换为独立模式时调用）。"""
        step_id = str(step_id or '').strip()
        node_globals = copy.deepcopy(CONFIG_STORE.get('node_globals') or {})
        if step_id not in node_globals:
            return {'success': True, 'node_globals': node_globals}
        del node_globals[step_id]
        saved = CONFIG_STORE.patch({'node_globals': node_globals})
        logger.info('[node-global] 已移除 %s 的全局配置', step_id)
        return {'success': True, 'node_globals': node_globals,
                'revision': saved.get('_revision')}

    # ===== 托盘工作流顺序（设置页弹窗调整，原生托盘菜单按序号展示） =====
    def get_tray_menu_data(self):
        """托盘菜单数据：按托盘顺序的工作流 + 开机自启/管理员状态。"""
        config = CONFIG_STORE.snapshot()
        workflows = config.get('workflows') or {}
        items = []
        for position, workflow_id in enumerate(_tray_workflow_ids(config), start=1):
            workflow = workflows.get(workflow_id) or {}
            items.append({'id': workflow_id,
                          'name': str(workflow.get('name') or workflow_id),
                          'tray_order': position})
        return {
            'workflows': items,
            'startup_enabled': bool(config.get('auto_startup', False)),
            'is_admin': bool(is_admin()),
        }

    def set_tray_workflow_order(self, order_ids):
        """提交托盘工作流的完整顺序（设置页弹窗拖动排序后调用）。"""
        config = CONFIG_STORE.snapshot()
        tray_ids = _tray_workflow_ids(config)
        requested = [str(x) for x in (order_ids or [])]
        if sorted(requested) != sorted(tray_ids):
            return {'success': False,
                    'message': '排序列表与托盘工作流不一致，已忽略本次调整'}
        workflows = copy.deepcopy(config.get('workflows') or {})
        for order, wid in enumerate(requested, start=1):
            workflows[wid]['tray_order'] = order
        saved = CONFIG_STORE.patch({'workflows': workflows})
        logger.info('[tray] 工作流托盘顺序已调整：%s', requested)
        return {'success': True, 'message': '托盘顺序已调整',
                'tray_order': requested,
                'menu_data': self.get_tray_menu_data(),
                'workflows': self._workflow_snapshot(saved),
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
        name = str(workflow.get('name') or workflow_id)
        # 最新请求优先：打断在途的认证/恢复/工作流并接管锁
        acquired, interrupted = preempt_auth_lock(f'运行工作流「{name}」')
        if not acquired:
            return {'success': False, 'message': '当前操作无法中断，请稍后重试'}
        if interrupted:
            logger.info('run_workflow: 已中断「%s」，开始执行 %s', interrupted, _wf_label(workflow_id))
        # 分配新操作纪元，保证进度事件归属于本次启动的工作流
        app_state.start_operation('auth')

        def _do_run():
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
        msg = f'工作流 {name} 已启动'
        if interrupted:
            msg = f'已中断「{interrupted}」；{msg}'
        return {'success': True, 'message': msg}
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
                    logger.info(f"restore_network: running bound workflow {_wf_label(restore_wf_id)}")
                    success, msg = run_workflow_by_id(restore_wf_id)
                else:
                    success, msg = run_restore_task()
                app_state.update_operation(kind='restore', status='success' if success else 'error', message=msg)
                network_status.invalidate()
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
                network_status.invalidate()
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
            # utf-8-sig：PowerShell 5.1 无 BOM 时按 ANSI 解码，中文标题会乱码
            with open(tmp_ps, 'w', encoding='utf-8-sig') as f:
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
            # utf-8-sig：PowerShell 5.1 无 BOM 时按 ANSI 解码，中文标题会乱码
            with open(tmp_ps, 'w', encoding='utf-8-sig') as f:
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

    def sync_from_warp(self, kind=None):
        """从 WARP 合并规则到本地配置。

        kind: None 全部；'domain' 仅域名排除；'ip' 仅 IP/CIDR；'dns' 仅 DNS fallback。
        """
        ok, msg, details = self._get_mgr().sync_from_warp(kind)
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

    def apply_ip_ranges_to_warp(self):
        """将配置的 IP/CIDR 排除规则同步到 WARP（"IP 排除"子tab）"""
        ok, msg, details = self._get_mgr().apply_ip_ranges_to_warp()
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
            allowed = {'page_size', 'traffic_subview', 'network_detail_collapsed',
                       'active_tab', 'theme', 'theme_dark'}
            clean = {key: value for key, value in (prefs or {}).items() if key in allowed}
            if clean.get('theme') not in ('light', 'dark', 'system'):
                clean.pop('theme', None)
            if 'theme_dark' in clean and not isinstance(clean['theme_dark'], bool):
                clean.pop('theme_dark', None)
            current = CONFIG_STORE.get('ui_prefs') or {}
            current.update(clean)
            saved = CONFIG_STORE.patch({'ui_prefs': current})
            # 主题切换 → 托盘图标配色跟随（深色徽章 / 浅色徽章）
            if 'theme_dark' in clean:
                apply_tray_theme(clean['theme_dark'])
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
                   'warp_connected': bool, 'link_type': 'wireless'|'wired',
                   'wired_interface': str}
        """
        result = {
            'ipv4': '', 'ipv6': '', 'ipv6_status': 'none',
            'mac': '', 'wifi_ssid': '', 'interface': '',
            'warp_connected': False, 'link_type': '', 'wired_interface': ''
        }
        try:
            # 链路类型与对应网卡：无线/有线统一判定，主页据此分别展示
            try:
                link_type, active_iface = resolve_active_interface()
                result['link_type'] = link_type
                if link_type == 'wired':
                    result['interface'] = active_iface
                    result['wired_interface'] = active_iface
            except Exception as e:
                logger.warning(f"[get_network_detail] resolve_active_interface failed: {e}")

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

            # WiFi SSID（有线联网时保持为空，主页按链路类型展示）
            try:
                result['wifi_ssid'] = get_current_wifi_ssid() or ''
            except Exception as e:
                logger.warning(f"[get_network_detail] get_current_wifi_ssid failed: {e}")

            # 网络接口名：有线时上面已填，无线/未知时按原逻辑取 WLAN
            if not result['interface']:
                try:
                    result['interface'] = get_wifi_interface_name() or ''
                except Exception as e:
                    logger.warning(f"[get_network_detail] get_wifi_interface_name failed: {e}")

            # WARP 连接状态（复用 check_network_status 逻辑）
            try:
                status = self.check_network_status()
                # status 为 'connected' 或 'partial' 时认为 WARP 已连接
                result['warp_connected'] = status.get('status') in ('connected', 'partial')
                # 免流判定透传：前端详情面板区分「IPv6 底层免流」与「IPv4 底层计费」
                result['warp_free'] = bool(status.get('warp_free'))
                result['warp_underlay'] = status.get('warp_underlay') or ''
                # IPv4 绑定状态透传：状态条区分「已禁用」与「未取到地址」
                result['ipv4_disabled'] = bool(status.get('ipv4_disabled'))
            except Exception as e:
                logger.warning(f"[get_network_detail] check_network_status failed: {e}")

            logger.info(f"[get_network_detail] Returning: ipv4={result['ipv4']}, ipv6_status={result['ipv6_status']}, "
                        f"warp={result['warp_connected']}, link={result['link_type']}")
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
    acquired, interrupted = preempt_auth_lock(f'执行工作流「{name}」')
    if not acquired:
        if icon:
            icon.notify('当前操作无法中断，请稍后重试', '校园网助手')
            icon.icon = create_icon('green')
            icon.title = '校园网助手'
        return
    if interrupted and icon:
        icon.notify(f'已中断「{interrupted}」，开始执行：{name}', '校园网助手')
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
        logger.exception("Workflow %s error", _wf_label(workflow_id))
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
    acquired, interrupted = preempt_auth_lock('托盘恢复网络')
    if not acquired:
        icon.notify('当前操作无法中断，请稍后重试', '校园网助手')
        return
    if interrupted:
        icon.notify(f'已中断「{interrupted}」，开始恢复网络', '校园网助手')
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

def _run_exit_hook():
    """托盘「退出」时、进程结束前同步执行用户绑定的工作流（默认未绑定即跳过）。

    典型用途：退出前断开 WARP / 注销校园网，把网络恢复干净。
    实现要点：
    - 必须同步等待完成：hook 跑完才允许 request_exit() 销毁窗口/停托盘，
      否则进程退出会直接中断在途的 hook，等于没执行。
    - 与在途认证/恢复互斥：先请求取消并等待 _auth_lock 释放，
      拿不到锁就跳过 hook（日志记录），不与在途操作并发改网络。
    - 有整体超时兜底：超时后放弃等待继续退出，由节点自身的超时保证
      大多数情况下 hook 能正常收尾。
    """
    try:
        wf_id = str(CONFIG_STORE.get('exit_hook_workflow') or '').strip()
    except Exception as e:
        logger.warning(f"exit hook: read config failed: {e}")
        return
    if not wf_id:
        return
    if not (CONFIG_STORE.get('workflows') or {}).get(wf_id):
        logger.warning(f"exit hook: bound workflow {_wf_label(wf_id)!r} not found, skipping")
        return

    logger.info(f"exit hook: requesting cancel of in-flight operation, then acquiring lock")
    _auth_cancelled.set()
    if not _auth_lock.acquire(timeout=EXIT_HOOK_LOCK_WAIT):
        logger.warning("exit hook: auth lock busy, skipping exit hook")
        _auth_cancelled.clear()
        return
    try:
        logger.info(f"exit hook: running workflow {_wf_label(wf_id)} before exit")
        result = {}

        def _run():
            try:
                result['out'] = run_workflow_by_id(wf_id)
            except Exception as exc:
                result['out'] = (False, str(exc))

        t = threading.Thread(target=_run, daemon=True, name='exit-hook')
        t.start()
        t.join(EXIT_HOOK_TIMEOUT)
        if t.is_alive():
            logger.warning(f"exit hook: workflow {wf_id} still running after "
                           f"{EXIT_HOOK_TIMEOUT}s, exiting anyway")
            return
        ok, msg = result.get('out', (False, 'no result'))
        logger.info(f"exit hook: workflow {_wf_label(wf_id)} finished: {'ok' if ok else 'FAILED'} - {msg}")
    finally:
        _auth_cancelled.clear()
        _auth_lock.release()

def on_exit(icon, item):
    logger.info("on_exit: user clicked Exit")
    _run_exit_hook()
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
        # 免流失效监测：WARP 断开/底层切到 IPv4 时气泡 + 前端 toast 提醒（计费风险）
        network_status.on_free_dropped = self._notify_free_loss
        # "当前分流配置"悬浮窗（独立子窗口，主窗口关闭时联动销毁）
        self._config_viewer_window = None
        self._html_url = None

    def _notify_free_loss(self, reason):
        """免流失效提醒：托盘气泡 + 前端 toast（流量正在走 IPv4 计费，需及时处理）。"""
        logger.warning('[free-loss] notify: %s', reason)
        try:
            if self.icon:
                self.icon.notify(reason, '校园网助手')
        except Exception as exc:
            logger.debug('free-loss tray notify failed: %s', exc)
        try:
            if self.settings_window:
                js_code = f'onToast({{message:{_js_escape(reason)}, kind:"error"}})'
                self.settings_window.evaluate_js(js_code)
        except Exception as exc:
            logger.debug('free-loss toast failed: %s', exc)

    # ------------------------------------------------------------------
    # "当前分流配置"悬浮窗（分流规则 tab 的实时状态展示）
    # ------------------------------------------------------------------
    def open_config_viewer(self):
        """打开（或聚焦已存在的）"当前分流配置"悬浮窗。

        独立 frameless 子窗口加载 dist/index.html#viewer，
        可拖出主窗口边界、可单独关闭；主窗口关闭时由 close_config_viewer 联动销毁。
        """
        try:
            win = self._config_viewer_window
            if win is not None:
                try:
                    win.show()
                    win.restore()
                    return {'success': True, 'message': '已打开'}
                except Exception:
                    # 旧窗口对象已失效（已被销毁），重建
                    self._config_viewer_window = None

            html_file = get_resource_path('frontend/dist/index.html')
            if not os.path.isfile(html_file):
                return {'success': False, 'message': '前端资源缺失，无法打开悬浮窗'}
            if not self._html_url:
                self._html_url = Path(html_file).resolve().as_uri()

            # 悬浮窗初始位置：主窗口左上角右下偏移，制造"浮在主窗口旁"的层次感
            x = y = None
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if hwnd:
                    rect = ctypes.wintypes.RECT()
                    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        x = rect.left + 80
                        y = rect.top + 80
            except Exception:
                pass

            kwargs = {}
            if x is not None and y is not None:
                kwargs['x'], kwargs['y'] = x, y
            win = webview.create_window(
                '当前分流配置 - CampusAuth',
                self._html_url + '#viewer',
                js_api=self.api,
                width=900, height=640,
                resizable=True,
                min_size=(600, 420),
                background_color='#0D0D0D',
                easy_drag=False,   # 与主窗口一致：由 pywebview-drag-region 精确控制拖动区域
                frameless=True,
                **kwargs,
            )
            win.events.closed += self._on_config_viewer_closed
            self._config_viewer_window = win
            logger.info('config viewer window opened')
            return {'success': True, 'message': '已打开'}
        except Exception as e:
            logger.error(f'open_config_viewer failed: {e}')
            return {'success': False, 'message': str(e)}

    def _on_config_viewer_closed(self, *args):
        self._config_viewer_window = None

    def close_config_viewer(self):
        """关闭"当前分流配置"悬浮窗（若存在）。"""
        win = self._config_viewer_window
        if win is None:
            return
        self._config_viewer_window = None
        try:
            win.destroy()
            logger.info('config viewer window closed')
        except Exception as e:
            logger.debug(f'close_config_viewer: {e}')

    # ------------------------------------------------------------------
    # 「测试工作流」：主窗口工作流 tab 的底部测试面板
    # （节点列表高亮执行进度 + 面板内详细日志，无独立窗口）
    # ------------------------------------------------------------------
    def open_workflow_runner(self, workflow_id):
        """测试指定工作流：抢占式启动，事件实时推送到主窗口测试面板。"""
        definition = (CONFIG_STORE.get('workflows') or {}).get(workflow_id)
        if not definition:
            return {'success': False, 'message': '工作流不存在'}
        name = str(definition.get('name') or workflow_id)
        # 最新请求优先：与主页/托盘按钮一致，打断在途操作
        acquired, interrupted = preempt_auth_lock(f'测试工作流「{name}」')
        if not acquired:
            return {'success': False, 'message': '当前操作无法中断，请稍后重试'}
        if interrupted:
            logger.info('runner: 已中断「%s」，开始测试 %s', interrupted, _wf_label(workflow_id))
        # 节点映射：runnerIndex（运行器序号，仅启用节点，与事件 step 序号一致）
        # → arrayIndex（编辑器节点数组下标，用于高亮定位）
        from core.auth_workflow import WORKFLOW_CATALOG
        steps = []
        for arr_idx, step in enumerate(definition.get('steps') or []):
            if not step.get('enabled', True):
                continue
            sid = str(step.get('id', ''))
            steps.append({'runnerIndex': len(steps) + 1, 'arrayIndex': arr_idx,
                          'id': sid,
                          'name': (WORKFLOW_CATALOG.get(sid) or {}).get('name', sid)})
        epoch = app_state.start_operation('auth')
        self._push_runner_js({'type': 'init', 'epoch': epoch,
                              'workflowId': str(workflow_id), 'workflowName': name,
                              'steps': steps, 'interrupted': interrupted,
                              'operationId': epoch})
        threading.Thread(target=self._run_runner_workflow,
                         args=(workflow_id, name, epoch), daemon=True).start()
        msg = f'已开始测试：{name}'
        if interrupted:
            msg = f'已中断「{interrupted}」；{msg}'
        return {'success': True, 'message': msg, 'epoch': epoch}

    def _push_runner_js(self, payload):
        """把测试面板事件推送到主窗口（SPA 常驻加载，直接 evaluate_js）。"""
        import json as _json
        instance = core.state._tray_app_instance
        win = instance.settings_window if instance else None
        if win is None:
            return
        try:
            win.evaluate_js('onRunnerEvent && onRunnerEvent('
                            + _json.dumps(payload, ensure_ascii=False) + ')')
        except Exception as e:
            logger.debug(f'_push_runner_js failed: {e}')

    def _run_runner_workflow(self, workflow_id, name, epoch):
        """测试线程：跑工作流并推送 done 事件（含每节点耗时/重试统计）。"""
        try:
            success, message = run_workflow_by_id(workflow_id)
        except Exception as exc:
            logger.exception('runner workflow %s error', _wf_label(workflow_id))
            success, message = False, f'执行异常：{exc}'
        result = getattr(core.state, 'last_workflow_result', None)
        step_stats, elapsed = {}, None
        if result is not None:
            try:
                step_stats = result.step_stats or {}
                elapsed = round(float(result.elapsed), 2)
            except Exception:
                step_stats, elapsed = {}, None
        self._push_runner_js({'type': 'done', 'epoch': epoch,
                              'workflowId': str(workflow_id), 'workflowName': name,
                              'success': bool(success), 'message': str(message),
                              'elapsed': elapsed, 'stepStats': step_stats,
                              'operationId': epoch})
        # 主窗口终态提示（与 run_workflow 的行为一致）
        try:
            if not _auth_cancelled.is_set() and core.state._tray_app_instance\
                    and core.state._tray_app_instance.settings_window:
                status = 'success' if success else 'error'
                js_code = f"onAuthProgress({{step:1,total:1,message:{_js_escape(message)},status:{_js_escape(status)}}})"
                core.state._tray_app_instance.settings_window.evaluate_js(js_code)
        except Exception:
            pass
        finally:
            _auth_cancelled.clear()
            _auth_lock.release()

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
        for win_attr in ('settings_window', '_config_viewer_window'):
            win = getattr(self, win_attr, None)
            if win:
                try:
                    win.destroy()
                except Exception as e:
                    logger.debug(f"request_exit: destroy {win_attr} failed: {e}")
                setattr(self, win_attr, None)
        cleanup_wifi_event()
        stop_watchdog()
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
                    # 免流成功 = 应用的核心目标状态 → 绿；未免流（WARP 走 IPv4）→ 橙警示
                    'connected': 'green', 'partial': 'orange', 'normal': 'green',
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
        """托盘菜单的工作流条目：按用户排序展示，并加序号便于快速定位。"""
        config = load_config()
        workflows = config.get('workflows') or {}
        items = []
        for position, workflow_id in enumerate(_tray_workflow_ids(config), start=1):
            workflow = workflows.get(workflow_id) or {}
            name = str(workflow.get('name') or workflow_id)
            items.append(pystray.MenuItem(
                f'{position}. {name}',
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
        # 启动时先按已保存的主题渲染托盘图标（前端就绪后也会推送一次 theme_dark）
        try:
            prefs = CONFIG_STORE.get('ui_prefs') or {}
            if isinstance(prefs.get('theme_dark'), bool):
                _TRAY_THEME['dark'] = prefs['theme_dark']
        except Exception:
            pass
        # 原生托盘菜单按应用主题渲染深色（前端就绪后 save_ui_prefs 会再同步）
        set_menu_dark_mode(_TRAY_THEME.get('dark', True))
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
                if lparam == win32.WM_RBUTTONUP:
                    # 显示前对弹出菜单窗口应用当前主题（深色/浅色）
                    theme_popup_menu_window(_TRAY_THEME.get('dark', True))
                original_on_notify(wparam, lparam)

            self.icon._message_handlers[WM_NOTIFY] = patched_on_notify

            # 包装 TrackPopupMenuEx：显示前一刻对弹出菜单窗口应用主题
            # （菜单窗口由系统按线程复用，首次创建后才能被 FindWindow 找到）
            original_tpm = win32.TrackPopupMenuEx

            def themed_track_popup_menu(hmenu, flags, x, y, hwnd_, prc):
                dark = _TRAY_THEME.get('dark', True)
                theme_popup_menu_window(dark)  # 确保创建钩子就绪并重刷已存在窗口
                destroy_menu_window()          # 销毁旧窗口：本次显示强制以当前主题重建
                return original_tpm(hmenu, flags, x, y, hwnd_, prc)

            win32.TrackPopupMenuEx = themed_track_popup_menu
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
                # 窗口回到用户视角：恢复正常轮询频率并立即刷新一次状态
                network_status.set_ui_visible(True)
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
                    if not cfg.get('wifi_name'):
                        logger.info('未配置 WiFi：跳过 WiFi 事件任务注册（开机自动认证按有线直接执行）')
                    elif register_wifi_event_task():
                        logger.info("WiFi event task registered on startup")
                    else:
                        logger.warning("Failed to register WiFi event task on startup")
                else:
                    logger.info("Not admin, skipping WiFi event task registration")
                # 开机自动认证只在开机自启（--silent）时执行；
                # 用户主动启动不代做网络动作，交给主页「开始认证」。
                if cfg.get('auto_auth'):
                    if should_run_boot_auth(cfg, silent=self._silent):
                        check_startup_wifi_and_auth()
                    else:
                        logger.info('手动启动：不执行开机自动认证')
                        _update_tray_status()
                else:
                    _update_tray_status()
            else:
                _update_tray_status()
            if cfg.get('warp_auto_reconnect'):
                start_watchdog()
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
            html_url = Path(html_file).resolve().as_uri()
            if html_file == dist_index:
                # 悬浮窗复用同一份 Vue 前端（#viewer hash 路由）
                self._html_url = html_url
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

        # 静默启动时窗口直接隐藏：立即进入托盘低功耗模式
        #（非静默启动由 ensure_visible / show_settings 恢复正常频率）
        network_status.set_ui_visible(not self._silent)

        def on_closing():
            logger.info("[on_closing] Window closing event triggered")
            try:
                self.save_window_geometry()
            except Exception as e:
                logger.error(f"[on_closing] save_window_geometry failed: {e}")
            # 主窗口关闭（无论隐藏到托盘还是真正退出）都联动关闭"当前分流配置"悬浮窗
            self.close_config_viewer()
            if self._should_exit:
                logger.info("[on_closing] Real exit requested, allowing close")
                return None
            logger.info("[on_closing] Hiding window to tray (not closing)")
            try:
                self.settings_window.hide()
                # 隐藏到托盘：状态服务进入低频模式，前端轮询由 document.hidden 暂停
                network_status.set_ui_visible(False)
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
        # 窗口显示后启用原生窗口行为：原生动画 / 系统贴靠 / 系统命令
        # （吞掉非客户区，白边不再出现；后台重试应对标题注册延迟）
        self.settings_window.events.shown += lambda *args:             enable_native_window_behaviors_with_retry('CampusAuth')

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
                        # 非静默启动：窗口可见，恢复正常轮询频率
                        network_status.set_ui_visible(True)
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
