"""网络检测模块。

包含 WiFi 扫描、IP/MAC 获取、IPv6 就绪检测、WARP 连接状态检测等功能。
"""
import ctypes
import logging
import sys
import time

from core.command import run_command
from core.state import _auth_cancelled

logger = logging.getLogger('wifi_tray')


def _check_cancel():
    """检查用户是否取消操作（本地辅助，避免与 tray_app 循环导入）。"""
    if _auth_cancelled.is_set():
        logger.info("Operation cancelled by user")
        return True
    return False


def _interruptible_sleep(seconds, check_interval=0.5):
    """可中断的 sleep（本地辅助，避免与 tray_app 循环导入）。"""
    elapsed = 0.0
    while elapsed < seconds:
        if _auth_cancelled.is_set():
            return False
        sleep_time = min(check_interval, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    return True


def _read_wifi_networks():
    """读取系统已缓存的 WiFi 列表（netsh 只返回上一次扫描的缓存结果）。"""
    code, output, _ = run_command('netsh wlan show networks', timeout=5)
    networks = []
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('SSID') and ':' in line:
            ssid = line.split(':', 1)[1].strip()
            if ssid and ssid not in networks:
                networks.append(ssid)
    return networks


_WLAN_SCAN_TIMEOUT = 5.0


def _trigger_wlan_scan():
    """通过 Native WiFi API 主动触发一次无线扫描。

    `netsh wlan show networks` 只是读取系统缓存的上次扫描结果，不会发起扫描；
    必须先调用 WlanScan 让无线网卡重新扫描，否则列表会长期停留在旧结果上
    （表现为：只有点开 Windows 的 WiFi 面板后 CampusAuth 才能读到网络）。
    返回 True 表示已成功下发扫描请求。
    """
    if sys.platform != 'win32':
        return False
    try:
        wlanapi = ctypes.WinDLL('wlanapi')
    except Exception as exc:  # 非 Windows 或缺少 wlanapi
        logger.warning('wlanapi 不可用，跳过主动扫描: %s', exc)
        return False

    class GUID(ctypes.Structure):
        _fields_ = [
            ('Data1', ctypes.c_ulong),
            ('Data2', ctypes.c_ushort),
            ('Data3', ctypes.c_ushort),
            ('Data4', ctypes.c_ubyte * 8),
        ]

    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [
            ('InterfaceGuid', GUID),
            ('strInterfaceDescription', ctypes.c_wchar * 256),
            ('isState', ctypes.c_uint),
        ]

    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ('dwNumberOfItems', ctypes.c_ulong),
            ('dwIndex', ctypes.c_ulong),
            ('InterfaceInfo', WLAN_INTERFACE_INFO * 1),
        ]

    try:
        handle = ctypes.c_void_p()
        negotiated = ctypes.c_ulong()
        wlanapi.WlanOpenHandle.argtypes = [
            ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
        ]
        wlanapi.WlanOpenHandle.restype = ctypes.c_ulong
        ret = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
        if ret != 0:
            logger.warning('WlanOpenHandle 失败: %s', ret)
            return False

        try:
            wlanapi.WlanEnumInterfaces.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)),
            ]
            wlanapi.WlanEnumInterfaces.restype = ctypes.c_ulong
            wlanapi.WlanScan.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(GUID),
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            wlanapi.WlanScan.restype = ctypes.c_ulong
            wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]

            iface_list = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
            ret = wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(iface_list))
            if ret != 0 or not iface_list:
                logger.warning('WlanEnumInterfaces 失败: %s', ret)
                return False
            try:
                count = int(iface_list.contents.dwNumberOfItems)
                triggered = False
                for i in range(count):
                    info = iface_list.contents.InterfaceInfo[i]
                    ret = wlanapi.WlanScan(
                        handle, ctypes.byref(info.InterfaceGuid), None, None, None)
                    if ret == 0:
                        triggered = True
                    else:
                        # 1061: 服务未启动；5: 拒绝访问；均为环境态，仅记录
                        logger.debug('WlanScan 接口 %d 返回 %s', i, ret)
                return triggered
            finally:
                wlanapi.WlanFreeMemory(iface_list)
        finally:
            wlanapi.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            wlanapi.WlanCloseHandle.restype = ctypes.c_ulong
            wlanapi.WlanCloseHandle(handle, None)
    except Exception as exc:
        logger.warning('主动 WiFi 扫描失败: %s', exc)
        return False


def scan_wifi_networks(force_scan=True):
    """扫描可见 WiFi。

    force_scan=True（默认）时先用 Native WiFi API 触发真实扫描，再轮询读取结果，
    直到拿到与扫描前不同的列表或超时；这样无需用户去点 Windows 的 WiFi 面板。
    """
    if not force_scan:
        return _read_wifi_networks()

    before = _read_wifi_networks()
    if not _trigger_wlan_scan():
        # 无法主动扫描（如无线服务未运行），退回读取系统缓存
        return before

    deadline = time.monotonic() + _WLAN_SCAN_TIMEOUT
    time.sleep(1.0)
    latest = _read_wifi_networks()
    while time.monotonic() < deadline and not _auth_cancelled.is_set():
        if latest and latest != before:
            return latest
        time.sleep(0.6)
        latest = _read_wifi_networks()
    return latest or before


