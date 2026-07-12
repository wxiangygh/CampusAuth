"""
网络流量监控模块。
通过分析实际 TCP 连接和路由表，判断每个连接的真实走向。

6 种分类（概念定义）：
  - ipv4            : 直接 IPv4，不走 WARP 隧道
  - ipv6            : 直接 IPv6，不走 WARP 隧道
  - ipv4_warp       : WARP 隧道基于 IPv4 + 内部传输 IPv4
  - ipv4_warp_ipv6  : WARP 隧道基于 IPv4 + 内部传输 IPv6
  - ipv6_warp       : WARP 隧道基于 IPv6 + 内部传输 IPv6
  - ipv6_warp_ipv4  : WARP 隧道基于 IPv6 + 内部传输 IPv4

WARP 底层连接类型判断：检查 warp-svc 进程的 UDP 端点绑定类型。
  WARP 使用 MASQUE/QUIC (UDP) 连接 Cloudflare 服务器，
  如果 UDP 端点绑定到 IPv6 地址（如 :::port），说明底层用 IPv6；
  绑定到 IPv4 地址（如 0.0.0.0:port），说明底层用 IPv4。
  排除本地回环 DNS 代理（127.0.2.x:53）。
"""
import subprocess
import json
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.command import run_powershell_simple as _run_ps

logger = logging.getLogger('traffic_monitor')

# 需要过滤的本地/回环地址
_LOCAL_ADDRS = {'::', '0.0.0.0', '::1', '127.0.0.1', 'localhost'}

# 6 种分类的显示标签和颜色（供前端使用）
# 标签格式：WARP[v4]→v4 表示 WARP 底层为 IPv4，内部传输 IPv4
ROUTE_TYPES = {
    'ipv4':            {'label': 'IPv4 直连',         'color': '#3b82f6'},
    'ipv6':            {'label': 'IPv6 直连',         'color': '#22C55E'},
    'ipv4_warp':       {'label': 'WARP[v4]→v4',       'color': '#F59E0B'},
    'ipv4_warp_ipv6':  {'label': 'WARP[v4]→v6',       'color': '#EAB308'},
    'ipv6_warp':       {'label': 'WARP[v6]→v6',       'color': '#EF4444'},
    'ipv6_warp_ipv4':  {'label': 'WARP[v6]→v4',       'color': '#A855F7'},
}


