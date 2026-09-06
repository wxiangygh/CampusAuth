"""网络检测模块。

包含 WiFi 扫描、IP/MAC 获取、IPv6 就绪检测、WARP 连接状态检测等功能。
"""
import ctypes
import logging
import os
import re
import sys
import tempfile
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
    # cancellable=False：本函数同时服务于状态轮询等后台探测，
    # 取消认证不应让链路检测退化（曾致纯有线机器被误判为 wireless）
    code, output, _ = run_command('netsh wlan show interfaces', timeout=3,
                                  cancellable=False)
    for line in output.split('\n'):
        line = line.strip()
        if (line.startswith('名称') or line.startswith('Name')) and ':' in line:
            return line.split(':', 1)[1].strip()
    return None


def _wlan_connected(output: str) -> bool:
    """netsh wlan show interfaces 输出是否存在「真实连接」的无线接口。

    按接口块解析（名称/状态/SSID）：必须同时满足 状态=已连接 且 SSID 非空。
    Wi-Fi Direct 虚拟接口（本地连接* N，移动热点/Miracast）可能显示
    「已连接」但 SSID 为空，不能据此认为当前是无线联网
    （曾导致纯有线机器的链路检测在虚拟接口抖动时误判为 wireless）。
    """
    if not output:
        return False
    state_connected = False
    ssid = ''
    for line in output.split('\n'):
        s = line.strip()
        if s.startswith('接口名称') or s.lower().startswith('name'):
            continue
        if (s.startswith('状态') or s.lower().startswith('state')) and ':' in s:
            value = s.split(':', 1)[1].strip().lower()
            state_connected = value in ('已连接', 'connected')
        elif s.startswith('SSID') and ':' in s and not s.upper().startswith('BSSID'):
            value = s.split(':', 1)[1].strip()
            if value:
                ssid = value
    return state_connected and bool(ssid)


def detect_link_type() -> str:
    """检测当前联网方式，返回 'wireless' 或 'wired'（优先无线的简单启发式）。

    供 Portal 节点在 link_mode=auto 时选用有线/无线配置变体。判定顺序：
    1. WLAN 接口已连接 → 'wireless'；
    2. 否则存在 Up 的有线(802.3)物理适配器 → 'wired'；
    3. 都判不出 → 'wireless'（兜底，与应用整体以 WLAN 为主一致）。

    有线探测复用 _list_physical_adapters（`Get-NetAdapter -Physical`，
    Status 作为属性输出）。不能用 `Get-NetAdapter -Status Up` 的命令形式：
    部分 Windows 构建不接受该参数（报"找不到与参数名称 Status 匹配的
    参数"），探测必败会兜底成 wireless——没有无线接口的有线机器因此被
    判成无线，Portal 自动检测全部失效。
    """
    _, wlan_output, _ = run_command('netsh wlan show interfaces', timeout=3,
                                    cancellable=False)
    if _wlan_connected(wlan_output):
        return 'wireless'
    for _attempt in range(2):  # 失败重试一次：偶发超时/取消不应误判
        adapters = _list_physical_adapters(_MEDIA_WIRED)
        if adapters and any(status.lower() == 'up' for _, status in adapters):
            return 'wired'
        if adapters:
            break  # 命令成功但无 Up 的有线网卡 → 真无线
    return 'wireless'


def _wlan_name_from_output(output: str) -> str | None:
    """从 netsh wlan show interfaces 输出解析 WLAN 接口名（已连接才有效）。"""
    for line in (output or '').split('\n'):
        s = line.strip()
        if (s.startswith('名称') or s.lower().startswith('name')) and ':' in s:
            name = s.split(':', 1)[1].strip()
            if name:
                return name
    return None


