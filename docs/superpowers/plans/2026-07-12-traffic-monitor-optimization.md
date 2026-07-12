# 流量监控渐进式加载优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将流量监控/可视化窗口的首屏时间从 ~15 秒降至 <3 秒，通过拆分后端接口为 fast/slow 两部分，前端先加载快数据（无域名）立即渲染，域名异步后加载。

**Architecture:** 后端 `traffic_monitor.py` 拆分出 `get_traffic_status_fast()`（精简 PowerShell，无 DNS 缓存和反向 DNS）和 `get_traffic_status_slow()`（仅获取域名映射）；`ApiBridge` 新增两个对应方法；两个 HTML 前端改为分阶段加载，自动刷新改为 fast-only 并增加防堆积机制。

**Tech Stack:** Python 3.12, PowerShell, pywebview, JavaScript (ES6+)

## Global Constraints

- Logger 名称：`traffic_monitor.py` 使用 `logging.getLogger('traffic_monitor')`，`tray_app.py` 使用 `logging.getLogger('wifi_tray')`
- 代码注释使用中文
- 保留原 `get_traffic_status()` 接口兼容性（内部改为 fast+slow 串行）
- 不修改 WARP 底层连接类型判断逻辑
- 不修改连接分类逻辑（6 种 route_type）
- 不修改窗口创建/管理逻辑
- HTML 前端的 `renderConnections`/`renderConnList` 渲染逻辑保持不变（hostname 为空时显示 IP 或"(无域名)"）

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `traffic_monitor.py` | 流量监控后端 | 新增 `get_traffic_status_fast()`、`get_traffic_status_slow()`、`_get_dns_cache_only()`；重构 `get_traffic_status()` |
| `tray_app.py` | ApiBridge 桥接 | 新增 `get_traffic_status_fast`、`get_traffic_status_slow` 方法；修改导入 |
| `traffic_flow.html` | 流量可视化前端 | `init()` 分阶段、新增 `refreshFast`/`refreshSlow`、自动刷新改 fast-only、防堆积 |
| `traffic_monitor.html` | 流量监控前端 | 同上 |

---

### Task 1: 后端拆分 — 新增 `get_traffic_status_fast()` 和辅助函数

**Files:**
- Modify: `traffic_monitor.py`（在 `get_traffic_status` 函数前插入新函数）

**Interfaces:**
- Consumes: `_run_ps`（已导入）、`_match_route_ifindex`（已有）、`_ip_in_network`（已有）、`ROUTE_TYPES`（已有）、`_LOCAL_ADDRS`（已有）
- Produces: 
  - `get_traffic_status_fast() -> dict`（与原 `get_traffic_status` 相同结构，但 hostname 为空）
  - `_get_dns_cache_only() -> dict[str, str]`（IP→域名映射）

- [ ] **Step 1: 新增 `_get_network_snapshot_fast()` 辅助函数**

在 `traffic_monitor.py` 的 `_get_network_snapshot` 函数之后（约第 179 行），插入新函数：

```python
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
```

- [ ] **Step 2: 新增 `get_traffic_status_fast()` 函数**

在 `traffic_monitor.py` 的 `get_traffic_status` 函数之前（约第 258 行），插入新函数：

```python
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
```

- [ ] **Step 3: 新增 `_get_dns_cache_only()` 辅助函数**

在 `_get_network_snapshot_fast` 之后插入：

```python
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
```

- [ ] **Step 4: 验证新函数可导入**

