import os
import sys
import json
import ctypes
import ctypes.wintypes
import threading
import subprocess
import logging
import time
import functools
import traceback
import tempfile
from pathlib import Path
from ctypes import wintypes
import pystray
from PIL import Image, ImageDraw
import webview

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
        logging.FileHandler(str(LOG_FILE), encoding='utf-8'),
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
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    creationflags = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=timeout,
            startupinfo=si,
            creationflags=creationflags
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)

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
            return f'"{custom}"'
        if os.path.isdir(custom):
            candidate = os.path.join(custom, 'warp-cli.exe')
            if os.path.isfile(candidate):
                logger.debug(f"get_warp_cli: using custom dir: {custom}")
                return f'"{candidate}"'
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
            return f'"{p}"'
    logger.warning("get_warp_cli: warp-cli not found")
    return None

def create_icon(color='blue'):
    size = (64, 64)
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        'blue': (246, 131, 32),
        'green': (52, 199, 89),
        'red': (255, 59, 48),
        'orange': (255, 152, 0)
    }
    c = colors.get(color, colors['blue'])
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
        img = create_icon('blue')
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

def disconnect_warp():
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            logger.info("Disconnecting WARP...")
            run_command(warp_cli + ' disconnect')
            time.sleep(2)
    logger.info("Terminating WARP processes...")
    run_command('taskkill /F /IM "Cloudflare WARP.exe" 2>nul')
    run_command('taskkill /F /IM "warp-svc.exe" 2>nul')
    run_command('taskkill /F /IM "Cloudflare WARP Notification.exe" 2>nul')
    time.sleep(3)
    code, svc_output, _ = run_command('sc query "CloudflareWARP"')
    if 'RUNNING' in svc_output:
        logger.info("Stopping WARP service...")
        run_command('net stop "CloudflareWARP"')
        time.sleep(2)
    logger.info("Waiting for WARP interface to disappear...")
    for i in range(10):
        code, output, _ = run_command('netsh interface ipv4 show interfaces')
        if 'CloudflareWARP' not in output:
            logger.info(f"WARP interface disappeared ({i+1} checks)")
            return True
        time.sleep(2)
    logger.info("WARP interface still exists, trying to disable...")
    run_command('netsh interface ipv4 set interface "CloudflareWARP" disabled')
    run_command('netsh interface set interface "CloudflareWARP" disable')
    time.sleep(3)
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

def wait_for_network_ready(portal_ip, max_retries=5):
    logger.info("Waiting for network to be ready...")
    for i in range(max_retries):
        try:
            import urllib.request
            req = urllib.request.Request(f'http://{portal_ip}/a79.htm', method='GET')
            req.add_header('User-Agent', 'Mozilla/5.0')
            response = urllib.request.urlopen(req, timeout=3)
            logger.info(f"Network ready ({i+1}/{max_retries}, HTTP {response.status})")
            return True
        except Exception as e:
            logger.info(f"Network not ready ({i+1}/{max_retries}): {e}")
        time.sleep(2)
    logger.info("Network may not be fully connected, continuing...")
    return False

def portal_login():
    username = CONFIG.get('username', '')
    password = CONFIG.get('password', '')
    portal_ip = CONFIG.get('portal_ip', '10.21.221.98')
    portal_port = CONFIG.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    wait_for_network_ready(portal_ip)
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
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    logger.info(f"Login URL: {full_url[:80]}...")
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Referer', f'http://{portal_addr}/eportal/portal.jsp')
        response = urllib.request.urlopen(req, timeout=10)
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
    except Exception as e:
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
        response = urllib.request.urlopen(req, timeout=15)
        result = response.read().decode('utf-8')
        logger.info(f"Logout response: {result}")
        time.sleep(3)
        return True
    except Exception as e:
        logger.error(f"Logout failed: {e}")
        return False