def resolve_active_interface() -> tuple[str, str]:
    """返回 (link_type, interface_name)：当前联网方式 + 应操作的网卡名。

    与 detect_link_type 同一套判定，但复用一次 netsh 输出直接取 WLAN
    接口名，避免探测方（状态轮询、get_network_detail）再各跑一遍命令：

    1. WLAN 已连接 → ('wireless', WLAN 接口名)；
    2. 否则存在 Up 的有线网卡 → ('wired', 有线网卡名)；
    3. 兜底 → ('wireless', WLAN 接口名或 'WLAN')。
    """
    _, wlan_output, _ = run_command('netsh wlan show interfaces', timeout=3,
                                    cancellable=False)
    if _wlan_connected(wlan_output):
        return 'wireless', _wlan_name_from_output(wlan_output) or 'WLAN'
    wired = get_wired_interface_name()
    if wired:
        return 'wired', wired
    # WLAN 未连接且没找到有线网卡：若系统根本没有可用无线接口，
    # 只能按有线兜底（纯有线机器上 WLAN 被禁用时 netsh 拿不到任何接口）
    if not get_wifi_interface_name():
        return 'wired', ''
    return 'wireless', get_wifi_interface_name() or 'WLAN'


# 物理网卡媒体类型：无线 802.11，有线 Native 802.3
_MEDIA_WIRELESS = '802\\.11'
_MEDIA_WIRED = '802\\.3'