Run: `python -c "from traffic_monitor import get_traffic_status_fast, _get_dns_cache_only; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 验证 `get_traffic_status_fast` 可调用且耗时 <3 秒**

Run: `python -c "import time; from traffic_monitor import get_traffic_status_fast; t0=time.time(); r=get_traffic_status_fast(); print(f'elapsed={time.time()-t0:.2f}s, total={r[\"total\"]}, all_hostname_empty={all(not c[\"hostname\"] for c in r[\"connections\"])}')"`
Expected: 输出 `elapsed=<3.0s, total=N, all_hostname_empty=True`（total 取决于当前连接数，耗时 <3 秒，所有 hostname 为空）

- [ ] **Step 6: 验证 `_get_dns_cache_only` 可调用**

Run: `python -c "from traffic_monitor import _get_dns_cache_only; d=_get_dns_cache_only(); print(f'entries={len(d)}, type={type(d).__name__}')"`
Expected: 输出 `entries=N, type=dict`（N 取决于当前 DNS 缓存）

- [ ] **Step 7: 提交**

```bash
git add traffic_monitor.py
git commit -m "feat: 新增 get_traffic_status_fast() 和 _get_dns_cache_only() 快速接口"
```

---

### Task 2: 后端拆分 — 新增 `get_traffic_status_slow()` 并重构 `get_traffic_status()`

**Files:**
- Modify: `traffic_monitor.py`（在 `get_traffic_status_fast` 之后新增 `get_traffic_status_slow`；重构 `get_traffic_status`）

**Interfaces:**
- Consumes: `_get_dns_cache_only()`（Task 1 产出）、`_batch_reverse_dns()`（已有）
- Produces: `get_traffic_status_slow(missing_ips: list[str] = None) -> dict[str, str]`

- [ ] **Step 1: 新增 `get_traffic_status_slow()` 函数**

在 `traffic_monitor.py` 的 `get_traffic_status_fast` 函数之后，插入新函数：

```python
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
```

- [ ] **Step 2: 重构 `get_traffic_status()` 使用 fast+slow 串行**

找到 `traffic_monitor.py` 中 `get_traffic_status` 函数（约第 258 行起），将整个函数体替换为：

```python
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
```

注意：原 `get_traffic_status` 函数体从第 258 行到第 350 行（`return {...}` 结束），整体替换。原函数中的 `_get_network_snapshot` 调用、分类逻辑、`_batch_reverse_dns` 调用等全部移除，改为调用 `get_traffic_status_fast` 和 `get_traffic_status_slow`。

- [ ] **Step 3: 验证 `get_traffic_status_slow` 可导入**

Run: `python -c "from traffic_monitor import get_traffic_status_slow; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 验证 `get_traffic_status_slow(None)` 仅返回 DNS 缓存**

Run: `python -c "from traffic_monitor import get_traffic_status_slow; d=get_traffic_status_slow(None); print(f'type={type(d).__name__}, entries={len(d)}')"`
Expected: 输出 `type=dict, entries=N`（N 取决于当前 DNS 缓存）

- [ ] **Step 5: 验证 `get_traffic_status_slow([])` 不触发反向 DNS**

Run: `python -c "from traffic_monitor import get_traffic_status_slow; d=get_traffic_status_slow([]); print(f'type={type(d).__name__}, entries={len(d)}')"`
Expected: 输出 `type=dict, entries=N`（与 Step 4 相同，不触发反向 DNS）

- [ ] **Step 6: 验证重构后的 `get_traffic_status()` 仍工作**

Run: `python -c "from traffic_monitor import get_traffic_status; r=get_traffic_status(); print(f'total={r[\"total\"]}, has_hostname={any(c[\"hostname\"] for c in r[\"connections\"])}')"`
Expected: 输出 `total=N, has_hostname=True/False`（取决于是否有可解析的域名，无报错即可）

- [ ] **Step 7: 提交**

```bash
git add traffic_monitor.py
git commit -m "feat: 新增 get_traffic_status_slow() 并重构 get_traffic_status() 为 fast+slow 串行"
```

---

### Task 3: ApiBridge 新增 fast/slow 方法

**Files:**
- Modify: `tray_app.py:19`（导入行）
- Modify: `tray_app.py:654-666`（ApiBridge 流量监控 API 区域）