def connect_warp():
    logger.info("Connecting to WARP...")
    warp_cli = get_warp_cli()
    if not warp_cli:
        logger.error("warp-cli not found")
        return False
    for attempt in range(3):
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' not in svc_output:
            logger.info(f"Starting WARP service (attempt {attempt+1})...")
            run_command('net start "CloudflareWARP"')
            time.sleep(5)
            code, svc_output, _ = run_command('sc query "CloudflareWARP"')
            if 'RUNNING' not in svc_output:
                logger.warning(f"WARP service failed to start, retrying...")
                time.sleep(3)
                continue
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
            logger.info("WARP already connected")
            return True
        logger.info("WARP not connected, issuing connect command...")
        run_command(warp_cli + ' connect')
        logger.info("Waiting for WARP connection...")
        for i in range(20):
            time.sleep(3)
            code, output, _ = run_command(warp_cli + ' status')
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                logger.info(f"WARP connected ({i+1} checks)")
                return True
            if i % 5 == 4:
                logger.info(f"WARP still connecting... ({i+1}/20 checks, status: {output.strip()[:100]})")
        logger.warning(f"WARP connection timeout (attempt {attempt+1})")
    logger.warning("WARP connection failed after 3 attempts")
    return False

WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"
_auth_lock = threading.Lock()
_wifi_event_handle = None

def _js_escape(s):
    return json.dumps(str(s), ensure_ascii=False)

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
                if not cfg.get('auto_auth'):
                    logger.info("Auto-auth disabled, skipping")
                    continue
                target_wifi = cfg.get('wifi_name', '')
                if not target_wifi:
                    logger.info("No target WiFi configured, skipping")
                    continue
                time.sleep(3)
                current_wifi = get_current_wifi_ssid()
                logger.info(f"Current WiFi: {current_wifi!r}, Target WiFi: {target_wifi!r}")
                if current_wifi == target_wifi:
                    if _auth_lock.acquire(blocking=False):
                        try:
                            logger.info("Target WiFi matched, starting auto-auth")
                            success, msg = run_auth_task()
                            logger.info(f"Auto-auth result: {success}, {msg}")
                        finally:
                            _auth_lock.release()
                    else:
                        logger.info("Auth already in progress, skipping")
                else:
                    logger.info(f"Connected to '{current_wifi}', not target '{target_wifi}', skipping")
    except Exception as e:
        logger.error(f"wifi_event_monitor crashed: {e}\n{traceback.format_exc()}")

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
        return
    target_wifi = cfg.get('wifi_name', '')
    if not target_wifi:
        return
    time.sleep(5)
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
            logger.info("WARP already connected on startup, skipping auto-auth")
            return
    current_wifi = get_current_wifi_ssid()
    logger.info(f"Startup WiFi check: current={current_wifi!r}, target={target_wifi!r}")
    if current_wifi == target_wifi:
        if _auth_lock.acquire(blocking=False):
            try:
                logger.info("Connected to target WiFi but WARP not connected, starting auto-auth")
                success, msg = run_auth_task()
                logger.info(f"Startup auto-auth result: {success}, {msg}")
            finally:
                _auth_lock.release()
        else:
            logger.info("Auth already in progress on startup check")

def cleanup_wifi_event():
    global _wifi_event_handle
    if _wifi_event_handle:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CloseHandle(_wifi_event_handle)
        _wifi_event_handle = None
        logger.info("WiFi event handle released")

def _push_auth_progress(step, total, message, status='running'):
    try:
        if _tray_app_instance and _tray_app_instance.settings_window:
            js_code = f"onAuthProgress({{step:{step}, total:{total}, message:{_js_escape(message)}, status:{_js_escape(status)}}})"
            _tray_app_instance.settings_window.evaluate_js(js_code)
            logger.debug(f"push_auth_progress: step={step}/{total}, status={status}, msg={message}")
    except Exception as e:
        logger.error(f"push_auth_progress failed: {e}")