def _list_physical_adapters(media_pattern: str) -> list[tuple[str, str]]:
    """列出媒体类型匹配的物理网卡，返回 [(名称, 状态)] 列表。

    状态为 Get-NetAdapter 的 Status（Up / Disconnected / Disabled 等）。
    """
    ps = ("Get-NetAdapter -Physical | Where-Object { $_.PhysicalMediaType "
          f"-match '{media_pattern}' }} | ForEach-Object {{ \"$($_.Name)|$($_.Status)\" }}")
    code, output, _ = run_command(
        ['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps],
        shell=False, timeout=8, cancellable=False)
    if code != 0:
        return []
    adapters = []
    for line in (output or '').split('\n'):
        line = line.strip()
        if '|' not in line:
            continue
        name, _, status = line.rpartition('|')
        name, status = name.strip(), status.strip()
        if name:
            adapters.append((name, status))
    return adapters


def get_wired_interface_name():
    """获取有线(802.3)物理网卡名称，Up 状态优先；找不到返回 None。"""
    adapters = _list_physical_adapters(_MEDIA_WIRED)
    for name, status in adapters:
        if status.lower() == 'up':
            return name
    return adapters[0][0] if adapters else None


def _run_ps_adapter(cmdlet: str, name: str, timeout: float = 15.0) -> bool:
    ps = f'{cmdlet} -Name "{name}" -Confirm:$false'
    code, _, _ = run_command(
        ['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps],
        shell=False, timeout=timeout)
    return code == 0


def apply_link_exclusive(link: str) -> tuple[bool, str]:
    """链路隔离：只保留 link 类型网卡工作，禁用另一类型的全部物理网卡。

    用户在 Portal 节点显式指定 wired/wireless 时调用（auto 不动网卡，保持兼容）。
    先启用 link 类型的所有网卡（可能被上一轮隔离禁用，保证来回切换可恢复），
    再禁用另一类型。操作均幂等。

    Returns:
        (成功, 消息)。link 类型一张物理网卡都没有时返回失败。
    """
    if link not in ('wired', 'wireless'):
        return True, ''
    target_pattern = _MEDIA_WIRED if link == 'wired' else _MEDIA_WIRELESS
    other_pattern = _MEDIA_WIRELESS if link == 'wired' else _MEDIA_WIRED
    target = _list_physical_adapters(target_pattern)
    if not target:
        label = '有线' if link == 'wired' else '无线'
        return False, f'未找到{label}网卡，无法按指定连接方式工作'
    others = _list_physical_adapters(other_pattern)
    enabled, disabled = [], []
    for name, _ in target:
        if _run_ps_adapter('Enable-NetAdapter', name):
            enabled.append(name)
    for name, _ in others:
        if _run_ps_adapter('Disable-NetAdapter', name):
            disabled.append(name)
    msg = (f'链路隔离：已启用 {enabled or "无"}，已禁用 {disabled or "无"}')
    logger.info('apply_link_exclusive(%s): %s', link, msg)
    return True, msg


def _is_virtual_adapter_ip(ip: str) -> bool:
    """判断是否虚拟适配器/未连接时才出现的地址。

    - 192.168.137.x：Windows 移动热点 / ICS 共享的默认网段（"本地连接* 1" 等
      Wi-Fi Direct 虚拟网卡），不是校园网分配的真实地址
    - 169.254.x：APIPA 自动配置地址，表示未获取到 DHCP
    """
    return ip.startswith('192.168.137.') or ip.startswith('169.254.')


def get_adapter_ipv4(interface_name):
    """解析指定网卡的 IPv4 地址（WARP 虚拟网段 / 热点共享 / APIPA 地址除外）。

    与 get_local_ip 的 socket 回退不同：只认指定网卡的 ipconfig 段，不依赖
    默认路由——WARP 全隧道在线时 socket 会落进隧道取到 WARP 的 172.16.x，
    物理网卡的真实校园网 IPv4（172.21.x 等）反而拿不到。
    """
    if not interface_name:
        return ''
    code, output, _ = run_command('ipconfig', timeout=4)
    if code != 0:
        return ''
    in_section = False
    for line in output.split('\n'):
        stripped = line.strip()
        if 'adapter' in stripped.lower() or '适配器' in stripped:
            # 段标题按结尾精确匹配，避免「以太网」误命中「以太网适配器 以太网 2:」段
            in_section = stripped.rstrip(':').endswith(str(interface_name))
            continue
        if not in_section:
            continue
        if ('IPv4' in stripped or 'IPv4 地址' in stripped) and ':' in stripped:
            ip = stripped.split(':', 1)[1].strip()
            if ip and not ip.startswith('172.16.') and not _is_virtual_adapter_ip(ip):
                return ip
    return ''


def get_local_ip():
    # 优先解析当前活动物理网卡的 IPv4：有线联网时不再依赖 WiFi 段和 socket
    # 回退（WARP 全隧道在线时 socket 会落进隧道，物理网卡地址取不到）
    try:
        _, active_iface = resolve_active_interface()
        ip = get_adapter_ipv4(active_iface)
        if ip:
            return ip
    except Exception:
        pass
    wifi_name = get_wifi_interface_name()
    if wifi_name:
        code, output, _ = run_command('ipconfig', timeout=4)
        lines = output.split('\n')
        found_wifi = False
        for line in lines:
            line_stripped = line.strip()
            # 遇到新的适配器标题时才重置 found_wifi（不在空行时重置，
            # 因为适配器标题行后常紧跟空行，会导致 WLAN 部分的 IPv4 被跳过）。
            # 注意：只认"标题里含 WLAN 网卡名"——不能用 'Wireless'/'无线' 宽匹配，
            # 否则会误匹配 Wi-Fi Direct 虚拟网卡（"本地连接* 1"，开了移动热点后
            # IP 是 192.168.137.1），导致取到共享地址而非真实校园网 IP。
            if 'adapter' in line_stripped.lower() or '适配器' in line_stripped:
                found_wifi = wifi_name in line_stripped
                continue
            if found_wifi and ('IPv4' in line_stripped or 'IPv4 地址' in line_stripped) and ':' in line_stripped:
                ip = line_stripped.split(':', 1)[1].strip()
                if ip and not _is_virtual_adapter_ip(ip) and not ip.startswith('172.16.'):
                    return ip
                continue
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith('172.16.') or _is_virtual_adapter_ip(ip):
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


def get_wifi_radio_state() -> tuple[str, str]:
    """读取 WLAN 无线电状态，返回 (硬件状态, 软件状态)。

    取值 'on' / 'off' / 'unknown'。netsh 在中文系统输出
    "无线电状态  硬件开/软件关"，英文系统输出 "Radio state  Hardware On/Software Off"。
    网卡被禁用或不存在时 netsh 报"系统上没有无线接口"，返回 ('unknown', 'unknown')。
    """
    code, output, _ = run_command('netsh wlan show interfaces', timeout=5,
                                  cancellable=False)
    if code != 0:
        return 'unknown', 'unknown'
    for line in output.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if '无线电状态' not in stripped and not low.startswith('radio state') \
                and not low.startswith('radio status'):
            continue
        if '无线电状态' in stripped:
            rest = stripped.replace('无线电状态', '').strip()
        else:
            rest = stripped.split(':', 1)[1].strip() if ':' in stripped \
                else stripped.split(None, 2)[-1].strip()
        hw, sw = 'unknown', 'unknown'
        for part in re.split(r'[/／]', rest):
            p = part.strip().lower()
            if '硬件' in p or 'hardware' in p:
                hw = 'off' if ('关' in p or 'off' in p) else 'on'
            elif '软件' in p or 'software' in p:
                sw = 'off' if ('关' in p or 'off' in p) else 'on'
        return hw, sw
    return 'unknown', 'unknown'


_RADIO_PS1_ERRORS = {
    'NO_RADIO': '未找到 WLAN 无线电设备',
    'TIMEOUT': '无线电操作超时',
    'DENIED_BY_USER': '系统拒绝控制无线电（Windows 隐私设置中禁用了无线设备控制）',
    'DENIED_BY_SYSTEM': '系统策略拒绝控制无线电',
}


def _enable_wifi_radio_via_winrt(timeout: float = 20.0) -> tuple[bool, str]:
    """用 WinRT Radio API 打开 WLAN 软件无线电（飞行模式/软开关场景）。

    脚本输出 ASCII 状态码（OK:xx / ERR:xx），中文提示由本函数映射，
    避免 PowerShell 5.1 无 BOM 文件的中文编码问题。
    """
    script = '''$ErrorActionPreference = 'Stop'
try {
  [Windows.Devices.Radios.Radio,Windows.Runtime,ContentType=WindowsRuntime] | Out-Null
  [Windows.Devices.Radios.RadioState,Windows.Runtime,ContentType=WindowsRuntime] | Out-Null
  $op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
  $deadline = (Get-Date).AddSeconds(6)
  while ($op.Status -eq 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }
  if ($op.Status -ne 1) { Write-Output 'ERR:TIMEOUT'; exit 0 }
  $wifi = @($op.GetResults() | Where-Object { $_.Kind -eq 'WiFi' }) | Select-Object -First 1
  if (-not $wifi) { Write-Output 'ERR:NO_RADIO'; exit 0 }
  if ($wifi.State -eq 'On') { Write-Output 'OK:ALREADY'; exit 0 }
  $op2 = $wifi.SetStateAsync([Windows.Devices.Radios.RadioState]::On)
  $deadline2 = (Get-Date).AddSeconds(6)
  while ($op2.Status -eq 0 -and (Get-Date) -lt $deadline2) { Start-Sleep -Milliseconds 50 }
  if ($op2.Status -ne 1) { Write-Output 'ERR:TIMEOUT'; exit 0 }
  $res = $op2.GetResults().ToString()
  if ($res -eq 'Allowed') { Write-Output 'OK:ON' }
  elseif ($res -eq 'DeniedByUser') { Write-Output 'ERR:DENIED_BY_USER' }
  elseif ($res -eq 'DeniedBySystem') { Write-Output 'ERR:DENIED_BY_SYSTEM' }
  else { Write-Output "ERR:FAILED_$res" }
} catch {
  Write-Output 'ERR:EXCEPTION'
}
'''
    fd, path = tempfile.mkstemp(prefix='campusauth_radio_', suffix='.ps1')
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as f:
            f.write(script)
        code, output, err = run_command(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path],
            shell=False, timeout=timeout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    text = (output or err or '').strip()
    if code != 0:
        return False, f'无线电开启脚本执行失败（返回码 {code}）'
    if text.startswith('OK:'):
        return True, 'WiFi 无线电已开启'
    token = text.split(':', 1)[-1].strip() if ':' in text else ''
    if token.startswith('FAILED_'):
        return False, f'开启 WiFi 无线电失败（{token[len("FAILED_"):]}）'
    if token.startswith('EXCEPTION'):
        return False, '开启 WiFi 无线电失败（WinRT 调用异常）'
    return False, _RADIO_PS1_ERRORS.get(token, f'开启 WiFi 无线电失败：{text[:80]}')


def prepare_wifi_connection(timeout: float = 20.0) -> tuple[bool, str]:
    """连接 WiFi 前的准备：确保无线网卡已启用、软件无线电已打开。

    处理两类"无线没开"的状态（都是自动连接失败的常见原因）：
    - 网卡被禁用（上一轮链路隔离/设备管理器）→ Enable-NetAdapter 启用全部
      无线物理网卡，等接口出现在 netsh 里；
    - 软件无线电关闭（飞行模式/软开关）→ WinRT Radio API 自动开启；
    - 硬件开关关闭 → 无法程序化处理，返回明确提示让用户手动开。
    返回 (ok, message)。
    """
    interface = get_wifi_interface_name()
    if not interface:
        adapters = _list_physical_adapters(_MEDIA_WIRELESS)
        if not adapters:
            return False, '本机没有无线网卡，无法连接 WiFi'
        enabled = [name for name, _ in adapters if _run_ps_adapter('Enable-NetAdapter', name)]
        logger.info('prepare_wifi_connection: 无线网卡被禁用，已启用 %s', enabled or '无')
        # Enable-NetAdapter 后接口注册到 WLAN 服务需要一点时间
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(1.0)
            interface = get_wifi_interface_name()
            if interface:
                break
        if not interface:
            return False, '无线网卡已启用但接口未就绪，请稍后重试'
    hw, sw = get_wifi_radio_state()
    if hw == 'off':
        return False, 'WiFi 无线电硬件开关处于关闭状态（Fn 键或物理开关），无法自动开启'
    if sw == 'off':
        ok, msg = _enable_wifi_radio_via_winrt(timeout=timeout)
        if not ok:
            return False, msg
        hw2, sw2 = get_wifi_radio_state()
        if sw2 == 'off':
            return False, 'WiFi 无线电开启指令已发出但状态未变化'
        logger.info('prepare_wifi_connection: 软件无线电已从关闭状态开启')
    return True, ''


def connect_wifi(ssid: str, timeout: float = 25) -> tuple[bool, str]:
    """主动连接指定 WiFi，返回 (ok, message)。

    开机自动认证用：无线环境下先连上用户配置的 WiFi 再执行认证。
    netsh wlan connect 走系统已保存的配置文件（profile 名与 SSID 一致），
    从未保存过的网络会直接报错返回。连接请求发出后轮询当前 SSID，
    稳定落到其他网络时提前判失败，避免等满超时。
    """
    ssid = (ssid or '').strip()
    if not ssid:
        return False, '未配置 WiFi 名称'
    ok, msg = prepare_wifi_connection(timeout=timeout)
    if not ok:
        return False, msg
    interface = get_wifi_interface_name()
    if not interface:
        return False, '未找到可用的无线网卡'
    code, output, err = run_command(
        f'netsh wlan connect name="{ssid}" interface="{interface}"',
        timeout=10, cancellable=False)
    if code != 0:
        detail = (output or err or '').strip().splitlines()
        return False, (detail[0].strip() if detail else '连接请求发送失败')
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        current = get_current_wifi_ssid()
        if current == ssid:
            return True, '已连接'
        # 连接尝试开始 8 秒后仍稳定挂在别的网络上 → 本次连接已失败
        if current and time.time() > deadline - timeout + 8:
            return False, f'连接到了其他网络：{current}'
    return False, '连接超时'


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