**Interfaces:**
- Consumes: `get_traffic_status_fast()`、`get_traffic_status_slow()`（Task 1/2 产出）
- Produces: `ApiBridge.get_traffic_status_fast`、`ApiBridge.get_traffic_status_slow`

- [ ] **Step 1: 修改 `tray_app.py` 导入行**

找到 `tray_app.py` 第 19 行：

```python
from traffic_monitor import get_traffic_status
```

替换为：

```python
from traffic_monitor import get_traffic_status, get_traffic_status_fast, get_traffic_status_slow
```

- [ ] **Step 2: 在 `ApiBridge.get_traffic_status` 之后新增两个方法**

找到 `tray_app.py` 中 `ApiBridge.get_traffic_status` 方法（约第 656-666 行），在其 `close_traffic_window` 方法之前（约第 668 行 `def close_traffic_window` 之前），插入以下两个方法：

```python
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

```

- [ ] **Step 3: 验证 tray_app 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 验证 ApiBridge 新方法存在**

Run: `python -c "import tray_app; assert hasattr(tray_app.ApiBridge, 'get_traffic_status_fast'); assert hasattr(tray_app.ApiBridge, 'get_traffic_status_slow'); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 5: 提交**

```bash
git add tray_app.py
git commit -m "feat: ApiBridge 新增 get_traffic_status_fast/slow 方法"
```

---

### Task 4: traffic_flow.html 前端改造 — 分阶段加载

**Files:**
- Modify: `traffic_flow.html`（`init()`、`refresh()`、`startAuto()` 区域）

**Interfaces:**
- Consumes: `api.get_traffic_status_fast()`、`api.get_traffic_status_slow(missing_ips)`（Task 3 产出）
- Produces: 改造后的前端加载流程

- [ ] **Step 1: 在 `traffic_flow.html` 中新增状态变量**

找到 `traffic_flow.html` 中 `_refreshTimer` 变量声明（约第 692 行 `let _refreshTimer = null;`），在其后添加：

```javascript
        let _refreshTimer = null;  // 防抖定时器，避免短时间内多次刷新叠加
        let _loadingFast = false;  // fast 请求加载标志，防止自动刷新堆积
        let _loadingSlow = false;  // slow 请求加载标志，独立于 fast
```

- [ ] **Step 2: 新增 `refreshFast()` 函数**

找到 `traffic_flow.html` 中 `async function refresh()` 函数（约第 694 行），在其之前插入新函数：

```javascript
        async function refreshFast() {
            if (!api || _loadingFast) return;
            _loadingFast = true;
            try {
                const data = await api.get_traffic_status_fast();
                lastData = data;
                stats = data.stats || stats;
                warpUnderlay = data.warp_underlay || 'ipv4';
                // 累计展示模式：将当前连接合并到历史（同 ID 覆盖为最新）
                if (cumulativeMode && data.connections && data.connections.length > 0) {
                    for (const c of data.connections) {
                        cumulativeConns.set(connId(c), c);
                    }
                }
                updateStats();
                renderConnList();
            } catch (e) {
                showToast('获取失败: ' + e);
                const container = document.getElementById('connList');
                if (container && (!lastData || !lastData.connections)) {
                    container.innerHTML = '<div class="empty-hint">获取数据失败，请点击刷新重试</div>';
                }
            } finally {
                _loadingFast = false;
            }
        }

        async function refreshSlow() {
            if (!api || _loadingSlow) return;
            _loadingSlow = true;
            try {
                // 收集当前未命中域名的 IP
                const missingIps = [];
                for (const c of (lastData?.connections || [])) {
                    if (!c.hostname && c.remote_ip) missingIps.push(c.remote_ip);
                }
                if (missingIps.length === 0) return;
                const ipToHost = await api.get_traffic_status_slow(missingIps);
                // 增量更新已渲染连接的域名
                let updated = false;
                for (const c of (lastData?.connections || [])) {
                    if (!c.hostname && ipToHost[c.remote_ip]) {
                        c.hostname = ipToHost[c.remote_ip];
                        updated = true;
                    }
                }
                if (updated) {
                    // 同步更新累计模式中的连接
                    if (cumulativeMode) {
                        for (const c of (lastData?.connections || [])) {
                            if (c.hostname) cumulativeConns.set(connId(c), c);
                        }
                    }
                    renderConnList();
                }
            } catch (e) {
                // slow 失败不影响主流程，域名保持 IP 显示
                console.warn('refreshSlow failed:', e);
            } finally {
                _loadingSlow = false;
            }
        }