def run_auth_task():
    logger.info("=" * 60)
    logger.info("Starting authentication process")
    logger.info("=" * 60)
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
    if wifi_name not in output or "已连接" not in output:
        logger.info(f"Not connected to {wifi_name}, attempting to connect...")
        code, output, err = run_command(f'netsh wlan connect name="{wifi_name}"')
        if code != 0:
            error_detail = output.strip() or err.strip() or f"返回码={code}"
            logger.error(f"Failed to connect to WiFi: {error_detail}")
            _push_auth_progress(0, 5, f'WiFi连接失败: {error_detail}', 'error')
            return False, f"WiFi连接失败: {error_detail}"
        time.sleep(3)
        code, output, _ = run_command('netsh wlan show interfaces')
        if wifi_name not in output or "已连接" not in output:
            logger.error(f"WiFi connected but target SSID not confirmed")
            _push_auth_progress(0, 5, f'未检测到目标网络 {wifi_name}', 'error')
            return False, f"WiFi连接后未检测到目标网络 {wifi_name}"
    logger.info(f"Connected to target WiFi: {wifi_name}")
    interface_name = get_wifi_interface_name()
    if not interface_name:
        logger.error("Cannot get WiFi interface name")
        _push_auth_progress(0, 5, '无法获取WiFi接口名称', 'error')
        return False, "无法获取WiFi接口名称"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(1, 5, '断开WARP...')
    logger.info("[1/5] Checking WARP...")
    disconnect_warp()
    _push_auth_progress(2, 5, '启用IPv4...')
    logger.info("[2/5] Checking IPv4 status...")
    code, output, _ = run_command(f'powershell -Command "(Get-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip).Enabled"')
    if 'True' not in output:
        logger.info("IPv4 disabled, enabling...")
        if not enable_ipv4(interface_name):
            _push_auth_progress(2, 5, 'IPv4启用失败', 'error')
            return False, "IPv4启用失败"
        time.sleep(5)
    logger.info("Waiting for IP assignment...")
    time.sleep(3)
    _push_auth_progress(3, 5, 'Portal认证...')
    logger.info("[3/5] Portal authentication...")
    success, msg = portal_login()
    if not success:
        if 'AC' in msg:
            logger.info("AC auth failed, logging out and retrying...")
            portal_logout()
            time.sleep(2)
            success, msg = portal_login()
        if not success:
            _push_auth_progress(3, 5, msg, 'error')
            return False, msg
    _push_auth_progress(4, 5, '禁用IPv4...')
    logger.info("[4/5] Disabling IPv4...")
    if not disable_ipv4(interface_name):
        _push_auth_progress(4, 5, '禁用IPv4失败', 'error')
        return False, "禁用IPv4失败"
    _push_auth_progress(5, 5, '连接WARP...')
    logger.info("[5/5] Connecting WARP...")
    if not connect_warp():
        _push_auth_progress(5, 5, 'WARP连接超时，请手动检查', 'error')
        return False, "WARP连接超时，请手动检查"
    logger.info("=" * 60)
    logger.info("Authentication completed successfully")
    logger.info("=" * 60)
    _push_auth_progress(5, 5, '认证成功', 'success')
    return True, "认证成功"

def run_restore_task():
    logger.info("=" * 60)
    logger.info("Restoring normal network mode")
    logger.info("=" * 60)
    _push_auth_progress(1, 3, '断开WARP...')
    logger.info("[1/3] Disconnecting WARP...")
    disconnect_warp()
    interface_name = get_wifi_interface_name()
    if not interface_name:
        interface_name = "WLAN"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(2, 3, '启用IPv4...')
    logger.info("[2/3] Enabling IPv4...")
    if not enable_ipv4(interface_name):
        return False, "启用IPv4失败"
    _push_auth_progress(3, 3, '验证网络...')
    logger.info("[3/3] Verifying network...")
    time.sleep(3)
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
    if has_ipv4:
        logger.info("IPv4 enabled with valid IP")
        return True, "网络已恢复正常模式"
    else:
        return False, "IPv4可能未正确配置"

def _build_schtasks_tr(extra_args=''):
    if getattr(sys, 'frozen', False):
        exe_path = sys.executable
        tr = f'"{exe_path}"'
        if extra_args:
            tr += f' {extra_args}'
    else:
        python_exe = sys.executable
        script = str(SCRIPT_DIR / 'tray_app.py')
        tr = f'"{python_exe}" "{script}"'
        if extra_args:
            tr += f' {extra_args}'
    return tr

