"""
网络流量监控模块。
通过分析实际 TCP 连接和路由表，判断每个连接的真实走向：
  IPv4 直连、IPv6 直连、IPv4+WARP、IPv6+WARP
不根据配置推断，而是通过 Get-NetTCPConnection + Get-NetRoute + Get-NetAdapter 分析。
"""
import subprocess
import json
import socket
import ipaddress
import logging

logger = logging.getLogger('traffic_monitor')

# 需要过滤的本地/回环地址
_LOCAL_ADDRS = {'::', '0.0.0.0', '::1', '127.0.0.1', 'localhost'}


def _run_ps(cmd, timeout=15):
    """执行 PowerShell 命令，返回 (exit_code, stdout, stderr)"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    try:
        result = subprocess.run(
            ['powershell', '-Command', cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.returncode, result.stdout or '', result.stderr or ''
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'
    except Exception as e:
        return -1, '', str(e)


def _get_network_snapshot():
    """一次性获取网络快照：WARP 接口索引 + TCP 连接列表 + 路由表 + 进程名映射"""
    ps_script = r'''
    $ErrorActionPreference = 'SilentlyContinue'
    $warp = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like "*Cloudflare*" -or $_.Name -like "*WARP*"} | Select-Object -First 1
    $warpIfIndex = if ($warp) { [int]$warp.ifIndex } else { -1 }

    $conns = Get-NetTCPConnection -State Established
    $procTable = @{}
    Get-Process | ForEach-Object { $procTable[$_.Id] = $_.ProcessName }

    $connList = @()
    foreach ($c in $conns) {
        $pid = [int]$c.OwningProcess
        $connList += [PSCustomObject]@{
            RemoteAddress = [string]$c.RemoteAddress
            RemotePort = [int]$c.RemotePort
            ProcessId = $pid
            ProcessName = if ($procTable.ContainsKey($pid)) { $procTable[$pid] } else { 'unknown' }
            InterfaceIndex = if ($c.InterfaceIndex) { [int]$c.InterfaceIndex } else { -1 }
        }
    }

    $routeList = @()
    Get-NetRoute | ForEach-Object {
        $routeList += [PSCustomObject]@{
            ifIndex = [int]$_.ifIndex
            DestinationPrefix = [string]$_.DestinationPrefix
        }
    }

    [PSCustomObject]@{
        WarpIfIndex = $warpIfIndex
        Connections = $connList
        Routes = $routeList
    } | ConvertTo-Json -Depth 3 -Compress
    '''
    code, output, _ = _run_ps(ps_script, timeout=15)
    if code != 0 or not output.strip():
        logger.error(f'get_network_snapshot failed: code={code}')
        return {'WarpIfIndex': -1, 'Connections': [], 'Routes': []}
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        logger.error(f'Failed to parse snapshot JSON: {e}')
        return {'WarpIfIndex': -1, 'Connections': [], 'Routes': []}


def _ip_in_network(ip_str, network_str, prefix_len):
    """判断 IP 是否在指定网段内"""
    try:
        network = ipaddress.ip_network(f'{network_str}/{prefix_len}', strict=False)
        ip = ipaddress.ip_address(ip_str)
        return ip in network
    except (ValueError, TypeError):
        return False


def _match_route_ifindex(ip_str, routes):
    """最长前缀匹配，返回匹配路由的 ifIndex"""
    is_ipv6 = ':' in ip_str
    best_ifindex = None
    best_prefix_len = -1
    for route in routes:
        prefix = route.get('DestinationPrefix', '')
        if not prefix or '/' not in prefix:
            continue
        network_part, len_part = prefix.split('/')
        try:
            prefix_len = int(len_part)
        except ValueError:
            continue
        if prefix_len <= best_prefix_len:
            continue
        # IPv4/IPv6 地址族必须匹配
        if (':' in network_part) != is_ipv6:
            continue
        if _ip_in_network(ip_str, network_part, prefix_len):
            best_prefix_len = prefix_len
            best_ifindex = route.get('ifIndex')
    return best_ifindex


# 反向 DNS 缓存，避免重复解析
_dns_cache = {}
_dns_cache_lock = None


def _reverse_dns(ip_str, timeout=0.8):
    """反向 DNS 解析（带超时和缓存），返回域名或空字符串"""
    global _dns_cache_lock
    if _dns_cache_lock is None:
        import threading
        _dns_cache_lock = threading.Lock()

    with _dns_cache_lock:
        if ip_str in _dns_cache:
            return _dns_cache[ip_str]

    result = ''
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        hostname, _, _ = socket.gethostbyaddr(ip_str)
        result = hostname
    except (socket.herror, socket.gaierror, socket.timeout, Exception):
        pass
    finally:
        socket.setdefaulttimeout(old_timeout)

    with _dns_cache_lock:
        _dns_cache[ip_str] = result
    return result


def get_traffic_status():
    """获取当前流量走向统计和连接详情。
    返回 {
        'stats': {'ipv4_direct': N, 'ipv6_direct': N, 'ipv4_warp': N, 'ipv6_warp': N},
        'connections': [{process, remote_ip, remote_port, route_type, hostname}],
        'warp_ifindex': N,
        'total': N
    }
    """
    snapshot = _get_network_snapshot()
    warp_ifindex = snapshot.get('WarpIfIndex', -1)
    connections = snapshot.get('Connections', [])
    routes = snapshot.get('Routes', [])

    stats = {'ipv4_direct': 0, 'ipv6_direct': 0, 'ipv4_warp': 0, 'ipv6_warp': 0}
    conn_details = []

    for conn in connections:
        remote_ip = conn.get('RemoteAddress', '')
        # 过滤本地/回环地址
        if not remote_ip or remote_ip in _LOCAL_ADDRS:
            continue

        is_ipv6 = ':' in remote_ip

        # 判断是否走 WARP：优先用连接的 InterfaceIndex，无则用路由表匹配
        conn_ifindex = conn.get('InterfaceIndex', -1)
        if conn_ifindex and conn_ifindex > 0 and warp_ifindex > 0:
            is_warp = (conn_ifindex == warp_ifindex)
        else:
            matched_ifindex = _match_route_ifindex(remote_ip, routes)
            is_warp = (matched_ifindex == warp_ifindex and warp_ifindex > 0)

        # 分类统计
        if is_ipv6 and is_warp:
            route_type = 'ipv6_warp'
            stats['ipv6_warp'] += 1
        elif is_ipv6 and not is_warp:
            route_type = 'ipv6_direct'
            stats['ipv6_direct'] += 1
        elif not is_ipv6 and is_warp:
            route_type = 'ipv4_warp'
            stats['ipv4_warp'] += 1
        else:
            route_type = 'ipv4_direct'
            stats['ipv4_direct'] += 1

        # 异步反向 DNS（带缓存）
        hostname = _reverse_dns(remote_ip)

        conn_details.append({
            'process': conn.get('ProcessName', 'unknown'),
            'remote_ip': remote_ip,
            'remote_port': conn.get('RemotePort', 0),
            'route_type': route_type,
            'is_warp': is_warp,
            'is_ipv6': is_ipv6,
            'hostname': hostname,
        })

    # 按进程名排序，同进程按 IP 排序
    conn_details.sort(key=lambda x: (x['process'].lower(), x['remote_ip']))

    return {
        'stats': stats,
        'connections': conn_details,
        'warp_ifindex': warp_ifindex,
        'total': len(conn_details),
    }