def get_wifi_interface_name():
    code, output, _ = run_command('netsh wlan show interfaces', timeout=3)
    for line in output.split('\n'):
        line = line.strip()
        if (line.startswith('名称') or line.startswith('Name')) and ':' in line:
            return line.split(':', 1)[1].strip()
    return None


def get_local_ip():
    wifi_name = get_wifi_interface_name()
    if wifi_name:
        code, output, _ = run_command('ipconfig', timeout=4)
        lines = output.split('\n')
        found_wifi = False
        for line in lines:
            line_stripped = line.strip()
            # 遇到新的适配器标题时才重置 found_wifi（不在空行时重置，
            # 因为适配器标题行后常紧跟空行，会导致 WLAN 部分的 IPv4 被跳过）
            if 'adapter' in line_stripped.lower() or '适配器' in line_stripped:
                found_wifi = (wifi_name in line_stripped or '无线' in line_stripped or 'Wireless' in line_stripped)
                continue
            if found_wifi and ('IPv4' in line_stripped or 'IPv4 地址' in line_stripped) and ':' in line_stripped:
                ip = line_stripped.split(':', 1)[1].strip()
                if ip and not ip.startswith('172.16.'):
                    return ip
                continue
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith('172.16.'):
            return ''
        return ip
    except Exception:
        return ''


def get_mac_address():
    code, output, _ = run_command('getmac /fo csv /nh', timeout=3)
    for line in output.split('\n'):
        if line.strip():
            parts = line.split(',')
            mac = parts[0].strip().strip('"').replace('-', '')
            return mac
    return '000000000000'


def get_current_wifi_ssid():
    code, output, _ = run_command('netsh wlan show interfaces', timeout=3)
    for line in output.split('\n'):
        line_stripped = line.strip()
        if (line_stripped.startswith('SSID') or line_stripped.startswith('配置文件')) and ':' in line_stripped:
            ssid = line_stripped.split(':', 1)[1].strip()
            if ssid:
                return ssid
    return ''


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


def has_public_ipv6():
    """检测本机是否获取到 2001 开头的公网 IPv6 地址。

    通过解析 ipconfig 输出查找 IPv6 地址，过滤掉链路本地、ULA、
    环回、文档保留和 ORCHIDv1 地址，仅保留 2001 开头的实际公网地址。

    Returns:
        tuple[bool, str]: (是否找到, 第一个匹配的地址)。
                          未找到时地址为空字符串。
    """
    code, output, _ = run_command('ipconfig', timeout=4)
    if code != 0:
        logger.warning("has_public_ipv6: ipconfig failed")
        return False, ''
    # 排除的地址前缀（非公网或保留段）
    excluded_prefixes = (
        'fe80:',      # 链路本地
        'fc',         # ULA 本地唯一（fc00::/7）
        'fd',         # ULA 本地唯一（fc00::/7 的下半段）
        '::1',        # 环回
        '2001:db8:',  # 文档保留
        '2001:0000:', # ORCHIDv1（2001::/32）
        '2001:0:',    # ORCHIDv1 简写形式
    )
    for line in output.split('\n'):
        line_stripped = line.strip()
        # 匹配 IPv6 地址行（中文/英文系统）
        if 'IPv6' not in line_stripped:
            continue
        # 使用 ': '（冒号+空格）分割标签和地址，避免误切 IPv6 地址内部的冒号
        parts = line_stripped.split(': ', 1)
        if len(parts) < 2:
            continue
        addr = parts[1].strip()
        # 跳过临时地址标记和空值
        if not addr or addr.startswith('('):
            continue
        # 去除可能的百分号后缀（如 fe80::1%12）
        addr = addr.split('%')[0].lower()
        # 必须以 2001 开头且不在排除列表中
        if addr.startswith('2001') and not addr.startswith(excluded_prefixes):
            logger.info(f"has_public_ipv6: found public IPv6: {addr}")
            return True, addr
    logger.debug("has_public_ipv6: no public IPv6 address found")
    return False, ''


def _wait_for_ipv6_ready(max_retries=20):
    """等待本机获取到 2001 开头的公网 IPv6 地址。

    每次检测调用 has_public_ipv6()，成功立即返回。
    重试间隔 3 秒，默认 20 次约 60 秒。

    Args:
        max_retries: 最大重试次数，默认 20

    Returns:
        bool: 是否在重试次数内获取到公网 IPv6 地址
    """
    logger.info(f"Waiting for public IPv6 (2001 prefix), max {max_retries} retries...")
    for i in range(max_retries):
        if _check_cancel(): return False
        found, addr = has_public_ipv6()
        if found:
            logger.info(f"Public IPv6 ready: {addr} (retry {i+1}/{max_retries})")
            return True
        if i == 0:
            logger.info("No public IPv6 yet, waiting for assignment...")
        elif (i + 1) % 5 == 0:
            logger.info(f"Still waiting for public IPv6 ({i+1}/{max_retries} retries)")
        if not _interruptible_sleep(3): return False
    logger.warning(f"No public IPv6 address after {max_retries} retries")
    return False


def is_warp_connected():
    # 延迟导入以避免循环依赖（core.warp_manager 由后续任务创建）
    from core.warp_manager import get_warp_cli
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


def _check_internet(timeout=2):
    try:
        import socket
        socket.create_connection(('8.8.8.8', 53), timeout=timeout)
        return True
    except Exception:
        return False
