"""网络检测模块。

包含 WiFi 扫描、IP/MAC 获取、IPv6 就绪检测、WARP 连接状态检测等功能。
"""
import logging
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
    code, output, _ = run_command('getmac /fo csv /nh')
    for line in output.split('\n'):
        if line.strip():
            parts = line.split(',')
            mac = parts[0].strip().strip('"').replace('-', '')
            return mac
    return '000000000000'


def get_current_wifi_ssid():
    code, output, _ = run_command('netsh wlan show interfaces')
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
    code, output, _ = run_command('ipconfig')
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


def _check_internet():
    try:
        import socket
        socket.create_connection(('8.8.8.8', 53), timeout=3)
        return True
    except Exception:
        return False
