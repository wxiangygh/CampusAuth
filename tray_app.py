import os
import sys
import json
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
        'window_y': None
    }
    logger.info(f"Loading config from: {CONFIG_FILE}")
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
                if 'portal_server' in cfg and 'portal_ip' not in cfg:
                    parts = cfg.pop('portal_server').rsplit(':', 1)
                    cfg['portal_ip'] = parts[0]
                    cfg['portal_port'] = parts[1] if len(parts) > 1 else ''
                    save_config_to_file(cfg)
                    logger.info(f"Migrated portal_server -> portal_ip={cfg['portal_ip']}, portal_port={cfg['portal_port']}")
                merged = {**defaults, **cfg}
                logger.info(f"Config loaded: wifi_name={merged.get('wifi_name')}, username={merged.get('username')}, auto_auth={merged.get('auto_auth')}")
                return merged
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    logger.info("Config file not found, using defaults")
    return defaults

def save_config_to_file(cfg):
    logger.info(f"Saving config to: {CONFIG_FILE}, wifi_name={cfg.get('wifi_name')}, username={cfg.get('username')}, auto_auth={cfg.get('auto_auth')}")
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info("Config saved successfully")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

CONFIG = load_config()

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def create_icon(color='orange'):
    size = (64, 64)
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        'gray': (180, 180, 180),
        'green': (52, 199, 89),
        'orange': (246, 131, 32),
        'red': (255, 59, 48)
    }
    c = colors.get(color, colors['orange'])
    draw.ellipse([4, 4, 60, 60], fill=c)
    cx, cy = 32, 28
    arcs = [(22, 10, 16), (16, 6, 10), (10, 3, 5)]
    for r, w, _ in arcs:
        draw.arc([cx - r, cy - r, cx + r, cy + r], 250, 290, fill=(255, 255, 255), width=w)
    draw.ellipse([cx - 3, cy + 10, cx + 3, cy + 16], fill=(255, 255, 255))
    return img