def _get_network_snapshot():
    """一次性获取网络快照：WARP 接口索引 + TCP 连接列表 + 路由表 + 进程名映射 + WARP 底层连接类型
    过滤掉 powershell、conhost、CampusAuth 自身等辅助进程的连接"""
    import os
    my_pid = os.getpid()
    ps_script = r'''
    $ErrorActionPreference = 'SilentlyContinue'

    # 1. 获取 WARP 虚拟网卡
    $warp = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like "*Cloudflare*" -or $_.Name -like "*WARP*"} | Select-Object -First 1
    $warpIfIndex = if ($warp) { [int]$warp.ifIndex } else { -1 }

    # 2. 获取 TCP 连接和进程名映射
    $conns = Get-NetTCPConnection -State Established
    $procTable = @{}
    Get-Process | ForEach-Object { $procTable[$_.Id] = $_.ProcessName }

    # 需要过滤的进程名（辅助/系统进程，不是用户实际发起的网络请求）
    $filterNames = @('powershell','conhost','wsmprovhost','WmiPrvSE','svchost','lsass','services','wininit','smss','csrss','dwm','RuntimeBroker','SearchHost','StartMenuExperienceHost','TextInputHost','ShellExperienceHost')

    $connList = @()
    foreach ($c in $conns) {
        $procId = [int]$c.OwningProcess
        $pname = if ($procTable.ContainsKey($procId)) { $procTable[$procId] } else { 'unknown' }
        # 过滤辅助进程
        if ($filterNames -contains $pname) { continue }
        $connList += [PSCustomObject]@{
            RemoteAddress = [string]$c.RemoteAddress
            RemotePort = [int]$c.RemotePort
            ProcessId = $procId
            ProcessName = $pname
            InterfaceIndex = if ($c.InterfaceIndex) { [int]$c.InterfaceIndex } else { -1 }
        }
    }

    # 3. 获取路由表（只获取默认路由和 WARP 接口路由，避免获取全部路由表很慢）
    $routeList = @()
    Get-NetRoute -PolicyStore ActiveStore | Where-Object {
        $_.DestinationPrefix -eq '0.0.0.0/0' -or
        $_.DestinationPrefix -eq '::/0' -or
        $_.ifIndex -eq $warpIfIndex
    } | ForEach-Object {
        $routeList += [PSCustomObject]@{
            ifIndex = [int]$_.ifIndex
            DestinationPrefix = [string]$_.DestinationPrefix
        }
    }

    # 3.5 获取系统 DNS 缓存，构建 IP→域名 反向映射
    # 这样可以把连接的远程 IP 反查为域名，速度快（系统已解析过）
    $dnsMap = @{}
    Get-DnsClientCache -ErrorAction SilentlyContinue | ForEach-Object {
        $entryName = [string]$_.Entry
        $entryData = [string]$_.Data
        if ($entryData -and $entryData -ne '') {
            # Data 可能是 IP 地址（A/AAAA 记录）或别名（CNAME）
            # 只处理看起来像 IP 的 Data
            if ($entryData -match '^\d+\.\d+\.\d+\.\d+$' -or $entryData -match ':') {
                if (-not $dnsMap.ContainsKey($entryData)) {
                    $dnsMap[$entryData] = $entryName
                }
            }
        }
    }

    # 4. 判断 WARP 底层连接类型（IPv4 或 IPv6）
    # 最准确方法：检查 warp-svc 进程的 UDP 端点绑定类型
    # WARP 使用 MASQUE/QUIC (UDP) 连接 Cloudflare 服务器，
    # 如果 UDP 端点绑定到 IPv6 地址（如 :::port），说明底层用 IPv6
    # 排除本地回环 DNS 代理（127.0.2.x:53）
    $warpUnderlay = 'ipv4'
    $underlayDebug = @()
    if ($warp -and $warpIfIndex -gt 0) {
        # 查找 WARP 服务进程
        $warpSvcProcs = Get-Process | Where-Object {
            $_.ProcessName -like "*warp*" -or $_.ProcessName -like "*cloudflare*"
        }
        $underlayDebug += "WARP processes: $($warpSvcProcs.Count)"

        if ($warpSvcProcs) {
            $warpProcIds = $warpSvcProcs | Select-Object -ExpandProperty Id
            # 获取 WARP 进程的所有 UDP 端点
            $warpUdp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object {
                $warpProcIds -contains $_.OwningProcess
            }
            $underlayDebug += "WARP UDP endpoints: $($warpUdp.Count)"

            foreach ($ep in $warpUdp) {
                $localAddr = [string]$ep.LocalAddress
                $localPort = [int]$ep.LocalPort
                $underlayDebug += "  UDP $localAddr`:$localPort"

                # 排除本地回环地址（127.x.x.x, ::1）- 这些是 DNS 代理，不是到 Cloudflare 的连接
                if ($localAddr -like "127.*" -or $localAddr -eq "::1") { continue }

                # 非回环 UDP 端点：判断 IPv4 还是 IPv6
                if ($localAddr -like "*:*") {
                    # IPv6 地址（包括 :: 通配地址）
                    $warpUnderlay = 'ipv6'
                    $underlayDebug += "    => IPv6 (non-loopback)"
                    break
                } else {
                    # IPv4 地址（包括 0.0.0.0 通配地址）
                    $warpUnderlay = 'ipv4'
                    $underlayDebug += "    => IPv4 (non-loopback)"
                    break
                }
            }
        }
        $underlayDebug += "WARP underlay => $warpUnderlay"
    }

    [PSCustomObject]@{
        WarpIfIndex = $warpIfIndex
        WarpUnderlay = $warpUnderlay
        UnderlayDebug = ($underlayDebug -join "`n")
        Connections = $connList
        Routes = $routeList
        DnsMap = $dnsMap
    } | ConvertTo-Json -Depth 3 -Compress
    '''
    code, output, _ = _run_ps(ps_script, timeout=15)
    if code != 0 or not output.strip():
        logger.error(f'get_network_snapshot failed: code={code}')
        return {'WarpIfIndex': -1, 'WarpUnderlay': 'ipv4', 'Connections': [], 'Routes': [], 'UnderlayDebug': '', 'DnsMap': {}}
    try:
        result = json.loads(output)
        # 记录底层类型判断的调试信息
        debug_info = result.get('UnderlayDebug', '')
        if debug_info:
            logger.info(f'WARP underlay detection:\n{debug_info}')
        return result
    except json.JSONDecodeError as e:
        logger.error(f'Failed to parse snapshot JSON: {e}')
        return {'WarpIfIndex': -1, 'WarpUnderlay': 'ipv4', 'Connections': [], 'Routes': [], 'UnderlayDebug': '', 'DnsMap': {}}