def setup_startup_task():
    if is_admin():
        logger.info("Already running as admin, setting up startup task")
        args = '--silent' if CONFIG.get('silent_startup') else ''
        tr_value = _build_schtasks_tr(args)
        cmd_list = ['schtasks', '/Create', '/TN', TASK_NAME_STARTUP, '/TR', tr_value, '/SC', 'ONLOGON', '/RL', 'HIGHEST', '/F']
        logger.info(f"setup_startup_task: cmd={' '.join(cmd_list)}")
        code, output, err = run_command(cmd_list, shell=False)
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
    tr_value = _build_schtasks_tr('--wifi-event')
    event_channel = 'Microsoft-Windows-WLAN-AutoConfig/Operational'
    event_filter = "*[System[Provider[@Name='Microsoft-Windows-WLAN-AutoConfig'] and EventID=8001]]"
    cmd_list = [
        'schtasks', '/Create',
        '/TN', 'WiFiAutoAuthEvent',
        '/TR', tr_value,
        '/SC', 'ONEVENT',
        '/EC', event_channel,
        '/MO', event_filter,
        '/RL', 'HIGHEST',
        '/F'
    ]
    logger.info(f"register_wifi_event_task: cmd={' '.join(cmd_list)}")
    try:
        code, output, err = run_command(cmd_list, shell=False)
        if code == 0:
            logger.info("WiFi event task registered")
            try:
                run_command([
                    'powershell', '-Command',
                    '$t = Get-ScheduledTask -TaskName "WiFiAutoAuthEvent"; '
                    '$t.Settings.DisallowStartIfOnBatteries = $false; '
                    '$t.Settings.StopIfGoingOnBatteries = $false; '
                    '$t.Settings.AllowStartOnDemand = $true; '
                    'Set-ScheduledTask -InputObject $t'
                ], shell=False)
                logger.info("WiFi event task power settings updated")
            except Exception as e2:
                logger.warning(f"Failed to update power settings: {e2}")
            return True
        else:
            logger.error(f"Failed to register WiFi event: code={code}, output={output}, err={err}")
    except Exception as e:
        logger.error(f"Failed to register WiFi event: {e}")
    return False

def unregister_wifi_event_task():
    try:
        run_command(['schtasks', '/Delete', '/TN', 'WiFiAutoAuthEvent', '/F'], shell=False)
        logger.info("WiFi event task unregistered")
    except:
        pass

def check_startup_status():
    code, output, _ = run_command(['schtasks', '/Query', '/TN', TASK_NAME_STARTUP], shell=False)
    enabled = code == 0
    logger.debug(f"check_startup_status: enabled={enabled}")
    return enabled

def remove_startup_task():
    code, output, _ = run_command(['schtasks', '/Delete', '/TN', TASK_NAME_STARTUP, '/F'], shell=False)
    if code == 0:
        logger.info("Startup task removed")
        return True
    logger.error(f"Failed to remove startup task")
    return False

def hide_console():
    try:
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
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, script, None, 1
        )
        logger.debug(f"elevate_if_needed: ShellExecuteW returned {ret}")
        if ret > 32:
            logger.info("Elevated process started, exiting current instance")
            os._exit(0)
        else:
            logger.error(f"elevate_if_needed: ShellExecuteW failed with code {ret}")
    except Exception as e:
        logger.error(f"Elevation failed: {e}")