```

- [ ] **Step 3: 修改 `init()` 函数使用分阶段加载**

找到 `traffic_flow.html` 中 `async function init()` 函数（约第 1041 行），替换为：

```javascript
        async function init() {
            if (_initStarted) return;
            _initStarted = true;
            try {
                await waitForApi(15000);
            } catch (e) {
                document.getElementById('connList').innerHTML =
                    '<div class="empty-hint">API 加载超时，请关闭窗口重试</div>';
                showToast('API 加载超时: ' + e.message);
                return;
            }
            initCanvas();
            await refreshFast();        // 先拿 fast 数据，立即渲染（域名显示 IP）
            refreshSlow();              // 后台异步（不 await），拿到域名后增量更新
            startAuto();                // 自动刷新改为 fast-only
        }
```

- [ ] **Step 4: 修改 `startAuto()` 使用 `refreshFast`**

找到 `traffic_flow.html` 中 `function startAuto()` 函数（约第 1032 行），替换为：

```javascript
        function startAuto() {
            stopAuto();
            autoTimer = setInterval(refreshFast, 3000);
        }
```

- [ ] **Step 5: 验证 HTML 语法正确（无未闭合标签）**

Run: `python -c "import re; src = open('traffic_flow.html', encoding='utf-8').read(); assert src.count('<script>') == src.count('</script>'); assert 'refreshFast' in src and 'refreshSlow' in src and '_loadingFast' in src and '_loadingSlow' in src; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 验证 `refreshFast` 和 `refreshSlow` 在正确位置**

Run: `python -c "src = open('traffic_flow.html', encoding='utf-8').read(); assert 'async function refreshFast()' in src, 'missing refreshFast'; assert 'async function refreshSlow()' in src, 'missing refreshSlow'; assert 'await refreshFast()' in src, 'init not using refreshFast'; assert 'refreshSlow();' in src, 'init not calling refreshSlow'; assert 'setInterval(refreshFast, 3000)' in src, 'startAuto not using refreshFast'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 提交**

```bash
git add traffic_flow.html
git commit -m "feat: traffic_flow.html 改为分阶段加载（fast 首屏 + slow 域名后加载）"
```

---

### Task 5: traffic_monitor.html 前端改造 — 分阶段加载

**Files:**
- Modify: `traffic_monitor.html`（`init()`、`refresh()`、`startAutoRefresh()` 区域）

**Interfaces:**
- Consumes: `api.get_traffic_status_fast()`、`api.get_traffic_status_slow(missing_ips)`（Task 3 产出）
- Produces: 改造后的前端加载流程

- [ ] **Step 1: 在 `traffic_monitor.html` 中新增状态变量**

找到 `traffic_monitor.html` 中已有的全局变量声明区域（搜索 `let autoRefreshTimer` 或 `let _initStarted`，约在第 230-260 行之间），在该区域添加：

```javascript
        let _loadingFast = false;  // fast 请求加载标志，防止自动刷新堆积
        let _loadingSlow = false;  // slow 请求加载标志，独立于 fast
