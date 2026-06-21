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
import functools
import traceback
import tempfile
from pathlib import Path
from ctypes import wintypes
import pystray
from PIL import Image, ImageDraw
import webview
from warp_exclusion import get_exclusion_manager, DnsMonitor

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

def log_func_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.debug(f"→ {func.__name__}() called")
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - t0
            logger.debug(f"← {func.__name__}() returned in {elapsed:.3f}s")
            return result
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"✗ {func.__name__}() failed in {elapsed:.3f}s: {e}\n{traceback.format_exc()}")
            raise
    return wrapper

def check_single_instance():
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

TRAY_MUTEX = None

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
    except:
        return False

def run_command(cmd, shell=True, timeout=30):
    """
    执行命令并返回结果。
    使用临时文件来捕获输出，避免 Windows Store 版 Python 在管理员权限下的 subprocess 管道问题。
    使用 CREATE_NO_WINDOW + SW_HIDE 彻底避免命令行窗口弹窗。
    """
    import tempfile
    import uuid

    # 构建命令字符串
    if isinstance(cmd, list):
        cmd_parts = []
        for part in cmd:
            if ' ' in part or '\t' in part:
                cmd_parts.append(f'"{part}"')
            else:
                cmd_parts.append(part)
        cmd_str = ' '.join(cmd_parts)
    else:
        cmd_str = cmd

    # 创建临时文件（使用唯一标识符避免冲突）
    unique_id = uuid.uuid4().hex
    tmp_out = os.path.join(tempfile.gettempdir(), f'cmd_out_{os.getpid()}_{unique_id}.txt')
    tmp_err = os.path.join(tempfile.gettempdir(), f'cmd_err_{os.getpid()}_{unique_id}.txt')

    # 构建重定向命令
    redirect_cmd = f'chcp 65001 >nul & {cmd_str} > "{tmp_out}" 2> "{tmp_err}"'

    # 使用 subprocess.Popen 避免窗口弹窗
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            redirect_cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            poll_interval = 0.5
            elapsed = 0.0
            exit_code = None
            while elapsed < timeout:
                exit_code = proc.poll()
                if exit_code is not None:
                    break
                if _auth_cancelled.is_set():
                    proc.kill()
                    logger.info(f"run_command: killed due to cancellation: {cmd_str[:80]}")
                    exit_code = -1
                    break
                time.sleep(poll_interval)
                elapsed += poll_interval
            if exit_code is None:
                proc.kill()
                exit_code = -1
        except Exception as e2:
            logger.error(f"run_command wait error: {e2}")
            try:
                proc.kill()
            except:
                pass
            exit_code = -1
    except Exception as e:
        logger.error(f"run_command Popen error: {e}")
        exit_code = -1

    # 读取输出文件
    stdout = ''
    stderr = ''
    for _attempt in range(3):
        try:
            if os.path.exists(tmp_out):
                with open(tmp_out, 'r', encoding='utf-8', errors='replace') as f:
                    stdout = f.read()
                if '\ufffd' in stdout:
                    try:
                        with open(tmp_out, 'r', encoding='gbk', errors='replace') as f:
                            stdout = f.read()
                    except Exception:
                        pass
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass
            break
        except Exception as e:
            if _attempt < 2:
                time.sleep(0.3)
            else:
                logger.debug(f"run_command: failed to read stdout: {e}")

    for _attempt in range(3):
        try:
            if os.path.exists(tmp_err):
                with open(tmp_err, 'r', encoding='utf-8', errors='replace') as f:
                    stderr = f.read()
                if '\ufffd' in stderr:
                    try:
                        with open(tmp_err, 'r', encoding='gbk', errors='replace') as f:
                            stderr = f.read()
                    except Exception:
                        pass
                try:
                    os.remove(tmp_err)
                except Exception:
                    pass
            break
        except Exception as e:
            if _attempt < 2:
                time.sleep(0.3)
            else:
                logger.debug(f"run_command: failed to read stderr: {e}")

    if exit_code == -1:
        stderr = "Command timed out" if not stderr else stderr

    return exit_code, stdout, stderr

def run_command_os_system(cmd_str):
    """使用 subprocess.Popen 执行命令，避免 os.system 的弹窗问题"""
    import tempfile
    import uuid
    unique_id = uuid.uuid4().hex
    tmp_out = os.path.join(tempfile.gettempdir(), f'cmd_out_{os.getpid()}_{unique_id}.txt')
    tmp_err = os.path.join(tempfile.gettempdir(), f'cmd_err_{os.getpid()}_{unique_id}.txt')

    redirect_cmd = f'{cmd_str} >"{tmp_out}" 2>"{tmp_err}"'

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            redirect_cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = -1
    except Exception as e:
        logger.error(f"run_command_os_system Popen error: {e}")
        exit_code = -1

    stdout = ''
    stderr = ''
    try:
        if os.path.exists(tmp_out):
            with open(tmp_out, 'r', encoding='utf-8', errors='ignore') as f:
                stdout = f.read()
            os.remove(tmp_out)
    except:
        pass
    try:
        if os.path.exists(tmp_err):
            with open(tmp_err, 'r', encoding='utf-8', errors='ignore') as f:
                stderr = f.read()
            os.remove(tmp_err)
    except:
        pass
    return exit_code, stdout, stderr