def _get_network_snapshot_fast():
    """快速获取网络快照（不含 DNS 缓存），用于首屏快速展示。

    与 _get_network_snapshot 的区别：移除 Get-DnsClientCache 调用，
    返回的 DnsMap 为空字典。目标耗时 <2 秒。
    """
    import os
    my_pid = os.getpid()
    ps_script = r'''
    $ErrorActionPreference = 'SilentlyContinue'

    # 1. 获取 WARP 虚拟网卡
    $warp = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like "*Cloudflare*" -or $_.Name -like "*WARP*"} | Select-Object -First 1
    $warpIfIndex = if ($warp) { [int]$warp.ifIndex } else { -1 }

    # 2. 获取 TCP 连接和进程名映射
    $conns = Get-NetTCPConnection -State Established
    $procTable = @{}
    Get-Process | ForEach-Object { $procTable[$_.Id] = $_.ProcessName }

    # 需要过滤的进程名（辅助/系统进程，不是用户实际发起的网络请求）
    $filterNames = @('powershell','conhost','wsmprovhost','WmiPrvSE','svchost','lsass','services','wininit','smss','csrss','dwm','RuntimeBroker','SearchHost','StartMenuExperienceHost','TextInputHost','ShellExperienceHost')

    $connList = @()
    foreach ($c in $conns) {
        $procId = [int]$c.OwningProcess
        $pname = if ($procTable.ContainsKey($procId)) { $procTable[$procId] } else { 'unknown' }
        if ($filterNames -contains $pname) { continue }
        $connList += [PSCustomObject]@{
            RemoteAddress = [string]$c.RemoteAddress
            RemotePort = [int]$c.RemotePort
            ProcessId = $procId
            ProcessName = $pname
            InterfaceIndex = if ($c.InterfaceIndex) { [int]$c.InterfaceIndex } else { -1 }
        }
    }

    # 3. 获取路由表（只获取默认路由和 WARP 接口路由）
    $routeList = @()
    Get-NetRoute -PolicyStore ActiveStore | Where-Object {
        $_.DestinationPrefix -eq '0.0.0.0/0' -or
        $_.DestinationPrefix -eq '::/0' -or
        $_.ifIndex -eq $warpIfIndex
    } | ForEach-Object {
        $routeList += [PSCustomObject]@{
            ifIndex = [int]$_.ifIndex
            DestinationPrefix = [string]$_.DestinationPrefix
        }
    }

    # 4. 判断 WARP 底层连接类型（IPv4 或 IPv6）
    $warpUnderlay = 'ipv4'
    $underlayDebug = @()
    if ($warp -and $warpIfIndex -gt 0) {
        $warpSvcProcs = Get-Process | Where-Object {
            $_.ProcessName -like "*warp*" -or $_.ProcessName -like "*cloudflare*"
        }
        $underlayDebug += "WARP processes: $($warpSvcProcs.Count)"

        if ($warpSvcProcs) {
            $warpProcIds = $warpSvcProcs | Select-Object -ExpandProperty Id
            $warpUdp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object {
                $warpProcIds -contains $_.OwningProcess
            }
            $underlayDebug += "WARP UDP endpoints: $($warpUdp.Count)"

            foreach ($ep in $warpUdp) {
                $localAddr = [string]$ep.LocalAddress
                $localPort = [int]$ep.LocalPort
                $underlayDebug += "  UDP $localAddr`:$localPort"

                if ($localAddr -like "127.*" -or $localAddr -eq "::1") { continue }

                if ($localAddr -like "*:*") {
                    $warpUnderlay = 'ipv6'
                    $underlayDebug += "    => IPv6 (non-loopback)"
                    break
                } else {
                    $warpUnderlay = 'ipv4'
                    $underlayDebug += "    => IPv4 (non-loopback)"
                    break
                }
            }
        }
        $underlayDebug += "WARP underlay => $warpUnderlay"
    }

    [PSCustomObject]@{
        WarpIfIndex = $warpIfIndex
        WarpUnderlay = $warpUnderlay
        UnderlayDebug = ($underlayDebug -join "`n")
        Connections = $connList
        Routes = $routeList
        DnsMap = @{}
    } | ConvertTo-Json -Depth 3 -Compress
    '''
    code, output, _ = _run_ps(ps_script, timeout=10)
    if code != 0 or not output.strip():
        logger.error(f'get_network_snapshot_fast failed: code={code}')
        return {'WarpIfIndex': -1, 'WarpUnderlay': 'ipv4', 'Connections': [], 'Routes': [], 'UnderlayDebug': '', 'DnsMap': {}}
    try:
        result = json.loads(output)
        debug_info = result.get('UnderlayDebug', '')
        if debug_info:
            logger.info(f'WARP underlay detection (fast):\n{debug_info}')
        return result
    except json.JSONDecodeError as e:
        logger.error(f'Failed to parse fast snapshot JSON: {e}')
        return {'WarpIfIndex': -1, 'WarpUnderlay': 'ipv4', 'Connections': [], 'Routes': [], 'UnderlayDebug': '', 'DnsMap': {}}