def ensure_app_icon():
    icon_path = SCRIPT_DIR / 'app.ico'
    if not icon_path.exists():
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
    def load_config(self):
        return load_config()

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
                core.state._tray_app_instance.settings_window.hide()
                logger.info("Window hidden via title bar button")
        except Exception as e:
            logger.error(f"close_window failed: {e}")

    def scan_wifi(self):
        return scan_wifi_networks()

    def save_config(self, config):
        global CONFIG
        logger.info(f"save_config called: wifi_name={config.get('wifi_name')}, username={config.get('username')}, auto_auth={config.get('auto_auth')}, auto_restore={config.get('auto_restore')}")
        cfg = load_config()
        old_auto_auth = cfg.get('auto_auth', False)
        old_auto_restore = cfg.get('auto_restore', False)
        cfg.update(config)
        save_config_to_file(cfg)
        CONFIG = cfg
        need_monitor = cfg.get('auto_auth') or cfg.get('auto_restore')
        old_need_monitor = old_auto_auth or old_auto_restore
        if need_monitor:
            if cfg.get('auto_auth') and not cfg.get('wifi_name'):
                return {'success': False, 'message': '请先选择或输入WiFi名称'}
            if not old_need_monitor:
                start_wifi_event_monitor()
            if register_wifi_event_task():
                return {'success': True, 'message': '设置已保存'}
            else:
                return {'success': False, 'message': '需要管理员权限才能启用WiFi事件监控'}
        else:
            if old_need_monitor:
                cleanup_wifi_event()
            unregister_wifi_event_task()
            return {'success': True, 'message': '设置已保存'}

    def cancel_operation(self):
        logger.info("cancel_operation called")
        if _auth_lock.locked():
            _auth_cancelled.set()
            logger.info("Cancel flag set, notifying frontend immediately")
            if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                js_code = f"onAuthProgress({{step:0, total:1, message:{_js_escape('已取消')}, status:{_js_escape('cancelled')}}})"
                core.state._tray_app_instance.settings_window.evaluate_js(js_code)
            return {'success': True, 'message': '已取消'}
        return {'success': True, 'message': '没有正在进行的操作'}

    def test_auth(self):
        def _do_auth():
            if not _auth_lock.acquire(blocking=False):
                js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape('认证正在进行中，请稍候')}, status:{_js_escape('error')}}})"
                if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
                    core.state._tray_app_instance.settings_window.evaluate_js(js_code)
                return
            try:
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
        cfg = load_config()
        old_auto_auth = cfg.get('auto_auth', False)
        old_auto_restore = cfg.get('auto_restore', False)
        for key in ('wifi_name', 'username', 'password', 'auto_auth', 'auto_restore', 'warp_cli_path', 'silent_startup', 'portal_ip', 'portal_port'):
            if key in form_data:
                cfg[key] = form_data[key]
        save_config_to_file(cfg)
        global CONFIG
        CONFIG = cfg
        logger.info(f"Auto-saved: wifi={form_data.get('wifi_name')}, user={form_data.get('username')}, auto_auth={form_data.get('auto_auth')}, auto_restore={form_data.get('auto_restore')}")
        new_auto_auth = cfg.get('auto_auth', False)
        new_auto_restore = cfg.get('auto_restore', False)
        need_monitor = new_auto_auth or new_auto_restore
        if need_monitor and not old_auto_auth and not old_auto_restore:
            start_wifi_event_monitor()
            if register_wifi_event_task():
                logger.info("WiFi event monitor started (auto_auth or auto_restore enabled)")
            else:
                logger.warning("Failed to register WiFi event task")
        elif not need_monitor and (old_auto_auth or old_auto_restore):
            cleanup_wifi_event()
            unregister_wifi_event_task()
            logger.info("WiFi event monitor stopped (both auto_auth and auto_restore disabled)")
        elif need_monitor and not core.state._wifi_monitor_started:
            start_wifi_event_monitor()
            if register_wifi_event_task():
                logger.info("WiFi event monitor restarted")

    def check_network_status(self):
        logger.info("check_network_status called")
        warp_connected = False
        ipv4_disabled = False
        warp_cli = get_warp_cli()
        if warp_cli:
            code, output, _ = run_command([warp_cli, 'status'], shell=False, timeout=10)
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                warp_connected = True
                logger.debug("check_network_status: WARP connected")
            elif code != 0:
                logger.warning(f"check_network_status: warp-cli status failed (code={code})")
        interface_name = get_wifi_interface_name()
        if not interface_name:
            interface_name = 'WLAN'
        ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
        code, output, err = run_command(['powershell', '-Command', ps_cmd], shell=False)
        logger.debug(f"check_network_status: IPv4 check code={code}, output={output!r}, err={err!r}")
        if 'False' in output:
            ipv4_disabled = True
            logger.debug(f"check_network_status: IPv4 disabled on {interface_name}")
        if warp_connected and ipv4_disabled:
            return {'status': 'connected', 'message': 'WARP已连接，IPv4已禁用'}
        elif warp_connected and not ipv4_disabled:
            return {'status': 'partial', 'message': 'WARP已连接，但IPv4未禁用'}
        elif not warp_connected and ipv4_disabled:
            warp_adapter = self._check_warp_adapter()
            if warp_adapter:
                logger.info("check_network_status: WARP adapter exists but warp-cli status failed, treating as connected")
                return {'status': 'connected', 'message': 'WARP已连接，IPv4已禁用'}
            return {'status': 'broken', 'message': 'IPv4已禁用但WARP未连接'}
        else:
            has_internet = _check_internet()
            if has_internet:
                return {'status': 'normal', 'message': '正常模式'}
            else:
                return {'status': 'disconnected', 'message': '未连接'}

    def _check_warp_adapter(self):
        try:
            ps_cmd = 'Get-NetAdapter -Name *WARP* | Where-Object { $_.Status -eq \"Up\" } | Select-Object -First 1 -ExpandProperty Name'
            code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False, timeout=5)
            if code == 0 and output.strip():
                logger.debug(f"_check_warp_adapter: found {output.strip()}")
                return True
        except Exception as e:
            logger.debug(f"_check_warp_adapter failed: {e}")
        return False

    def restore_network(self):
        logger.info("restore_network called")
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
                success, msg = run_restore_task()
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
        global CONFIG
        if enabled:
            if not is_admin():
                return {'success': False, 'message': '需要管理员权限'}
            if setup_startup_task():
                CONFIG['auto_startup'] = True
                save_config_to_file(CONFIG)
                if core.state._tray_app_instance:
                    core.state._tray_app_instance._refresh_tray_menu()
                return {'success': True, 'message': '开机自启已开启'}
            return {'success': False, 'message': '设置失败'}
        else:
            remove_startup_task()
            CONFIG['auto_startup'] = False
            save_config_to_file(CONFIG)
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

    def refresh_startup_task(self):
        logger.info("refresh_startup_task called")
        if check_startup_status():
            if is_admin():
                setup_startup_task()
                return {'success': True, 'message': '自启任务已更新'}
            return {'success': False, 'message': '需要管理员权限'}
        return {'success': True, 'message': '无需更新'}

    # ------------------------------------------------------------------
    # WARP 排除管理 API（供 warp_exclusion.html 前端调用）
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
        cfg = self._get_mgr().get_config()
        return cfg.get('auto_enable_ipv4', True)

    def set_auto_enable_ipv4(self, enabled):
        from warp_exclusion import load_exclusion_config, save_exclusion_config
        cfg = load_exclusion_config()
        cfg['auto_enable_ipv4'] = enabled
        save_exclusion_config(cfg)
        return {'success': True, 'message': '已更新'}

    def close_exclusion_window(self):
        """关闭 WARP 排除管理窗口（销毁 WebView2 进程，释放资源）"""
        if core.state._tray_app_instance and core.state._tray_app_instance._exclusion_window:
            try:
                core.state._tray_app_instance._exclusion_window.destroy()
            except Exception as e:
                logger.debug(f'close_exclusion_window destroy: {e}')
            core.state._tray_app_instance._exclusion_window = None

    # ------------------------------------------------------------------
    # 流量监控 API（供 traffic_monitor.html 前端调用）
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

    def close_traffic_window(self):
        """关闭流量监控窗口（销毁 WebView2 进程，释放资源）"""
        if core.state._tray_app_instance and core.state._tray_app_instance._traffic_window:
            try:
                core.state._tray_app_instance._traffic_window.destroy()
            except Exception as e:
                logger.debug(f'close_traffic_window destroy: {e}')
            core.state._tray_app_instance._traffic_window = None

    def close_flow_window(self):
        """关闭流量可视化窗口（销毁 WebView2 进程，释放资源）"""
        if core.state._tray_app_instance and core.state._tray_app_instance._flow_window:
            try:
                core.state._tray_app_instance._flow_window.destroy()
            except Exception as e:
                logger.debug(f'close_flow_window destroy: {e}')
            core.state._tray_app_instance._flow_window = None