class ApiBridge:
    def load_config(self):
        return load_config()

    def scan_wifi(self):
        return scan_wifi_networks()

    def save_config(self, config):
        global CONFIG
        logger.info(f"save_config called: wifi_name={config.get('wifi_name')}, username={config.get('username')}, auto_auth={config.get('auto_auth')}")
        cfg = load_config()
        old_auto_auth = cfg.get('auto_auth', False)
        cfg.update(config)
        save_config_to_file(cfg)
        CONFIG = cfg
        if cfg['auto_auth']:
            if not cfg['wifi_name']:
                return {'success': False, 'message': '请先选择或输入WiFi名称'}
            if not old_auto_auth:
                start_wifi_event_monitor()
            if register_wifi_event_task():
                return {'success': True, 'message': '自动认证已启用'}
            else:
                return {'success': False, 'message': '自动认证需要管理员权限才能启用'}
        else:
            if old_auto_auth:
                cleanup_wifi_event()
            unregister_wifi_event_task()
            return {'success': True, 'message': '设置已保存'}

    def test_auth(self):
        def _do_auth():
            if not _auth_lock.acquire(blocking=False):
                js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape('认证正在进行中，请稍候')}, status:{_js_escape('error')}}})"
                if _tray_app_instance and _tray_app_instance.settings_window:
                    _tray_app_instance.settings_window.evaluate_js(js_code)
                return
            try:
                success, msg = run_auth_task()
                status = "success" if success else "error"
                js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(msg)}, status:{_js_escape(status)}}})"
                if _tray_app_instance and _tray_app_instance.settings_window:
                    _tray_app_instance.settings_window.evaluate_js(js_code)
            except Exception as e:
                logger.error(f"test_auth thread error: {e}")
                js_code = f"onAuthProgress({{step:5, total:5, message:{_js_escape(str(e))}, status:{_js_escape('error')}}})"
                if _tray_app_instance and _tray_app_instance.settings_window:
                    _tray_app_instance.settings_window.evaluate_js(js_code)
            finally:
                _auth_lock.release()
        threading.Thread(target=_do_auth, daemon=True).start()
        return {'success': True, 'message': '认证已启动'}

    def auto_save_form(self, form_data):
        cfg = load_config()
        old_auto_auth = cfg.get('auto_auth', False)
        for key in ('wifi_name', 'username', 'password', 'auto_auth', 'warp_cli_path', 'silent_startup', 'portal_ip', 'portal_port'):
            if key in form_data:
                cfg[key] = form_data[key]
        save_config_to_file(cfg)
        global CONFIG
        CONFIG = cfg
        logger.info(f"Auto-saved: wifi={form_data.get('wifi_name')}, user={form_data.get('username')}, auto={form_data.get('auto_auth')}")
        new_auto_auth = cfg.get('auto_auth', False)
        if new_auto_auth and not old_auto_auth:
            start_wifi_event_monitor()
            if register_wifi_event_task():
                logger.info("Auto-auth enabled: WiFi event task registered")
            else:
                logger.warning("Auto-auth enabled: failed to register WiFi event task")
        elif not new_auto_auth and old_auto_auth:
            cleanup_wifi_event()
            unregister_wifi_event_task()
            logger.info("Auto-auth disabled: WiFi event task unregistered")

    def check_network_status(self):
        logger.info("check_network_status called")
        warp_connected = False
        ipv4_disabled = False
        warp_cli = get_warp_cli()
        if warp_cli:
            code, output, _ = run_command(warp_cli + ' status')
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                warp_connected = True
                logger.debug("check_network_status: WARP connected")
        interface_name = get_wifi_interface_name()
        if not interface_name:
            interface_name = 'WLAN'
        code, output, _ = run_command(f'powershell -Command "(Get-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip).Enabled"')
        if 'False' in output:
            ipv4_disabled = True
            logger.debug(f"check_network_status: IPv4 disabled on {interface_name}")
        if warp_connected and ipv4_disabled:
            return {'status': 'connected', 'message': 'WARP已连接，IPv4已禁用'}
        elif warp_connected and not ipv4_disabled:
            return {'status': 'partial', 'message': 'WARP已连接，但IPv4未禁用'}
        elif not warp_connected and ipv4_disabled:
            return {'status': 'broken', 'message': 'IPv4已禁用但WARP未连接'}
        else:
            return {'status': 'disconnected', 'message': '未连接'}

    def restore_network(self):
        logger.info("restore_network called")
        def _do_restore():
            try:
                success, msg = run_restore_task()
                status = "success" if success else "error"
                js_code = f"onAuthProgress({{step:3, total:3, message:{_js_escape(msg)}, status:{_js_escape(status)}}})"
                if _tray_app_instance and _tray_app_instance.settings_window:
                    _tray_app_instance.settings_window.evaluate_js(js_code)
            except Exception as e:
                logger.error(f"restore_network thread error: {e}")
        threading.Thread(target=_do_restore, daemon=True).start()
        return {'success': True, 'message': '恢复已启动'}

    def get_startup_status(self):
        enabled = check_startup_status()
        return {'enabled': enabled}

    def set_startup(self, enabled):
        logger.info(f"set_startup called: enabled={enabled}")
        if enabled:
            if not is_admin():
                return {'success': False, 'message': '需要管理员权限'}
            if setup_startup_task():
                return {'success': True, 'message': '开机自启已开启'}
            return {'success': False, 'message': '设置失败'}
        else:
            if remove_startup_task():
                return {'success': True, 'message': '开机自启已关闭'}
            return {'success': False, 'message': '取消失败'}

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
        return
    try:
        success, msg = run_auth_task()
        if success:
            icon.icon = create_icon('green')
            icon.title = '认证成功'
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
    icon.icon = create_icon('orange')
    icon.title = '正在恢复...'
    icon.notify('正在恢复网络到正常模式...', '校园网助手')
    threading.Thread(target=_run_restore, args=(icon,), daemon=True).start()

def _run_restore(icon):
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