def _get_dns_cache_only():
    """仅获取系统 DNS 缓存，返回 IP→域名映射。

    执行独立的 PowerShell 脚本，仅调用 Get-DnsClientCache。
    用于 get_traffic_status_slow 的第一步。

    Returns:
        dict[str, str]: {ip: hostname}（仅包含缓存命中的）
    """
    ps_script = r'''
    $ErrorActionPreference = 'SilentlyContinue'
    $dnsMap = @{}
    Get-DnsClientCache -ErrorAction SilentlyContinue | ForEach-Object {
        $entryName = [string]$_.Entry
        $entryData = [string]$_.Data
        if ($entryData -and $entryData -ne '') {
            if ($entryData -match '^\d+\.\d+\.\d+\.\d+$' -or $entryData -match ':') {
                if (-not $dnsMap.ContainsKey($entryData)) {
                    $dnsMap[$entryData] = $entryName
                }
            }
        }
    }
    $dnsMap | ConvertTo-Json -Compress
    '''
    code, output, _ = _run_ps(ps_script, timeout=8)
    if code != 0 or not output.strip():
        logger.warning(f'get_dns_cache_only failed: code={code}')
        return {}
    try:
        # PowerShell 空字典会输出 "" 或 "{}"，需处理
        result = json.loads(output) if output.strip() != '{}' else {}
        if isinstance(result, dict):
            return result
        # PowerShell 单条记录可能返回非 dict 格式，兜底处理
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f'Failed to parse DNS cache JSON: {e}')
        return {}


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


def _reverse_dns(ip_str, timeout=1.0):
    """对单个 IP 做反向 DNS 查询，带超时保护。
    返回域名或空字符串（失败时）。"""
    try:
        # socket.setdefaulttimeout 只影响后续 socket 操作
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            hostname, _, _ = socket.gethostbyaddr(ip_str)
            return hostname if hostname else ''
        finally:
            socket.setdefaulttimeout(old_timeout)
    except (socket.herror, socket.gaierror, socket.timeout, OSError):
        return ''


def _batch_reverse_dns(ip_list, timeout=1.0, max_workers=10):
    """并发批量反向 DNS 查询。
    返回 {ip: hostname} 映射（仅包含查询成功的）。
    使用线程池并发，避免串行查询过慢。"""
    result = {}
    if not ip_list:
        return result
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_reverse_dns, ip, timeout): ip for ip in ip_list}
        try:
            for future in as_completed(futures, timeout=timeout + 2):
                ip = futures[future]
                try:
                    hostname = future.result()
                    if hostname:
                        result[ip] = hostname
                except Exception:
                    pass
        except Exception:
            # 部分查询超时（as_completed 抛 TimeoutError），取消剩余任务，返回已完成的
            logger.debug(f'Reverse DNS: {len(result)}/{len(ip_list)} resolved, rest timed out')
            for f in futures:
                f.cancel()
    return result


