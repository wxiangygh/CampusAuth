# 流量监控渐进式加载优化设计

## 背景

CampusAuth 的流量监控（`traffic_monitor.html`）和流量可视化（`traffic_flow.html`）窗口启动非常慢，用户从点击菜单到看到数据需等待 14-19 秒。

## 根本原因分析

通过日志分析（tray_app.log）和代码审查，确认两层瓶颈：

### 后端瓶颈：`get_traffic_status()` 耗时 14-19 秒

| 日志时间 | 耗时 | 连接数 | 反向 DNS 查询数 |
|---------|------|--------|----------------|
| 13:07:55 | 18.73s | 41 | 19 |
| 13:08:15 | 17.45s | 39 | 21 |
| 14:14:19 | 15.96s | 44 | 15 |
| 14:14:37 | 14.87s | 49 | 15 |
| 14:14:45 | 13.75s | 50 | 15 |

耗时组成：
1. **PowerShell 脚本**（`_get_network_snapshot`，timeout=15s）：~8-12 秒
   - `Get-NetTCPConnection -State Established`：获取 TCP 连接
   - `Get-Process`：构建 PID→进程名映射
   - `Get-NetRoute -PolicyStore ActiveStore`：获取路由表
   - `Get-DnsClientCache`：获取 DNS 缓存（IP→域名映射，最慢的 cmdlet 之一）
   - `Get-NetUDPEndpoint`：判断 WARP 底层连接类型
   - PowerShell 进程启动本身约 1-2 秒
2. **反向 DNS 后备查询**（`_batch_reverse_dns`）：~3-5 秒
   - 对 15-21 个未命中 DNS 缓存的 IP 并发查询
   - 每个查询 timeout=1.0s，`as_completed` timeout=3s
3. **Python 后处理**（分类逻辑、排序）：~0.5 秒

### 前端瓶颈：启动时序 + 自动刷新堆积

1. 窗口创建后等待 `pywebviewready` 事件（或 1 秒超时 fallback）
2. `waitForApi(15000)` 最多等 15 秒
3. `init()` 中 `await refresh()` 是首次数据获取——此时用户看到空白/加载中
4. 自动刷新 `setInterval(refresh, 3000)` 每 3 秒触发一次 14 秒的查询，导致请求堆积

## 设计目标

- 首屏时间（窗口出现到看到数据）从 ~15 秒降至 <3 秒
- 域名信息异步加载，不阻塞首屏
- 修复自动刷新堆积问题
- 保留原 `get_traffic_status()` 接口兼容性

## 方案选择

**方案 A：渐进式加载**（已选定）

拆分 `get_traffic_status()` 为 fast/slow 两部分，前端先拿快数据展示，慢数据异步加载。

## 详细设计

### 1. 后端拆分 `traffic_monitor.py`

#### 1.1 `get_traffic_status_fast()`

**功能**：快速获取连接列表和分类统计，不包含域名。

**实现**：
- 执行精简 PowerShell 脚本（从 `_get_network_snapshot` 移除 `Get-DnsClientCache` 部分）
- 不执行反向 DNS 后备查询
- 返回与原 `get_traffic_status()` 相同结构，但 `hostname` 字段为空字符串
- 目标耗时 <2 秒

**PowerShell 脚本精简**：
- 保留：`Get-NetTCPConnection`、`Get-Process`、`Get-NetRoute`、`Get-NetUDPEndpoint`（WARP 底层判断）
- 移除：`Get-DnsClientCache` 整段（约 15 行）

**返回结构**：
```python
{
    'stats': {route_type: N, ...},  # 6 种分类计数
    'connections': [{process, remote_ip, remote_port, hostname: '', route_type, is_warp, is_ipv6, warp_underlay}],
    'warp_ifindex': N,
    'warp_underlay': 'ipv4' | 'ipv6',
    'total': N,
    'route_types': ROUTE_TYPES,
}
```

#### 1.2 `get_traffic_status_slow()`

**功能**：获取 IP→域名映射，供前端增量更新。

**实现**：
- 执行独立的 PowerShell 脚本，仅调用 `Get-DnsClientCache` 构建 IP→域名映射
- 对传入的未命中 IP 列表执行 `_batch_reverse_dns()` 后备查询
- 返回 `{ip: hostname}` 字典

**签名**：
```python
def get_traffic_status_slow(missing_ips: list[str] = None) -> dict[str, str]:
    """获取 IP→域名映射。

    Args:
        missing_ips: 需要查询的 IP 列表。None 时仅返回 DNS 缓存映射。

    Returns:
        dict[str, str]: {ip: hostname}（仅包含查询成功的）
    """
```

**实现逻辑**：
1. PowerShell 执行 `Get-DnsClientCache`，构建 `dns_map`
2. 如果 `missing_ips` 非空，过滤出 `dns_map` 中未命中的 IP
3. 对未命中 IP 执行 `_batch_reverse_dns(ip_list, timeout=1.0, max_workers=10)`
4. 合并 `dns_map` 和反向 DNS 结果，返回

#### 1.3 `_get_dns_cache_only()`

**新增辅助函数**：仅获取 DNS 缓存映射。