icon_instance = None

def on_settings(icon, item):
    logger.info("User clicked: Settings")

def on_auth(icon, item):
    logger.info("User clicked: Manual Auth")
    icon.icon = create_icon('orange')
    icon.title = '正在认证...'
    icon.notify('正在执行校园网认证...', '校园网助手')
    threading.Thread(target=_run_auth, args=(icon,), daemon=True).start()

def _run_auth(icon):
    if not _auth_lock.acquire(blocking=False):
        icon.notify('认证正在进行中，请稍候', '校园网助手')
        icon.icon = create_icon('green')
        icon.title = '校园网助手'
        return
    try:
        success, msg = run_auth_task()
        if success:
            icon.icon = create_icon('orange')
            icon.title = 'WARP已连接'
            icon.notify(msg, '校园网助手')
        else:
            icon.icon = create_icon('red')
            icon.title = '认证失败'
            icon.notify(f'失败: {msg}', '校园网助手')
    except Exception as e:
        logger.error(f"Auth error: {e}")
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
    """注销并重新认证：先注销 Portal，再重新执行认证流程"""
    logger.info("User clicked: Re-auth (logout + auth)")
    icon.icon = create_icon('orange')
    icon.title = '正在重新认证...'
    icon.notify('正在注销并重新认证...', '校园网助手')
    threading.Thread(target=_run_reauth, args=(icon,), daemon=True).start()