def get_traffic_status_fast():
    """快速获取流量走向统计和连接详情（不含域名）。

    与 get_traffic_status 的区别：不获取 DNS 缓存，不执行反向 DNS，
    hostname 字段为空字符串。目标耗时 <2 秒。

    Returns:
        dict: 与 get_traffic_status 相同结构，但 connections 中 hostname 为空
    """
    snapshot = _get_network_snapshot_fast()
    warp_ifindex = snapshot.get('WarpIfIndex', -1)
    warp_underlay = snapshot.get('WarpUnderlay', 'ipv4')
    connections = snapshot.get('Connections', [])
    routes = snapshot.get('Routes', [])

    import os
    my_pid = os.getpid()

    stats = {k: 0 for k in ROUTE_TYPES}
    conn_details = []

    for conn in connections:
        if conn.get('ProcessId') == my_pid:
            continue
        remote_ip = conn.get('RemoteAddress', '')
        if not remote_ip or remote_ip in _LOCAL_ADDRS:
            continue

        is_ipv6 = ':' in remote_ip
        conn_ifindex = conn.get('InterfaceIndex', -1)
        if conn_ifindex and conn_ifindex > 0 and warp_ifindex > 0:
            is_warp = (conn_ifindex == warp_ifindex)
        else:
            matched_ifindex = _match_route_ifindex(remote_ip, routes)
            is_warp = (matched_ifindex == warp_ifindex and warp_ifindex > 0)

        if not is_warp:
            route_type = 'ipv6' if is_ipv6 else 'ipv4'
        else:
            if warp_underlay == 'ipv6':
                route_type = 'ipv6_warp_ipv4' if not is_ipv6 else 'ipv6_warp'
            else:
                route_type = 'ipv4_warp' if not is_ipv6 else 'ipv4_warp_ipv6'

        stats[route_type] = stats.get(route_type, 0) + 1

        conn_details.append({
            'process': conn.get('ProcessName', 'unknown'),
            'remote_ip': remote_ip,
            'remote_port': conn.get('RemotePort', 0),
            'hostname': '',  # 快速模式不获取域名，留空由 slow 接口填充
            'route_type': route_type,
            'is_warp': is_warp,
            'is_ipv6': is_ipv6,
            'warp_underlay': warp_underlay,
        })

    conn_details.sort(key=lambda x: (x['process'].lower(), x['remote_ip']))

    return {
        'stats': stats,
        'connections': conn_details,
        'warp_ifindex': warp_ifindex,
        'warp_underlay': warp_underlay,
        'total': len(conn_details),
        'route_types': ROUTE_TYPES,
    }


def get_traffic_status_slow(missing_ips=None):
    """获取 IP→域名映射，供前端增量更新域名显示。

    先查询系统 DNS 缓存（快），对未命中的 IP 再做反向 DNS 后备查询（慢）。

    Args:
        missing_ips: 需要查询的 IP 列表。None 时仅返回 DNS 缓存映射，
                     不触发反向 DNS。传空列表时同样不触发。

    Returns:
        dict[str, str]: {ip: hostname}（仅包含查询成功的）
    """
    # 第一步：获取 DNS 缓存
    dns_map = _get_dns_cache_only()
    if not missing_ips:
        logger.debug(f'get_traffic_status_slow: dns_cache_only={len(dns_map)} entries')
        return dns_map

    # 第二步：过滤出 DNS 缓存未命中的 IP
    ips_to_reverse = [ip for ip in missing_ips if ip not in dns_map]
    if not ips_to_reverse:
        logger.debug(f'get_traffic_status_slow: all {len(missing_ips)} IPs hit DNS cache')
        return dns_map

    # 第三步：反向 DNS 后备查询
    logger.debug(f'get_traffic_status_slow: reverse DNS for {len(ips_to_reverse)}/{len(missing_ips)} IPs')
    rdns_map = _batch_reverse_dns(ips_to_reverse, timeout=1.0, max_workers=10)

    # 合并结果
    result = dict(dns_map)
    result.update(rdns_map)
    logger.debug(f'get_traffic_status_slow: total resolved={len(result)}/{len(missing_ips)}')
    return result


def get_traffic_status():
    """获取当前流量走向统计和连接详情（含域名）。

    内部调用 get_traffic_status_fast + get_traffic_status_slow 串行执行。
    保留此接口用于向后兼容；前端应优先使用 fast/slow 分离接口。

    返回 {
        'stats': {route_type: N, ...},  # 6 种分类的计数
        'connections': [{process, remote_ip, remote_port, hostname, route_type, is_warp, is_ipv6, warp_underlay}],
        'warp_ifindex': N,
        'warp_underlay': 'ipv4' | 'ipv6',
        'total': N
    }
    """
    # 快速获取连接列表（无域名）
    result = get_traffic_status_fast()

    # 慢速获取域名映射
    missing_ips = list({c['remote_ip'] for c in result['connections'] if not c['hostname']})
    if missing_ips:
        dns_map = get_traffic_status_slow(missing_ips)
        for c in result['connections']:
            if not c['hostname'] and c['remote_ip'] in dns_map:
                c['hostname'] = dns_map[c['remote_ip']]

    return result