```python
def _get_dns_cache_only() -> dict[str, str]:
    """仅执行 Get-DnsClientCache，返回 IP→域名映射。"""
```

供 `get_traffic_status_slow()` 内部调用。

#### 1.4 保留 `get_traffic_status()`

原接口保留，内部改为调用 `fast` + `slow` 串行（保持向后兼容，但不再被前端直接调用）。

### 2. `ApiBridge` 新增方法（`tray_app.py`）

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

**导入变更**：`tray_app.py` 顶部从 `from traffic_monitor import get_traffic_status` 改为：
```python
from traffic_monitor import get_traffic_status, get_traffic_status_fast, get_traffic_status_slow
```

### 3. 前端改造

#### 3.1 `traffic_flow.html` 改造

**`init()` 改为分阶段加载**：

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

**新增 `refreshFast()`**：

```javascript
async function refreshFast() {
    if (!api || _loadingFast) return;
    _loadingFast = true;
    try {
        const data = await api.get_traffic_status_fast();
        lastData = data;
        stats = data.stats || stats;
        warpUnderlay = data.warp_underlay || 'ipv4';
        // 累计展示模式处理（与原逻辑相同）
        if (cumulativeMode && data.connections && data.connections.length > 0) {
            for (const c of data.connections) {
                cumulativeConns.set(connId(c), c);
            }
        }
        updateStats();
        renderConnList();
    } catch (e) {
        showToast('获取失败: ' + e);
    } finally {
        _loadingFast = false;
    }
}
```

**新增 `refreshSlow()`**：

```javascript
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
            // 更新累计模式中的连接
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

**新增状态变量**：
```javascript
let _loadingFast = false;
let _loadingSlow = false;
```

**`renderConnList()` 修改**：hostname 为空时显示 IP（原逻辑可能已如此，需确认）。

**自动刷新改为 fast-only**：
```javascript
function startAuto() {
    stopAuto();
    autoTimer = setInterval(refreshFast, 3000);
}
```

**防堆积机制**：`refreshFast()` 开头 `if (_loadingFast) return`，避免 setInterval 触发的请求堆积。

#### 3.2 `traffic_monitor.html` 改造

与 `traffic_flow.html` 相同的改造模式：`init()` 分阶段、新增 `refreshFast`/`refreshSlow`、自动刷新改 fast-only、防堆积。

### 4. 防堆积机制

- `_loadingFast` 标志：`refreshFast()` 开头检查，未完成则跳过
- `_loadingSlow` 标志：`refreshSlow()` 独立标志，不阻塞 fast
- 两个标志独立，允许 fast 和 slow 并行执行

## 数据流

```
[窗口创建]
  ↓
[waitForApi]
  ↓
[refreshFast()]                    ← <2 秒
  ├─ 调 get_traffic_status_fast
  ├─ 渲染统计条 + 连接列表（hostname 为空时显示 IP）
  └─ 启动 startAuto()（每 3 秒 refreshFast，防堆积）
  ↓
[refreshSlow()]                    ← 异步，不 await，<5 秒
  ├─ 收集未命中域名的 IP
  ├─ 调 get_traffic_status_slow(missingIps)
  ├─ 拿到 {ip: hostname}
  └─ 遍历已渲染连接，更新域名显示（增量重渲染）
```

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| fast 失败 | 显示"获取数据失败"，不触发 slow |
| slow 失败 | 连接列表保留 IP 显示，不影响主流程 |
| slow 超时 | 前端独立，`_loadingSlow` 标志确保不堆积 |
| 自动刷新期间 fast 未完成 | 跳过本次，等下次 interval |

## 变更文件

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `traffic_monitor.py` | 新增函数 | `get_traffic_status_fast()`、`get_traffic_status_slow()`、`_get_dns_cache_only()` |
| `traffic_monitor.py` | 重构 | `get_traffic_status()` 内部改为调用 fast+slow 串行 |
| `tray_app.py` | 新增方法 | `ApiBridge.get_traffic_status_fast`、`ApiBridge.get_traffic_status_slow` |
| `tray_app.py` | 修改导入 | 添加 `get_traffic_status_fast, get_traffic_status_slow` |
| `traffic_flow.html` | 前端改造 | `init()` 分阶段、新增 `refreshFast`/`refreshSlow`、自动刷新改 fast-only |
| `traffic_monitor.html` | 前端改造 | 同上 |

## 测试策略

### 后端测试
- `get_traffic_status_fast()` 耗时 <3 秒（实测验证）
- `get_traffic_status_slow()` 返回字典，耗时 <5 秒
- `get_traffic_status()`（fast+slow 串行）结果与原实现一致

### 前端测试
- 首屏时间 <3 秒（从窗口创建到看到数据）
- 域名在首屏后 5 秒内逐步填充
- 自动刷新不堆积（连续观察 1 分钟，无请求积压）
- 窗口隐藏/显示时正常停止/恢复

## 非目标

- 不优化 PowerShell 进程启动开销（约 1-2 秒，属于系统级开销）
- 不修改 WARP 底层连接类型判断逻辑
- 不修改连接分类逻辑（6 种 route_type）
- 不修改累计展示模式逻辑
- 不修改窗口创建/管理逻辑