def run_elevated_powershell(ps_command, timeout=30):
    logger.info(f"run_elevated_powershell: cmd={ps_command[:120]!r}")
    tmp_out = os.path.join(tempfile.gettempdir(), f'ipv6_elev_{os.getpid()}_{int(time.time()*1000)}.txt')
    tmp_err = os.path.join(tempfile.gettempdir(), f'ipv6_elev_err_{os.getpid()}_{int(time.time()*1000)}.txt')
    tmp_done = os.path.join(tempfile.gettempdir(), f'ipv6_elev_done_{os.getpid()}_{int(time.time()*1000)}.txt')
    wrapped = (
        f'$ErrorActionPreference="Stop"; '
        f'try {{ {ps_command}; "0" | Out-File -FilePath "{tmp_done}" -Encoding utf8 }} '
        f'catch {{ "1" | Out-File -FilePath "{tmp_done}" -Encoding utf8; $_.Exception.Message | Out-File -FilePath "{tmp_err}" -Encoding utf8 }}'
    )
    full_cmd = f'-ExecutionPolicy Bypass -Command "{wrapped}"'
    logger.debug(f"run_elevated_powershell: full_cmd={full_cmd[:200]!r}")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", full_cmd, None, 0
    )
    logger.debug(f"run_elevated_powershell: ShellExecuteW returned {ret}")
    if ret <= 32:
        logger.error(f"run_elevated_powershell: ShellExecuteW failed with code {ret}")
        for f in [tmp_out, tmp_err, tmp_done]:
            try: os.remove(f)
            except: pass
        return -1, "", f"ShellExecuteW failed with code {ret}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(tmp_done):
            break
        time.sleep(0.3)
    else:
        logger.error(f"run_elevated_powershell: timed out after {timeout}s")
        for f in [tmp_out, tmp_err, tmp_done]:
            try: os.remove(f)
            except: pass
        return -1, "", "Command timed out"
    time.sleep(0.2)
    out_text = ""
    err_text = ""
    try:
        with open(tmp_done, 'r', encoding='utf-8', errors='ignore') as f:
            exit_flag = f.read().strip()
    except:
        exit_flag = "1"
    try:
        if os.path.exists(tmp_err):
            with open(tmp_err, 'r', encoding='utf-8', errors='ignore') as f:
                err_text = f.read().strip()
    except:
        pass
    code = 0 if exit_flag == "0" else 1
    logger.debug(f"run_elevated_powershell: code={code}, err={err_text[:200]!r}")
    for f in [tmp_out, tmp_err, tmp_done]:
        try: os.remove(f)
        except: pass
    return code, out_text, err_text

def get_warp_cli():
    custom = CONFIG.get('warp_cli_path', '').strip()
    if custom:
        if os.path.isfile(custom):
            logger.debug(f"get_warp_cli: using custom path: {custom}")
            return custom
        if os.path.isdir(custom):
            candidate = os.path.join(custom, 'warp-cli.exe')
            if os.path.isfile(candidate):
                logger.debug(f"get_warp_cli: using custom dir: {custom}")
                return candidate
        logger.warning(f"get_warp_cli: custom path not found: {custom}")
    code, _, _ = run_command('warp-cli --version')
    if code == 0:
        logger.debug("get_warp_cli: found in PATH")
        return 'warp-cli'
    default_paths = [
        r'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe',
        r'C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe',
    ]
    for p in default_paths:
        if os.path.isfile(p):
            logger.debug(f"get_warp_cli: found at {p}")
            return p
    logger.warning("get_warp_cli: warp-cli not found")
    return None

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

def scan_wifi_networks():
    code, output, _ = run_command('netsh wlan show networks')
    networks = []
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('SSID') and ':' in line:
            ssid = line.split(':', 1)[1].strip()
            if ssid and ssid not in networks:
                networks.append(ssid)
    return networks

def get_wifi_interface_name():
    code, output, _ = run_command('netsh wlan show interfaces')
    for line in output.split('\n'):
        line = line.strip()
        if (line.startswith('名称') or line.startswith('Name')) and ':' in line:
            return line.split(':', 1)[1].strip()
    return None

def get_local_ip():
    wifi_name = get_wifi_interface_name()
    if wifi_name:
        code, output, _ = run_command('ipconfig')
        lines = output.split('\n')
        found_wifi = False
        for line in lines:
            line_stripped = line.strip()
            if wifi_name in line_stripped or '无线' in line_stripped or 'Wireless' in line_stripped:
                found_wifi = True
                continue
            if found_wifi and ('IPv4' in line_stripped or 'IPv4 地址' in line_stripped) and ':' in line_stripped:
                ip = line_stripped.split(':', 1)[1].strip()
                if ip and not ip.startswith('172.16.'):
                    return ip
                continue
            if found_wifi and line_stripped == '':
                found_wifi = False
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith('172.16.'):
            return ''
        return ip
    except:
        return ''

def get_mac_address():
    code, output, _ = run_command('getmac /fo csv /nh')
    for line in output.split('\n'):
        if line.strip():
            parts = line.split(',')
            mac = parts[0].strip().strip('"').replace('-', '')
            return mac
    return '000000000000'

def disconnect_warp(full=True):
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command([warp_cli, 'status'], shell=False)
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            logger.info("Disconnecting WARP...")
            run_command([warp_cli, 'disconnect'], shell=False)
            if not _interruptible_sleep(1): return False
    if _check_cancel(): return False
    if full:
        logger.info("Stopping WARP service...")
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' in svc_output:
            run_command('net stop "CloudflareWARP"')
        if _check_cancel(): return False
        logger.info("Disabling WARP service auto-start...")
        run_command('sc config "CloudflareWARP" start= disabled')
        if warp_cli:
            run_command([warp_cli, 'set-mode', 'warp+doh'], shell=False)
            run_command([warp_cli, 'disable-wifi'], shell=False)
            run_command([warp_cli, 'disable-ethernet'], shell=False)
        logger.info("Waiting for WARP interface to disappear...")
        for i in range(10):
            if _check_cancel(): return False
            code, output, _ = run_command('netsh interface ipv4 show interfaces')
            if 'CloudflareWARP' not in output:
                logger.info(f"WARP interface disappeared ({i+1} checks)")
                return True
            if not _interruptible_sleep(1): return False
        logger.info("WARP interface still exists, trying to disable...")
        run_command('netsh interface ipv4 set interface "CloudflareWARP" disabled')
        run_command('netsh interface set interface "CloudflareWARP" disable')
        _interruptible_sleep(2)
    return True