```

- [ ] **Step 2: 新增 `refreshFast()` 和 `refreshSlow()` 函数**

找到 `traffic_monitor.html` 中 `async function refresh()` 函数（约第 289 行），在其之前插入新函数：

```javascript
        async function refreshFast() {
            if (!api || _loadingFast) return;
            _loadingFast = true;
            const btn = document.getElementById('refreshBtn');
            const bar = document.getElementById('loadingBar');
            if (btn) btn.disabled = true;
            if (bar) bar.classList.add('active');
            try {
                const data = await api.get_traffic_status_fast();
                lastData = data;
                renderStats(data.stats);
                renderWarpInfo(data.warp_underlay);
                renderConnections();
            } catch (e) {
                showToast('获取失败: ' + e);
                const container = document.getElementById('connList');
                if (container && (!lastData || !lastData.connections)) {
                    container.innerHTML = '<div class="empty-hint">获取数据失败，请点击刷新重试</div>';
                }
            } finally {
                _loadingFast = false;
                if (btn) btn.disabled = false;
                if (bar) bar.classList.remove('active');
            }
        }

        async function refreshSlow() {
            if (!api || _loadingSlow) return;
            _loadingSlow = true;
            try {
                const missingIps = [];
                for (const c of (lastData?.connections || [])) {
                    if (!c.hostname && c.remote_ip) missingIps.push(c.remote_ip);
                }
                if (missingIps.length === 0) return;
                const ipToHost = await api.get_traffic_status_slow(missingIps);
                let updated = false;
                for (const c of (lastData?.connections || [])) {
                    if (!c.hostname && ipToHost[c.remote_ip]) {
                        c.hostname = ipToHost[c.remote_ip];
                        updated = true;
                    }
                }
                if (updated) renderConnections();
            } catch (e) {
                console.warn('refreshSlow failed:', e);
            } finally {
                _loadingSlow = false;
            }
        }

```

- [ ] **Step 3: 修改 `init()` 函数使用分阶段加载**

找到 `traffic_monitor.html` 中 `async function init()` 函数（约第 397 行），替换为：

```javascript
        async function init() {
            if (_initStarted) return;
            _initStarted = true;
            try {
                await waitForApi(15000);
            } catch (e) {
                document.getElementById('connList').innerHTML =
                    '<div class="empty-hint">API 加载超时，请关闭窗口重试</div>';
                showToast('API 加载超时: ' + e.message);
                return;
            }
            await refreshFast();        // 先拿 fast 数据，立即渲染
            refreshSlow();              // 后台异步，拿到域名后增量更新
            startAutoRefresh();         // 自动刷新改为 fast-only
        }
```

- [ ] **Step 4: 修改 `startAutoRefresh()` 使用 `refreshFast`**

找到 `traffic_monitor.html` 中 `function startAutoRefresh()` 函数（约第 388 行），替换为：

```javascript
        function startAutoRefresh() {
            stopAutoRefresh();
            autoRefreshTimer = setInterval(refreshFast, 3000);
        }
```

- [ ] **Step 5: 验证 HTML 语法正确**

Run: `python -c "src = open('traffic_monitor.html', encoding='utf-8').read(); assert src.count('<script>') == src.count('</script>'); assert 'refreshFast' in src and 'refreshSlow' in src and '_loadingFast' in src and '_loadingSlow' in src; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 验证关键函数引用正确**