def _run_reauth(icon):
    if not _auth_lock.acquire(blocking=False):
        icon.notify('操作正在进行中，请稍候', '校园网助手')
        icon.icon = create_icon('green')
        icon.title = '校园网助手'
        return
    try:
        # 1. 先注销 Portal
        logger.info("[reauth] Logging out from portal...")
        portal_logout()
        # 2. 重新认证
        logger.info("[reauth] Starting re-authentication...")
        success, msg = run_auth_task()
        if success:
            icon.icon = create_icon('orange')
            icon.title = 'WARP已连接'
            icon.notify(msg, '校园网助手')
        else:
            icon.icon = create_icon('red')
            icon.title = '认证失败'
            icon.notify(f'失败: {msg}', '校园网助手')
    except Exception as e:
        logger.error(f"Re-auth error: {e}")
        icon.icon = create_icon('red')
        icon.title = '错误'
        icon.notify(f'错误: {e}', '校园网助手')
    finally:
        _auth_lock.release()

def on_exit(icon, item):
    logger.info("on_exit: user clicked Exit")
    if core.state._tray_app_instance:
        core.state._tray_app_instance._should_exit = True
        if not core.state._tray_app_instance._webview_started:
            core.state._tray_app_instance._webview_start_event.set()
        # 销毁所有 webview 窗口，让 webview.start() 退出
        for win_attr in ('settings_window', '_exclusion_window', '_traffic_window', '_flow_window'):
            win = getattr(core.state._tray_app_instance, win_attr, None)
            if win:
                try:
                    win.destroy()
                except Exception as e:
                    logger.debug(f"on_exit: destroy {win_attr} failed: {e}")
                setattr(core.state._tray_app_instance, win_attr, None)
    cleanup_wifi_event()
    icon.stop()
    if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
        core.state._tray_app_instance.save_window_position()
    if core.state.TRAY_MUTEX:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(core.state.TRAY_MUTEX)
        core.state.TRAY_MUTEX = None
        logger.debug("on_exit: mutex released")
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
    WIN_W = 400
    WIN_H = 560

    def __init__(self, silent=False):
        self.icon = None
        self.api = ApiBridge()
        self.settings_window = None
        self._exclusion_window = None
        self._traffic_window = None
        self._flow_window = None
        self._should_exit = False
        self._silent = silent
        self._webview_started = False
        self._webview_start_event = threading.Event()
        self._init_done = False

    def create_tray(self):
        self.icon = pystray.Icon('wifi_auto_auth')
        self.icon.icon = create_icon('gray')
        self.icon.title = '校园网助手'
        startup_enabled = CONFIG.get('auto_startup', False)
        menu_items = [
            pystray.MenuItem('显示主窗口', lambda i, item: self.show_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('手动认证', on_auth),
            pystray.MenuItem('注销并重新认证', on_reauth),
            pystray.MenuItem('恢复正常模式', on_restore),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('WARP排除管理', lambda i, item: self.show_exclusion()),
            pystray.MenuItem('流量监控', lambda i, item: self.show_traffic_monitor()),
            pystray.MenuItem('流量可视化', lambda i, item: self.show_flow_monitor()),
            pystray.MenuItem('打开设置', lambda i, item: self.show_settings()),
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
            startup_enabled = CONFIG.get('auto_startup', False)
            startup_label = '取消开机自启' if startup_enabled else '设置开机自启'
            menu_items = [
                pystray.MenuItem('手动认证', on_auth),
                pystray.MenuItem('注销并重新认证', on_reauth),
                pystray.MenuItem('恢复正常模式', on_restore),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('WARP排除管理', lambda i, item: self.show_exclusion()),
                pystray.MenuItem('流量监控', lambda i, item: self.show_traffic_monitor()),
                pystray.MenuItem('流量可视化', lambda i, item: self.show_flow_monitor()),
                pystray.MenuItem('打开设置', lambda i, item: self.show_settings('settings')),
                pystray.Menu.SEPARATOR,
            ]
            if not is_admin():
                menu_items.append(pystray.MenuItem('以管理员身份运行', lambda i, item: elevate_if_needed()))
                menu_items.append(pystray.Menu.SEPARATOR)
            menu_items.extend([
                pystray.MenuItem(startup_label, self._toggle_startup),
                pystray.MenuItem('查看日志', on_show_log),
                pystray.MenuItem('退出', on_exit),
            ])
            self.icon.menu = pystray.Menu(*menu_items)
            logger.debug(f"Tray menu refreshed, startup={'enabled' if startup_enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"_refresh_tray_menu failed: {e}")

    def show_exclusion(self):
        """显示 WARP 排除管理窗口"""
        logger.info("[show_exclusion] Called")
        if self._exclusion_window:
            try:
                self._exclusion_window.show()
                self._exclusion_window.restore()
                bring_window_to_top('WARP排除管理')
                logger.info("[show_exclusion] Existing window shown")
                return
            except Exception as e:
                logger.error(f"[show_exclusion] Show existing window failed: {e}")
                self._exclusion_window = None

        # 创建新的排除管理窗口
        html_file = get_resource_path('warp_exclusion.html')
        html_url = f'file:///{html_file.replace(chr(92), "/")}'
        # 计算窗口居中位置
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        ex_w, ex_h = 520, 700
        ex_x = (screen_w - ex_w) // 2
        ex_y = (screen_h - ex_h) // 2
        # 关闭时清理引用（不调用 destroy，避免 closing 事件递归导致堆栈溢出）
        # 返回 None 允许窗口正常关闭，pywebview 会自动销毁 WebView2 进程
        def _on_exclusion_closing():
            logger.info("[exclusion] Window closing, clearing reference")
            self._exclusion_window = None
            return None
        self._exclusion_window = create_webview_window(
            self.api, 'WARP排除管理', html_url, ex_w, ex_h,
            on_closing_callback=_on_exclusion_closing,
            background_color='#0D0D0D', easy_drag=False,
            x=ex_x, y=ex_y,
        )
        if self._exclusion_window:
            logger.info(f"[show_exclusion] Window created, url={html_url}")

    def show_traffic_monitor(self):
        """显示流量监控窗口"""
        logger.info("[show_traffic_monitor] Called")
        if self._traffic_window:
            try:
                self._traffic_window.show()
                self._traffic_window.restore()
                bring_window_to_top('流量监控')
                logger.info("[show_traffic_monitor] Existing window shown")
                return
            except Exception as e:
                logger.error(f"[show_traffic_monitor] Show existing window failed: {e}")
                self._traffic_window = None

        html_file = get_resource_path('traffic_monitor.html')
        html_url = f'file:///{html_file.replace(chr(92), "/")}'
        # 计算窗口居中位置
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        mon_w, mon_h = 640, 760
        mon_x = (screen_w - mon_w) // 2
        mon_y = (screen_h - mon_h) // 2
        # 关闭时清理引用（不调用 destroy，避免 closing 事件递归导致堆栈溢出）
        def _on_traffic_closing():
            logger.info("[traffic] Window closing, clearing reference")
            self._traffic_window = None
            return None
        self._traffic_window = create_webview_window(
            self.api, '流量监控', html_url, mon_w, mon_h,
            on_closing_callback=_on_traffic_closing,
            background_color='#0D0D0D', easy_drag=False,
            x=mon_x, y=mon_y,
        )
        if self._traffic_window:
            logger.info(f"[show_traffic_monitor] Window created, url={html_url}")

    def show_flow_monitor(self):
        """显示流量可视化窗口（数据流动画）"""
        logger.info("[show_flow_monitor] Called")
        if self._flow_window:
            try:
                self._flow_window.show()
                self._flow_window.restore()
                bring_window_to_top('流量可视化')
                logger.info("[show_flow_monitor] Existing window shown")
                return
            except Exception as e:
                logger.error(f"[show_flow_monitor] Show existing window failed: {e}")
                self._flow_window = None

        html_file = get_resource_path('traffic_flow.html')
        html_url = f'file:///{html_file.replace(chr(92), "/")}'
        # 计算窗口居中位置
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        flow_w, flow_h = 960, 820
        flow_x = (screen_w - flow_w) // 2
        flow_y = (screen_h - flow_h) // 2
        # 关闭时清理引用（不调用 destroy，避免 closing 事件递归导致堆栈溢出）
        def _on_flow_closing():
            logger.info("[flow] Window closing, clearing reference")
            self._flow_window = None
            return None
        self._flow_window = create_webview_window(
            self.api, '流量可视化', html_url, flow_w, flow_h,
            on_closing_callback=_on_flow_closing,
            background_color='#0D0D0D', easy_drag=False,
            x=flow_x, y=flow_y,
        )
        if self._flow_window:
            logger.info(f"[show_flow_monitor] Window created, url={html_url}")

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
                self.settings_window.restore()
                logger.info("[show_settings] Window shown via pywebview")

                hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if hwnd:
                    logger.info(f"[show_settings] Found window hwnd={hwnd}")
                    SW_RESTORE = 9
                    HWND_TOPMOST = -1
                    HWND_NOTOPMOST = -2
                    SWP_NOMOVE = 0x0002
                    SWP_NOSIZE = 0x0001
                    SWP_SHOWWINDOW = 0x0040
                    ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
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

    def save_window_position(self):
        try:
            if self.settings_window:
                # 使用 Win32 API 获取实际窗口位置（更可靠）
                hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if hwnd:
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    x, y = rect.left, rect.top
                    logger.debug(f"save_window_position: Win32 API got ({x}, {y})")
                else:
                    x = self.settings_window.x
                    y = self.settings_window.y
                    logger.debug(f"save_window_position: pywebview got ({x}, {y})")
                # 忽略 (0,0) 和负值（可能是隐藏后的位置）
                if x is not None and y is not None and x > 0 and y > 0:
                    cfg = load_config()
                    cfg['window_x'] = x
                    cfg['window_y'] = y
                    save_config_to_file(cfg)
                    logger.info(f"Window position saved: ({x}, {y})")
                else:
                    logger.warning(f"Window position ({x}, {y}) ignored (too close to edge)")
        except Exception as e:
            logger.error(f"save_window_position exception: {e}")

    def run(self):
        core.state._tray_app_instance = self
        cfg = load_config()
        self.create_tray()
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

        html_file = get_resource_path('settings.html')
        logger.debug(f"run: html_file={html_file}")
        
        if cfg.get('window_x') is not None and cfg.get('window_y') is not None:
            import ctypes as _ct
            user32 = _ct.windll.user32
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            wx, wy = cfg['window_x'], cfg['window_y']
            if wx < 0 or wy < 0 or wx > screen_w - 100 or wy > screen_h - 100:
                logger.info(f"Window position ({wx},{wy}) out of screen ({screen_w}x{screen_h}), centering")
                wx = (screen_w - TrayApp.WIN_W) // 2
                wy = (screen_h - TrayApp.WIN_H) // 2
        else:
            import ctypes as _ct
            user32 = _ct.windll.user32
            wx = (user32.GetSystemMetrics(0) - TrayApp.WIN_W) // 2
            wy = (user32.GetSystemMetrics(1) - TrayApp.WIN_H) // 2

        try:
            html_url = f'file:///{html_file.replace(chr(92), "/")}'
            self.settings_window = webview.create_window(
                'CampusAuth',
                url=html_url,
                js_api=self.api,
                width=TrayApp.WIN_W,
                height=TrayApp.WIN_H,
                x=wx,
                y=wy,
                resizable=False,
                background_color='#0D0D0D',
                easy_drag=True,
                frameless=True,
                hidden=self._silent
            )
            logger.info(f"Window created at ({wx}, {wy}), url={html_url}")
        except Exception as e:
            logger.error(f"run: create_window failed: {e}\n{traceback.format_exc()}")
            return
        
        def on_closing():
            logger.info("[on_closing] Window closing event triggered")
            self.save_window_position()
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

        if not self._silent:
            def ensure_visible():
                try:
                    time.sleep(0.5)
                    if self.settings_window:
                        self.settings_window.show()
                        self.settings_window.restore()
                        logger.info("Non-silent mode: window ensured visible")
                except Exception as e:
                    logger.error(f"Non-silent mode ensure visible failed: {e}")
            threading.Thread(target=ensure_visible, daemon=True).start()

            threading.Thread(target=delayed_init, daemon=True).start()

        self._webview_started = True
        webview.start(debug=False)
        
        if self.icon:
            self.icon.stop()

def main():
    global CONFIG
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