def on_exit(icon, item):
    logger.info("on_exit: user clicked Exit")
    global _tray_app_instance
    if _tray_app_instance:
        _tray_app_instance._should_exit = True
        if _tray_app_instance.settings_window:
            _tray_app_instance.save_window_position()
    cleanup_wifi_event()
    icon.stop()
    if _tray_app_instance and _tray_app_instance.settings_window:
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
        self._should_exit = False
        self._silent = silent

    def create_tray(self):
        self.icon = pystray.Icon('wifi_auto_auth')
        self.icon.icon = create_icon('blue')
        self.icon.title = 'WiFi Auto-Auth'
        menu_items = [
            pystray.MenuItem('手动认证', on_auth),
            pystray.MenuItem('恢复正常模式', on_restore),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('打开设置', lambda i, item: self.show_settings('settings'), default=True),
            pystray.Menu.SEPARATOR,
        ]
        if not is_admin():
            menu_items.append(pystray.MenuItem('以管理员身份运行', lambda i, item: elevate_if_needed()))
            menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.extend([
            pystray.MenuItem('设置开机自启', on_setup_admin),
            pystray.MenuItem('查看日志', on_show_log),
            pystray.MenuItem('退出', on_exit),
        ])
        self.icon.menu = pystray.Menu(*menu_items)

    def show_settings(self, tab='status'):
        logger.debug(f"TrayApp.show_settings(tab={tab!r}) called, window={self.settings_window}")
        if self.settings_window:
            try:
                self.settings_window.show()
                self.settings_window.restore()
                if tab == 'settings':
                    self.settings_window.evaluate_js("switchToTab('settings')")
                logger.debug("TrayApp.show_settings(): window shown and restored")
            except Exception as e:
                logger.error(f"TrayApp.show_settings(): failed: {e}\n{traceback.format_exc()}")

    def save_window_position(self):
        try:
            if self.settings_window:
                x = self.settings_window.x
                y = self.settings_window.y
                if x is not None and y is not None and x >= 0 and y >= 0:
                    cfg = load_config()
                    cfg['window_x'] = x
                    cfg['window_y'] = y
                    save_config_to_file(cfg)
                    logger.info(f"Window position saved: ({x}, {y})")
        except Exception as e:
            logger.error(f"save_window_position exception: {e}")

    def run(self):
        global _tray_app_instance
        _tray_app_instance = self
        cfg = load_config()
        self.create_tray()
        logger.info(f"Tray started (admin: {is_admin()})")

        if cfg.get('auto_auth'):
            start_wifi_event_monitor()
            threading.Thread(target=check_startup_wifi_and_auth, daemon=True).start()

        tray_thread = threading.Thread(target=self.icon.run, daemon=True)
        tray_thread.start()

        html_file = get_resource_path('settings.html')
        logger.debug(f"run: html_file={html_file}")
        cfg = load_config()
        
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
            app_icon = ensure_app_icon()
            logger.debug(f"run: app_icon={app_icon}")
        except Exception as e:
            logger.error(f"run: ensure_app_icon failed: {e}")
            app_icon = None

        try:
            self.settings_window = webview.create_window(
                'CampusAuth',
                url=f'file:///{html_file.replace(chr(92), "/")}',
                js_api=self.api,
                width=TrayApp.WIN_W,
                height=TrayApp.WIN_H,
                x=wx,
                y=wy,
                resizable=False,
                background_color='#FFFFFF',
                easy_drag=True
            )
            logger.info(f"Window created at ({wx}, {wy})")
        except Exception as e:
            logger.error(f"run: create_window failed: {e}\n{traceback.format_exc()}")
            return
        
        def on_closing():
            logger.debug("TrayApp.run(): window closing event")
            if self._should_exit:
                logger.debug("TrayApp.run(): real exit, allowing close")
                return None
            self.save_window_position()
            threading.Timer(0.1, self.settings_window.hide).start()
            logger.debug("TrayApp.run(): hiding to tray, cancelling close")
            return False
        
        self.settings_window.events.closing += on_closing

        _icon_handles = []

        def set_window_icon():
            try:
                ico_path = ensure_app_icon()
                hwnd = ctypes.windll.user32.FindWindowW(None, 'CampusAuth')
                if not hwnd:
                    logger.debug("set_window_icon: FindWindowW returned None, retrying...")
                    time.sleep(0.5)
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

        if self._silent:
            def hide_on_shown():
                try:
                    time.sleep(0.3)
                    if self.settings_window:
                        self.settings_window.hide()
                        logger.info("Silent mode: window hidden on startup")
                except Exception as e:
                    logger.error(f"Silent mode hide failed: {e}")
            threading.Thread(target=hide_on_shown, daemon=True).start()

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