Run: `python -c "src = open('traffic_monitor.html', encoding='utf-8').read(); assert 'async function refreshFast()' in src; assert 'async function refreshSlow()' in src; assert 'await refreshFast()' in src; assert 'refreshSlow();' in src; assert 'setInterval(refreshFast, 3000)' in src; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 提交**

```bash
git add traffic_monitor.html
git commit -m "feat: traffic_monitor.html 改为分阶段加载（fast 首屏 + slow 域名后加载）"
```

---

### Task 6: 最终验证

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 验证所有模块导入正常**

Run: `python -c "import tray_app; import traffic_monitor; from traffic_monitor import get_traffic_status, get_traffic_status_fast, get_traffic_status_slow; from traffic_monitor import _get_dns_cache_only; print('ALL IMPORTS OK')"`
Expected: 输出 `ALL IMPORTS OK`

- [ ] **Step 2: 验证 ApiBridge 方法存在**

Run: `python -c "import tray_app; assert hasattr(tray_app.ApiBridge, 'get_traffic_status_fast'); assert hasattr(tray_app.ApiBridge, 'get_traffic_status_slow'); assert hasattr(tray_app.ApiBridge, 'get_traffic_status'); print('APIBRIDGE OK')"`
Expected: 输出 `APIBRIDGE OK`

- [ ] **Step 3: 验证 fast 接口耗时 <3 秒**

Run: `python -c "import time; from traffic_monitor import get_traffic_status_fast; t0=time.time(); r=get_traffic_status_fast(); elapsed=time.time()-t0; assert elapsed < 3.0, f'fast 接口耗时 {elapsed:.2f}s 超过 3 秒'; print(f'FAST OK: elapsed={elapsed:.2f}s, total={r[\"total\"]}')"`
Expected: 输出 `FAST OK: elapsed=<3.0s, total=N`

- [ ] **Step 4: 验证 slow 接口返回字典**

Run: `python -c "from traffic_monitor import get_traffic_status_slow; d = get_traffic_status_slow(None); assert isinstance(d, dict), f'slow 返回类型错误: {type(d)}'; print(f'SLOW OK: entries={len(d)}')"`
Expected: 输出 `SLOW OK: entries=N`

- [ ] **Step 5: 验证重构后的 get_traffic_status 仍工作**

Run: `python -c "from traffic_monitor import get_traffic_status; r = get_traffic_status(); assert 'stats' in r and 'connections' in r and 'total' in r; print(f'COMPAT OK: total={r[\"total\"]}')"`
Expected: 输出 `COMPAT OK: total=N`

- [ ] **Step 6: 验证 fast 返回的 hostname 全为空**

Run: `python -c "from traffic_monitor import get_traffic_status_fast; r = get_traffic_status_fast(); all_empty = all(not c['hostname'] for c in r['connections']); assert all_empty, 'fast 返回的 hostname 不全为空'; print('HOSTNAME EMPTY OK')"`
Expected: 输出 `HOSTNAME EMPTY OK`

- [ ] **Step 7: 验证 HTML 前端关键函数存在**

Run: `python -c "src1 = open('traffic_flow.html', encoding='utf-8').read(); src2 = open('traffic_monitor.html', encoding='utf-8').read(); assert 'refreshFast' in src1 and 'refreshSlow' in src1; assert 'refreshFast' in src2 and 'refreshSlow' in src2; assert '_loadingFast' in src1 and '_loadingSlow' in src1; assert '_loadingFast' in src2 and '_loadingSlow' in src2; print('HTML OK')"`
Expected: 输出 `HTML OK`

- [ ] **Step 8: 最终提交（如有清理）**

如果前面步骤中有未提交的清理，在此提交。否则跳过。

```bash
git add -A
git commit -m "refactor: 流量监控渐进式加载优化完成" || echo "无需提交"
```

---

## 验证总结

### 功能验证清单

完成所有任务后，手动验证以下功能：

- [ ] `python tray_app.py` 能正常启动
- [ ] 打开"流量监控"窗口，首屏 <3 秒出现数据（域名显示"(无域名)"或 IP）
- [ ] 首屏后 5 秒内，域名逐步填充
- [ ] 打开"流量可视化"窗口，首屏 <3 秒出现动画和数据
- [ ] 自动刷新不堆积（连续观察 1 分钟，日志中无请求积压）
- [ ] 窗口隐藏/显示时正常停止/恢复

### 预期结果对比

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 首屏时间 | ~15 秒 | <3 秒 |
| 域名显示 | 首屏同步 | 首屏后异步填充 |
| 自动刷新 | 3 秒触发 15 秒查询（堆积） | 3 秒触发 2 秒查询（防堆积） |
| `get_traffic_status` 接口 | 保留（兼容） | 保留（内部 fast+slow 串行） |