@log_func_call
def disable_ipv4(interface_name):
    logger.info(f"disable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Disable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False)
    logger.debug(f"disable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name}")
        return True
    logger.warning(f"disable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd)
    logger.debug(f"disable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name} (elevated)")
        return True
    logger.error(f"disable_ipv4: failed to disable IPv4 on {interface_name} (both methods)")
    return False

@log_func_call
def enable_ipv4(interface_name):
    logger.info(f"enable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Enable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False)
    logger.debug(f"enable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name}")
        return True
    logger.warning(f"enable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd)
    logger.debug(f"enable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name} (elevated)")
        return True
    logger.error(f"enable_ipv4: failed to enable IPv4 on {interface_name} (both methods)")
    return False

def wait_for_network_ready(portal_ip, portal_port='801', max_retries=5):
    logger.info("Waiting for network to be ready...")
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    for i in range(max_retries):
        if _check_cancel(): return False
        try:
            import urllib.request
            req = urllib.request.Request(f'http://{portal_addr}/eportal/portal/login', method='GET')
            req.add_header('User-Agent', 'Mozilla/5.0')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            response = opener.open(req, timeout=5)
            logger.info(f"Network ready ({i+1}/{max_retries}, HTTP {response.status})")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (200, 302, 401, 403):
                logger.info(f"Network ready ({i+1}/{max_retries}, HTTP {e.code})")
                return True
            logger.info(f"Network not ready ({i+1}/{max_retries}): HTTP {e.code}")
        except Exception as e:
            logger.info(f"Network not ready ({i+1}/{max_retries}): {e}")
        if not _interruptible_sleep(2): return False
    logger.info("Network may not be fully connected, continuing...")
    return False

def _wait_for_ipv6_ready(max_retries=8):
    logger.info("Waiting for IPv6 to be ready...")
    ipv6_test_targets = [
        ('2606:4700:d0::a29f:c001', 443),
        ('2606:4700:4700::1111', 443),
        ('2606:4700:103::1', 443),
    ]
    for i in range(max_retries):
        if _check_cancel(): return False
        for addr, port in ipv6_test_targets:
            try:
                import socket
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((addr, port))
                sock.close()
                logger.info(f"IPv6 ready via {addr} ({i+1}/{max_retries})")
                return True
            except Exception as e:
                logger.debug(f"IPv6 not ready via {addr} ({i+1}/{max_retries}): {e}")
        if not _interruptible_sleep(2): return False
    logger.warning(f"IPv6 not ready after {max_retries} retries")
    return False

def portal_login():
    username = CONFIG.get('username', '')
    password = CONFIG.get('password', '')
    portal_ip = CONFIG.get('portal_ip', '10.21.221.98')
    portal_port = CONFIG.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    wait_for_network_ready(portal_ip, portal_port)
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    import urllib.request
    import urllib.parse
    url = f"http://{portal_addr}/eportal/portal/login"
    full_account = username + "@campus"
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': full_account,
        'user_password': password,
        'wlan_user_ip': local_ip,
        'wlan_user_ipv6': '',
        'wlan_user_mac': mac_addr,
        'wlan_ac_ip': '',
        'wlan_ac_name': '',
        'jsVersion': '4.2.1',
        'terminal_type': '1',
        'lang': 'zh-cn',
        'v': '9171'
    }
    logger.info(f"portal_login: username='{username}', password_len={len(password)}, password_prefix='{password[:20]}...' if len(password)>20 else password")
    logger.info(f"portal_login: _pwd_encrypted={CONFIG.get('_pwd_encrypted')}")
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    logger.info(f"Login URL length: {len(full_url)}")
    logger.info(f"Login URL: {full_url[:200]}...")
    logger.info(f"Login URL (password param): user_password={params['user_password'][:30]}{'...' if len(params['user_password'])>30 else ''}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _attempt in range(3):
        try:
            req = urllib.request.Request(full_url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
            req.add_header('Referer', f'http://{portal_addr}/eportal/portal.jsp')
            response = opener.open(req, timeout=10)
            result = response.read().decode('utf-8')
            logger.info(f"Response: {result}")
            if '"result":1' in result or '"result": 1' in result:
                return True, "认证成功"
            elif '已经在线' in result or 'already online' in result or '"ret_code":2' in result:
                return True, "IP已经在线"
            elif 'AC' in result:
                return False, "AC认证失败"
            else:
                return False, f"认证失败: {result[:100]}"
        except urllib.error.HTTPError as e:
            if e.code in (502, 503) and _attempt < 2:
                logger.warning(f"Portal login got HTTP {e.code}, retrying ({_attempt+1}/3)...")
                if not _interruptible_sleep(3): return False, "已取消"
                continue
            return False, f"认证请求失败: HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            if _attempt < 2 and ('timed out' in str(e) or 'Connection' in str(e)):
                logger.warning(f"Portal login error, retrying ({_attempt+1}/3): {e}")
                if not _interruptible_sleep(3): return False, "已取消"
                continue
            return False, f"认证请求失败: {e}"

def portal_logout():
    username = CONFIG.get('username', '')
    password = CONFIG.get('password', '')
    portal_ip = CONFIG.get('portal_ip', '10.21.221.98')
    portal_port = CONFIG.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    import urllib.request
    import urllib.parse
    url = f"http://{portal_addr}/eportal/portal/logout"
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': username + "@campus",
        'user_password': password,
        'ac_logout': '0',
        'register_mode': '0',
        'wlan_user_ip': local_ip,
        'wlan_user_ipv6': '',
        'wlan_vlan_id': '0',
        'wlan_user_mac': mac_addr,
        'wlan_ac_ip': '',
        'wlan_ac_name': '',
        'jsVersion': '4.2.1',
        'v': '7724',
        'lang': 'zh'
    }
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    logger.info(f"Logout URL: {full_url[:80]}...")
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Accept', '*/*')
        req.add_header('Referer', f'http://{portal_addr}/eportal/portal.jsp')
        req.add_header('Connection', 'keep-alive')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = opener.open(req, timeout=15)
        result = response.read().decode('utf-8')
        logger.info(f"Logout response: {result}")
        _interruptible_sleep(3)
        return True
    except Exception as e:
        logger.error(f"Logout failed: {e}")
        return False

_conf_json_backup = None

def _set_warp_masque_mode(warp_cli, enable):
    if not warp_cli:
        logger.warning("warp-cli not found, cannot set MASQUE mode")
        return False
    try:
        if enable:
            logger.info("Setting WARP tunnel protocol to MASQUE with h3-with-h2-fallback...")
            run_command([warp_cli, 'tunnel', 'protocol', 'set', 'MASQUE'], shell=False)
            run_command([warp_cli, 'tunnel', 'masque-options', 'set', 'h3-with-h2-fallback'], shell=False)
            logger.info("MASQUE h3-with-h2-fallback mode set (QUIC/UDP:443 with TCP/443 fallback)")
        else:
            logger.info("Resetting WARP tunnel protocol to default...")
            run_command([warp_cli, 'tunnel', 'protocol', 'reset'], shell=False)
            run_command([warp_cli, 'tunnel', 'masque-options', 'reset'], shell=False)
            logger.info("WARP tunnel protocol reset to default")
        return True
    except Exception as e:
        logger.error(f"Failed to set MASQUE mode: {e}")
        return False

def _set_warp_endpoint_ipv6(enable):
    global _conf_json_backup
    conf_path = os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'),
                              'Cloudflare', 'conf.json')
    try:
        if not os.path.exists(conf_path):
            logger.warning(f"WARP conf.json not found at {conf_path}")
            return False
        with open(conf_path, 'r', encoding='utf-8') as f:
            conf = json.load(f)
        if enable:
            _conf_json_backup = json.dumps(conf)
            if 'endpoints' in conf:
                for ep in conf['endpoints']:
                    ep['v4'] = ''
                logger.info(f"Cleared IPv4 endpoints in conf.json ({len(conf['endpoints'])} endpoints)")
            else:
                logger.warning("No endpoints found in conf.json")
        else:
            if _conf_json_backup:
                conf = json.loads(_conf_json_backup)
                _conf_json_backup = None
                logger.info("Restored original conf.json endpoints")
            else:
                if 'endpoints' in conf:
                    for ep in conf['endpoints']:
                        if not ep.get('v4'):
                            ep['v4'] = '162.159.198.2:443'
                    logger.info("Reconstructed IPv4 endpoints in conf.json")
        with open(conf_path, 'w', encoding='utf-8') as f:
            json.dump(conf, f)
        return True
    except Exception as e:
        logger.error(f"Failed to update WARP conf.json: {e}")
        return False

def connect_warp(force_restart=False):
    logger.info("Connecting to WARP...")
    warp_cli = get_warp_cli()
    if not warp_cli:
        logger.error("warp-cli not found")
        return False
    run_command('sc config "CloudflareWARP" start= auto')
    if force_restart:
        logger.info("Force restarting WARP service to apply config changes...")
        run_command('net stop "CloudflareWARP"')
        time.sleep(2)
        run_command('net start "CloudflareWARP"')
        time.sleep(3)
    return _connect_warp_inner(warp_cli)

def _connect_warp_inner(warp_cli):
    for attempt in range(2):
        if _check_cancel(): return False
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' not in svc_output:
            logger.info(f"Starting WARP service (attempt {attempt+1})...")
            run_command('net start "CloudflareWARP"')
            if not _interruptible_sleep(5): return False
            code, svc_output, _ = run_command('sc query "CloudflareWARP"')
            if 'RUNNING' not in svc_output:
                logger.warning(f"WARP service failed to start, retrying...")
                if not _interruptible_sleep(3): return False
                continue
        code, output, _ = run_command([warp_cli, 'status'], shell=False)
        logger.debug(f"connect_warp: initial status: {output.strip()[:150]}")
        if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
            logger.info("WARP already connected")
            return True
        if 'Manual Disconnection' in output or 'Account is disconnected' in output:
            logger.info("WARP in disconnected state, re-enabling networks...")
            run_command([warp_cli, 'enable-wifi'], shell=False)
            run_command([warp_cli, 'enable-ethernet'], shell=False)
            if not _interruptible_sleep(1): return False
            if attempt > 0:
                logger.info("Still stuck in Manual Disconnection, restarting WARP service...")
                run_command('net stop "CloudflareWARP"')
                if not _interruptible_sleep(3): return False
                run_command('net start "CloudflareWARP"')
                if not _interruptible_sleep(5): return False
                code, output, _ = run_command([warp_cli, 'status'], shell=False)
                logger.debug(f"connect_warp: status after restart: {output.strip()[:150]}")
                if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                    logger.info("WARP connected after service restart")
                    return True
        if _check_cancel(): return False
        logger.info("WARP not connected, issuing connect command...")
        run_command([warp_cli, 'connect'], shell=False)
        logger.info("Waiting for WARP connection...")
        for i in range(10):
            if not _interruptible_sleep(3): return False
            code, output, _ = run_command([warp_cli, 'status'], shell=False)
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                logger.info(f"WARP connected ({i+1} checks)")
                return True
            if 'Manual Disconnection' in output or 'Account is disconnected' in output:
                logger.info(f"WARP fell back to disconnected state ({i+1}/10), breaking inner loop")
                break
            if 'Unable' in output:
                if 'No Network' in output:
                    logger.info(f"WARP reports No Network ({i+1}/10), waiting for IPv6...")
                    continue
                logger.info(f"WARP unable to connect ({i+1}/10), breaking inner loop")
                break
            if i % 3 == 2:
                logger.info(f"WARP still connecting... ({i+1}/10 checks, status: {output.strip()[:100]})")
        logger.warning(f"WARP connection timeout (attempt {attempt+1})")
    logger.warning("WARP connection failed after 2 attempts")
    return False

def is_warp_connected():
    warp_cli = get_warp_cli()
    if not warp_cli:
        return False
    code, output, _ = run_command([warp_cli, 'status'], shell=False, timeout=10)
    if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
        return True
    try:
        ps_cmd = 'Get-NetAdapter -Name *WARP* | Where-Object { $_.Status -eq "Up" } | Select-Object -First 1 -ExpandProperty Name'
        code2, output2, _ = run_command(['powershell', '-Command', ps_cmd], shell=False, timeout=5)
        if code2 == 0 and output2.strip():
            logger.info(f"is_warp_connected: warp-cli failed but WARP adapter '{output2.strip()}' is up")
            return True
    except Exception:
        pass
    return False

def update_tray_icon(success, message=''):
    try:
        if _tray_app_instance and _tray_app_instance.icon:
            if success:
                _tray_app_instance.icon.icon = create_icon('orange')
                _tray_app_instance.icon.title = message or 'WARP已连接'
            else:
                _tray_app_instance.icon.icon = create_icon('red')
                _tray_app_instance.icon.title = message or '认证失败'
    except Exception as e:
        logger.error(f"update_tray_icon failed: {e}")

def update_tray_icon_restore(success, message=''):
    try:
        if _tray_app_instance and _tray_app_instance.icon:
            if success:
                _tray_app_instance.icon.icon = create_icon('green')
                _tray_app_instance.icon.title = message or '已恢复正常'
            else:
                _tray_app_instance.icon.icon = create_icon('red')
                _tray_app_instance.icon.title = message or '恢复失败'
    except Exception as e:
        logger.error(f"update_tray_icon_restore failed: {e}")

WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"
_auth_lock = threading.Lock()
_auth_cancelled = threading.Event()
_wifi_event_handle = None

def _js_escape(s):
    return json.dumps(str(s), ensure_ascii=False)

def _is_cancelled():
    return _auth_cancelled.is_set()

def _check_cancel():
    if _auth_cancelled.is_set():
        logger.info("Operation cancelled by user")
        return True
    return False

def _interruptible_sleep(seconds, check_interval=0.5):
    elapsed = 0.0
    while elapsed < seconds:
        if _auth_cancelled.is_set():
            return False
        sleep_time = min(check_interval, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    return True

def _create_event_with_acl(name):
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

def get_current_wifi_ssid():
    code, output, _ = run_command('netsh wlan show interfaces')
    for line in output.split('\n'):
        line_stripped = line.strip()
        if (line_stripped.startswith('SSID') or line_stripped.startswith('配置文件')) and ':' in line_stripped:
            ssid = line_stripped.split(':', 1)[1].strip()
            if ssid:
                return ssid
    return ''

def wifi_event_monitor():
    global _wifi_event_handle
    try:
        _wifi_event_handle = _create_event_with_acl(WIFI_EVENT_NAME)
        if not _wifi_event_handle:
            logger.error(f"Failed to create WiFi event handle: error={ctypes.get_last_error()}")
            return
        logger.info("WiFi event monitor thread started")
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        while True:
            result = kernel32.WaitForSingleObject(_wifi_event_handle, 0xFFFFFFFF)
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
        global _wifi_monitor_started
        _wifi_monitor_started = False

_wifi_monitor_started = False

def start_wifi_event_monitor():
    global _wifi_monitor_started
    if _wifi_monitor_started:
        logger.info("WiFi event monitor already running, skipping")
        return
    _wifi_monitor_started = True
    t = threading.Thread(target=wifi_event_monitor, daemon=True)
    t.start()
    logger.info("WiFi event monitor thread launched")

def check_startup_wifi_and_auth():
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
    try:
        if not _tray_app_instance or not _tray_app_instance.icon:
            return
        status = _tray_app_instance.api.check_network_status()
        s = status.get('status', 'disconnected')
        if s == 'connected' or s == 'partial':
            _tray_app_instance.icon.icon = create_icon('orange')
            _tray_app_instance.icon.title = status.get('message', 'WARP已连接')
        elif s == 'normal':
            _tray_app_instance.icon.icon = create_icon('green')
            _tray_app_instance.icon.title = '正常模式'
        else:
            _tray_app_instance.icon.icon = create_icon('gray')
            _tray_app_instance.icon.title = '未连接'
    except Exception as e:
        logger.error(f"_update_tray_status failed: {e}")

def cleanup_wifi_event():
    global _wifi_event_handle
    if _wifi_event_handle:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(_wifi_event_handle)
        _wifi_event_handle = None
        logger.info("WiFi event handle released")

def _push_auth_progress(step, total, message, status='running', action='auth'):
    try:
        if _tray_app_instance and _tray_app_instance.settings_window:
            js_code = f"onAuthProgress({{step:{step}, total:{total}, message:{_js_escape(message)}, status:{_js_escape(status)}, action:{_js_escape(action)}}})"
            _tray_app_instance.settings_window.evaluate_js(js_code)
            logger.debug(f"push_auth_progress: step={step}/{total}, status={status}, action={action}, msg={message}")
    except Exception as e:
        logger.error(f"push_auth_progress failed: {e}")

def run_auth_task():
    logger.info("=" * 60)
    logger.info("Starting authentication process")
    logger.info("=" * 60)
    _auth_cancelled.clear()
    wifi_name = CONFIG.get('wifi_name', '')
    if not wifi_name:
        logger.error("WiFi name not configured")
        return False, "WiFi名称未配置"
    if not CONFIG.get('username') or not CONFIG.get('password'):
        logger.error("Credentials not configured")
        return False, "账号或密码未配置"
    _push_auth_progress(0, 5, '检查WiFi连接...')
    logger.info(f"[0/5] Checking WiFi connection to: {wifi_name}")
    code, output, err = run_command('netsh wlan show interfaces')
    logger.debug(f"netsh wlan show interfaces output:\n{output[:500]}")
    wifi_connected = wifi_name in output and ("已连接" in output or "connected" in output.lower())
    if not wifi_connected:
        logger.info(f"Not connected to {wifi_name}, attempting to connect...")
        code, output, err = run_command(f'netsh wlan connect name="{wifi_name}"')
        if code != 0:
            error_detail = output.strip() or err.strip() or f"返回码={code}"
            logger.error(f"Failed to connect to WiFi: {error_detail}")
            _push_auth_progress(0, 5, f'WiFi连接失败: {error_detail}', 'error')
            return False, f"WiFi连接失败: {error_detail}"
        for retry in range(8):
            if not _interruptible_sleep(2): return False, "已取消"
            code, output, _ = run_command('netsh wlan show interfaces')
            if wifi_name in output and ("已连接" in output or "connected" in output.lower()):
                wifi_connected = True
                break
            logger.debug(f"WiFi not ready yet (check {retry+1}/8)")
        if not wifi_connected:
            logger.error(f"WiFi connected but target SSID not confirmed")
            _push_auth_progress(0, 5, f'未检测到目标网络 {wifi_name}', 'error')
            return False, f"WiFi连接后未检测到目标网络 {wifi_name}"
    if _check_cancel(): return False, "已取消"
    logger.info(f"Connected to target WiFi: {wifi_name}")
    interface_name = get_wifi_interface_name()
    if not interface_name:
        logger.error("Cannot get WiFi interface name")
        _push_auth_progress(0, 5, '无法获取WiFi接口名称', 'error')
        return False, "无法获取WiFi接口名称"
    if is_warp_connected():
        logger.info("WARP already connected, checking IPv4 status...")
        ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
        code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False)
        if 'False' in output:
            logger.info("WARP connected and IPv4 disabled, no need to re-authenticate")
            _push_auth_progress(5, 5, '已认证，WARP已连接', 'success')
            return True, "已认证，WARP已连接"
        logger.info("WARP connected but IPv4 still enabled, disabling IPv4...")
        if disable_ipv4(interface_name):
            _push_auth_progress(5, 5, '已认证，WARP已连接', 'success')
            return True, "已认证，WARP已连接"
        else:
            _push_auth_progress(5, 5, 'WARP已连接，但IPv4禁用失败', 'error')
            return False, "WARP已连接，但IPv4禁用失败"
    if _check_cancel(): return False, "已取消"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(1, 5, '断开WARP...')
    logger.info("[1/5] Checking WARP...")
    disconnect_warp(full=False)
    code, svc_output, _ = run_command('sc query "CloudflareWARP"')
    warp_service_was_running = 'RUNNING' in svc_output
    if warp_service_was_running:
        logger.info("Disabling WARP virtual adapter to release network filter...")
        run_command('netsh interface set interface "CloudflareWARP" disable')
        _interruptible_sleep(1)
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(2, 5, '启用IPv4...')
    logger.info("[2/5] Checking IPv4 status...")
    ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
    code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False)
    if 'True' not in output:
        logger.info("IPv4 disabled, enabling...")
        if not enable_ipv4(interface_name):
            _push_auth_progress(2, 5, 'IPv4启用失败', 'error')
            return False, "IPv4启用失败"
        for ip_retry in range(6):
            if _check_cancel(): return False, "已取消"
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1)
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
                if ip and not ip.startswith('127.'):
                    logger.info(f"IPv4 address obtained: {ip}")
                    break
            except Exception:
                pass
            if not _interruptible_sleep(1): return False, "已取消"
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(3, 5, 'Portal认证...')
    logger.info("[3/5] Portal authentication...")
    success, msg = portal_login()
    if not success:
        if 'AC' in msg:
            logger.info("AC auth failed, logging out and retrying...")
            portal_logout()
            if not _interruptible_sleep(2): return False, "已取消"
            if _check_cancel(): return False, "已取消"
            success, msg = portal_login()
        if not success:
            _push_auth_progress(3, 5, msg, 'error')
            logger.warning("Portal auth failed, rolling back: reconnecting WARP")
            disable_ipv4(interface_name)
            if warp_service_was_running:
                run_command('netsh interface set interface "CloudflareWARP" enable')
            connect_warp()
            return False, msg
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(4, 5, '设置IPv6 DNS...')
    logger.info("[4/5] Setting IPv6 DNS before disabling IPv4...")
    run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" static 2606:4700:4700::1111 primary')
    run_command(f'netsh interface ipv6 add dnsservers "{interface_name}" 2606:4700:4700::1001 index=2')
    logger.info("IPv6 DNS set to Cloudflare (2606:4700:4700::1111, 2606:4700:4700::1001)")
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(5, 5, '禁用IPv4并连接WARP...')
    logger.info("[5/5] Disabling IPv4 and connecting WARP...")
    if not disable_ipv4(interface_name):
        _push_auth_progress(5, 5, '禁用IPv4失败', 'error')
        return False, "禁用IPv4失败"
    if _check_cancel(): return False, "已取消"
    if warp_service_was_running:
        logger.info("Re-enabling WARP virtual adapter...")
        run_command('netsh interface set interface "CloudflareWARP" enable')
    if not _wait_for_ipv6_ready(max_retries=5):
        _push_auth_progress(5, 5, 'IPv6网络不可用', 'error')
        return False, "IPv6网络不可用，无法连接WARP"
    run_command('sc config "CloudflareWARP" start= auto')
    code, svc_output, _ = run_command('sc query "CloudflareWARP"')
    if 'RUNNING' not in svc_output:
        logger.info("Starting WARP service for MASQUE config...")
        run_command('net start "CloudflareWARP"')
        time.sleep(3)
    warp_cli = get_warp_cli()
    _set_warp_masque_mode(warp_cli, True)
    if not connect_warp():
        _set_warp_masque_mode(warp_cli, False)
        _push_auth_progress(5, 5, 'WARP连接超时，请手动检查', 'error')
        return False, "WARP连接超时，请手动检查"
    _set_warp_masque_mode(warp_cli, False)
    logger.info("=" * 60)
    logger.info("Authentication completed successfully")
    logger.info("=" * 60)
    _push_auth_progress(5, 5, '认证成功', 'success')
    return True, "认证成功"

def run_restore_task():
    logger.info("=" * 60)
    logger.info("Restoring normal network mode")
    logger.info("=" * 60)
    _auth_cancelled.clear()
    interface_name = get_wifi_interface_name()
    if not interface_name:
        interface_name = "WLAN"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(1, 2, '恢复网络...', action='restore')
    logger.info("[1/2] Disconnecting WARP and enabling IPv4 in parallel...")

    warp_result = [None]
    ipv4_result = [None]

    def _disconnect_warp_thread():
        warp_result[0] = disconnect_warp()

    def _enable_ipv4_thread():
        ipv4_result[0] = enable_ipv4(interface_name)
        run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp')

    t_warp = threading.Thread(target=_disconnect_warp_thread, daemon=True)
    t_ipv4 = threading.Thread(target=_enable_ipv4_thread, daemon=True)
    t_warp.start()
    t_ipv4.start()
    t_warp.join()
    t_ipv4.join()

    if _check_cancel(): return False, "已取消"
    if not ipv4_result[0]:
        logger.warning("enable_ipv4 failed, rolling back: reconnecting WARP")
        connect_warp()
        _push_auth_progress(2, 2, '启用IPv4失败，已恢复WARP', 'error', action='restore')
        return False, "启用IPv4失败，已恢复WARP"
    _push_auth_progress(2, 2, '验证网络...', action='restore')
    logger.info("[2/2] Verifying network...")
    if not _interruptible_sleep(3): return False, "已取消"
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.'])
    if not has_ipv4:
        logger.warning("No valid IPv4 address found after enabling")
        _push_auth_progress(2, 2, 'IPv4未获取到有效地址', 'error', action='restore')
        return False, "IPv4未获取到有效地址"
    try:
        import urllib.request
        req = urllib.request.Request('http://www.baidu.com', method='HEAD')
        urllib.request.urlopen(req, timeout=5)
        logger.info("Network connectivity verified")
        _push_auth_progress(2, 2, '网络已恢复正常模式', 'success', action='restore')
        return True, "网络已恢复正常模式"
    except Exception as e:
        logger.warning(f"IPv4 has IP but no internet: {e}")
        _push_auth_progress(3, 3, 'IPv4已启用，但可能需要Portal认证', 'success', action='restore')
        return True, "IPv4已启用，但可能需要Portal认证"

def _build_schtasks_tr(extra_args=''):
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
    if is_admin():
        logger.info("Already running as admin, setting up startup task")
        args = '--silent' if CONFIG.get('silent_startup') else ''
        tr_value = _build_schtasks_tr(args)
        cmd_str = f'schtasks /Create /TN "{TASK_NAME_STARTUP}" /TR "{tr_value}" /SC ONLOGON /RL HIGHEST /F'
        logger.info(f"setup_startup_task: cmd={cmd_str}")
        code, output, err = run_command_os_system(cmd_str)
        if code == 0:
            logger.info("Startup task created successfully")
            return True
        else:
            logger.error(f"Failed to create startup task: code={code}, output={output}, err={err}")
            return False
    return False

def register_wifi_event_task():
    if not CONFIG.get('wifi_name'):
        return False
    try:
        run_command_os_system('schtasks /Delete /TN WiFiAutoAuthEvent /F')
        logger.info("Old WiFi event task deleted (if existed)")
    except Exception:
        pass
    tr_value = _build_schtasks_tr('--wifi-event')
    event_channel = 'Microsoft-Windows-WLAN-AutoConfig/Operational'
    event_filter = "*[System[Provider[@Name='Microsoft-Windows-WLAN-AutoConfig'] and EventID=8001]]"
    cmd_str = f'schtasks /Create /TN "WiFiAutoAuthEvent" /TR "{tr_value}" /SC ONEVENT /EC "{event_channel}" /MO "{event_filter}" /RL HIGHEST /F'
    logger.info(f"register_wifi_event_task: cmd={cmd_str}")
    try:
        code, output, err = run_command_os_system(cmd_str)
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
    try:
        run_command_os_system('schtasks /Delete /TN WiFiAutoAuthEvent /F')
        logger.info("WiFi event task unregistered")
    except:
        pass

def check_startup_status():
    code, output, _ = run_command_os_system(f'schtasks /Query /TN "{TASK_NAME_STARTUP}"')
    enabled = code == 0
    logger.debug(f"check_startup_status: enabled={enabled}")
    return enabled

def remove_startup_task():
    code, output, _ = run_command_os_system(f'schtasks /Delete /TN "{TASK_NAME_STARTUP}" /F')
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
            except:
                pass
            os._exit(0)
        else:
            logger.error(f"elevate_if_needed: ShellExecuteW failed with code {ret}")
    except Exception as e:
        logger.error(f"Elevation failed: {e}")

class ApiBridge:
    def load_config(self):
        return load_config()

    def minimize_window(self):
        try:
            if _tray_app_instance and _tray_app_instance.settings_window:
                _tray_app_instance.settings_window.minimize()
                logger.info("Window minimized via title bar button")
        except Exception as e:
            logger.error(f"minimize_window failed: {e}")

    def close_window(self):
        try:
            if _tray_app_instance and _tray_app_instance.settings_window:
                _tray_app_instance.settings_window.hide()
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
            if _tray_app_instance and _tray_app_instance.settings_window:
                js_code = f"onAuthProgress({{step:0, total:1, message:{_js_escape('已取消')}, status:{_js_escape('cancelled')}}})"
                _tray_app_instance.settings_window.evaluate_js(js_code)
            return {'success': True, 'message': '已取消'}
        return {'success': True, 'message': '没有正在进行的操作'}

    def test_auth(self):
        def _do_auth():
            if not _auth_lock.acquire(blocking=False):
                js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape('认证正在进行中，请稍候')}, status:{_js_escape('error')}}})"
                if _tray_app_instance and _tray_app_instance.settings_window:
                    _tray_app_instance.settings_window.evaluate_js(js_code)
                return
            try:
                success, msg = run_auth_task()
                if _auth_cancelled.is_set():
                    logger.info("test_auth: operation was cancelled, skipping final notification")
                else:
                    status = "success" if success else "error"
                    js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(msg)}, status:{_js_escape(status)}}})"
                    if _tray_app_instance and _tray_app_instance.settings_window:
                        _tray_app_instance.settings_window.evaluate_js(js_code)
                    update_tray_icon(success, msg)
            except Exception as e:
                logger.error(f"test_auth thread error: {e}")
                if not _auth_cancelled.is_set():
                    js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(str(e))}, status:{_js_escape('error')}}})"
                    if _tray_app_instance and _tray_app_instance.settings_window:
                        _tray_app_instance.settings_window.evaluate_js(js_code)
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
        elif need_monitor and not _wifi_monitor_started:
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
            has_internet = self._check_internet()
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

    def _check_internet(self):
        try:
            import socket
            socket.create_connection(('8.8.8.8', 53), timeout=3)
            return True
        except Exception:
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
                    if _tray_app_instance and _tray_app_instance.settings_window:
                        _tray_app_instance.settings_window.evaluate_js(js_code)
                    return
            try:
                success, msg = run_restore_task()
                if _auth_cancelled.is_set():
                    logger.info("restore_network: operation was cancelled, skipping final notification")
                else:
                    status = "success" if success else "error"
                    js_code = f"onAuthProgress({{step:3, total:3, message:{_js_escape(msg)}, status:{_js_escape(status)}, action:'restore'}})"
                    if _tray_app_instance and _tray_app_instance.settings_window:
                        _tray_app_instance.settings_window.evaluate_js(js_code)
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
                if _tray_app_instance:
                    _tray_app_instance._refresh_tray_menu()
                return {'success': True, 'message': '开机自启已开启'}
            return {'success': False, 'message': '设置失败'}
        else:
            remove_startup_task()
            CONFIG['auto_startup'] = False
            save_config_to_file(CONFIG)
            if _tray_app_instance:
                _tray_app_instance._refresh_tray_menu()
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
            except:
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
        """关闭 WARP 排除管理窗口"""
        if _tray_app_instance and _tray_app_instance._exclusion_window:
            _tray_app_instance._exclusion_window.hide()
            _tray_app_instance._exclusion_window = None

icon_instance = None
_tray_app_instance = None

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

def on_exit(icon, item):
    logger.info("on_exit: user clicked Exit")
    global _tray_app_instance
    if _tray_app_instance:
        _tray_app_instance._should_exit = True
        if not _tray_app_instance._webview_started:
            _tray_app_instance._webview_start_event.set()
    cleanup_wifi_event()
    icon.stop()
    if _tray_app_instance and _tray_app_instance.settings_window:
        _tray_app_instance.save_window_position()
        _tray_app_instance.settings_window.destroy()
    global TRAY_MUTEX
    if TRAY_MUTEX:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(TRAY_MUTEX)
        TRAY_MUTEX = None
        logger.debug("on_exit: mutex released")
    logger.info("on_exit: application exiting")

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
            pystray.MenuItem('恢复正常模式', on_restore),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('WARP排除管理', lambda i, item: self.show_exclusion()),
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
                pystray.MenuItem('恢复正常模式', on_restore),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('WARP排除管理', lambda i, item: self.show_exclusion()),
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
                hwnd = ctypes.windll.user32.FindWindowW(None, 'WARP排除管理')
                if hwnd:
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
                logger.info("[show_exclusion] Existing window shown")
                return
            except Exception as e:
                logger.error(f"[show_exclusion] Show existing window failed: {e}")
                self._exclusion_window = None

        # 创建新的排除管理窗口
        html_file = get_resource_path('warp_exclusion.html')
        html_url = f'file:///{html_file.replace(chr(92), "/")}'
        try:
            user32 = ctypes.windll.user32
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            ex_w, ex_h = 520, 700
            ex_x = (screen_w - ex_w) // 2
            ex_y = (screen_h - ex_h) // 2

            self._exclusion_window = webview.create_window(
                'WARP排除管理',
                url=html_url,
                js_api=self.api,
                width=ex_w,
                height=ex_h,
                x=ex_x,
                y=ex_y,
                resizable=True,
                background_color='#0D0D0D',
                easy_drag=True,
                frameless=True,
            )
            logger.info(f"[show_exclusion] Window created, url={html_url}")
        except Exception as e:
            logger.error(f"[show_exclusion] create_window failed: {e}\n{traceback.format_exc()}")

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
        global _tray_app_instance
        _tray_app_instance = self
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
    global CONFIG, TRAY_MUTEX
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
    TRAY_MUTEX = check_single_instance()
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
