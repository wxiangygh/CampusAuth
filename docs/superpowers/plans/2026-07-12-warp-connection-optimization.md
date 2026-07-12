# WARP 连接失败优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化认证流程中的 WARP 连接步骤，通过增强 IPv6 就绪检测和自动重新认证回退机制，将失败率从 10-20% 降至 5% 以下。

**Architecture:** 新增 `has_public_ipv6()` 函数检测 2001 开头的公网 IPv6 地址；重构 `_wait_for_ipv6_ready()` 改用此检测标准；在 `run_auth_task` 步骤 5 增加"IPv6 不可达→重新认证"的回退循环；恢复 `_set_warp_endpoint_ipv6` 调用修复回归 bug；WARP 连接失败时记录诊断日志。

**Tech Stack:** Python 3.12, Windows netsh/ipconfig/PowerShell, Cloudflare WARP CLI

## Global Constraints

- Logger 名称使用 `logging.getLogger('wifi_tray')`（不要用 'tray_app'）
- 代码注释使用中文
- 不修改 HTML 前端文件
- 不修改 ApiBridge 接口
- 不修改 `disconnect_warp` 或 `connect_warp` 内部实现
- A/B 类变量导入策略：A 类（Event/Lock/str 常量）用 `from core.state import X`；B 类（None/bool/dict/object 可重新赋值）用 `core.state.X = value`
- 保留 `_check_cancel()` 检查以支持用户取消操作

---

## File Structure

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `core/network.py` | 网络检测 | 新增 `has_public_ipv6()`，重构 `_wait_for_ipv6_ready()` |
| `core/auth.py` | 认证流程 | 重构 `run_auth_task` 步骤 5，恢复 `_set_warp_endpoint_ipv6` 调用 |
| `tray_app.py` | 主入口 | 清理 `_set_warp_endpoint_ipv6` 导入 |

---

### Task 1: 新增 `has_public_ipv6()` 到 `core/network.py`

**Files:**
- Modify: `core/network.py`（在 `_wait_for_ipv6_ready` 函数前插入新函数）

**Interfaces:**
- Consumes: `core.command.run_command`
- Produces: `has_public_ipv6() -> tuple[bool, str]` — 后续 Task 2 和 Task 3 使用

- [ ] **Step 1: 在 `core/network.py` 的 `_wait_for_ipv6_ready` 函数前插入 `has_public_ipv6`**

在 `core/network.py` 中找到 `_wait_for_ipv6_ready` 函数定义（约第 132 行 `def _wait_for_ipv6_ready(max_retries=8):`），在它前面插入以下完整函数：

```python
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
        if 'IPv6' not in line_stripped and 'IPv6 地址' not in line_stripped:
            continue
        if ':' not in line_stripped:
            continue
        # 提取地址部分（最后一个冒号之后）
        addr = line_stripped.rsplit(':', 1)[1].strip()
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
```

- [ ] **Step 2: 验证函数可导入**

Run: `python -c "from core.network import has_public_ipv6; print(has_public_ipv6)"`
Expected: 输出函数对象 `<function has_public_ipv6 at 0x...>`

- [ ] **Step 3: 验证函数在本机工作**

Run: `python -c "from core.network import has_public_ipv6; found, addr = has_public_ipv6(); print(f'found={found}, addr={addr}')"`
Expected: 输出 `found=True/False, addr=...`（取决于本机当前 IPv6 状态，无报错即可）

- [ ] **Step 4: 提交**

```bash
git add core/network.py
git commit -m "feat: 新增 has_public_ipv6() 检测公网 IPv6 地址"
```

---

### Task 2: 重构 `_wait_for_ipv6_ready()` 使用 2001 开头检测

**Files:**
- Modify: `core/network.py:132-154`（`_wait_for_ipv6_ready` 函数整体替换）

**Interfaces:**
- Consumes: `has_public_ipv6()`（Task 1 产出），`_check_cancel()`，`_interruptible_sleep()`
- Produces: `_wait_for_ipv6_ready(max_retries=20) -> bool`（签名不变，默认重试次数从 8 改为 20）

- [ ] **Step 1: 替换 `_wait_for_ipv6_ready` 函数**

将 `core/network.py` 中的 `_wait_for_ipv6_ready` 函数（约第 132-154 行）整体替换为：

```python
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
```

- [ ] **Step 2: 验证函数签名和导入**

Run: `python -c "from core.network import _wait_for_ipv6_ready; import inspect; sig = inspect.signature(_wait_for_ipv6_ready); print(sig)"`
Expected: 输出 `(max_retries=20)`

- [ ] **Step 3: 验证 core.auth 导入正常（不破坏下游）**

Run: `python -c "import core.auth; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 提交**

```bash
git add core/network.py
git commit -m "refactor: _wait_for_ipv6_ready 改用 2001 开头公网 IPv6 检测"
```

---

### Task 3: 恢复 `_set_warp_endpoint_ipv6` 调用并重构 `run_auth_task` 步骤 5

**Files:**
- Modify: `core/auth.py:17-20`（导入 `_set_warp_endpoint_ipv6`）
- Modify: `core/auth.py:344-385`（`run_auth_task` 步骤 4-5 重构）
- Modify: `tray_app.py:29-33`（删除 `_set_warp_endpoint_ipv6` 导入）

**Interfaces:**
- Consumes: 
  - `_set_warp_endpoint_ipv6(enable) -> bool`（来自 `core.warp_manager`）
  - `_wait_for_ipv6_ready(max_retries=20) -> bool`（Task 2 产出）
  - `has_public_ipv6() -> tuple[bool, str]`（Task 1 产出，用于诊断）
  - `portal_login()`, `portal_logout()`, `enable_ipv4()`, `disable_ipv4()`, `connect_warp()`, `get_warp_cli()`, `_set_warp_masque_mode()`（现有函数）
- Produces: 重构后的 `run_auth_task()`，行为变更：IPv6 不可达时自动重新认证

- [ ] **Step 1: 在 `core/auth.py` 导入区添加 `_set_warp_endpoint_ipv6`**

找到 `core/auth.py` 第 17-20 行的导入块：

```python
from core.warp_manager import (
    connect_warp, disconnect_warp, get_warp_cli,
    _set_warp_masque_mode,
)
```

替换为：

```python
from core.warp_manager import (
    connect_warp, disconnect_warp, get_warp_cli,
    _set_warp_masque_mode, _set_warp_endpoint_ipv6,
)
```

- [ ] **Step 2: 在 `core/auth.py` 导入区添加 `has_public_ipv6`**

找到 `core/auth.py` 第 13-16 行的导入块：

```python
from core.network import (
    get_wifi_interface_name, get_local_ip, get_mac_address,
    wait_for_network_ready, _wait_for_ipv6_ready, is_warp_connected,
)
```

替换为：

```python
from core.network import (
    get_wifi_interface_name, get_local_ip, get_mac_address,
    wait_for_network_ready, _wait_for_ipv6_ready, is_warp_connected,
    has_public_ipv6,
)
```

- [ ] **Step 3: 重构 `run_auth_task` 步骤 5**

找到 `core/auth.py` 中 `run_auth_task` 函数的步骤 5 部分（约第 344-385 行），从第 344 行 `if _check_cancel(): return False, "已取消"`（步骤 4 结束后的取消检查）之后，直到 `return True, "认证成功"` 之前的内容。

将以下代码块（约第 345-385 行）：

```python
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
    # 认证成功后，根据配置决定是否重新启用 IPv4
    # auto_enable_ipv4=True：启用 IPv4（同时保持 WARP，用户可同时访问 IPv4 和 IPv6）
    # auto_enable_ipv4=False：保持 IPv4 禁用，所有流量走 WARP（IPv6）
    if CONFIG.get('auto_enable_ipv4', True):
        logger.info("auto_enable_ipv4=True, re-enabling IPv4 after auth")
        if enable_ipv4(interface_name):
            logger.info("IPv4 re-enabled after auth")
        else:
            logger.warning("Failed to re-enable IPv4 after auth")
    else:
        logger.info("auto_enable_ipv4=False, keeping IPv4 disabled (all traffic via WARP)")
    _push_auth_progress(5, 5, '认证成功', 'success')
    return True, "认证成功"
```

替换为：

```python
    _push_auth_progress(5, 5, '禁用IPv4并连接WARP...')
    logger.info("[5/5] Disabling IPv4 and connecting WARP...")
    if not disable_ipv4(interface_name):
        _push_auth_progress(5, 5, '禁用IPv4失败', 'error')
        return False, "禁用IPv4失败"
    if _check_cancel(): return False, "已取消"
    if warp_service_was_running:
        logger.info("Re-enabling WARP virtual adapter...")
        run_command('netsh interface set interface "CloudflareWARP" enable')
    # 等待公网 IPv6 地址（2001 开头），最多 60 秒
    if not _wait_for_ipv6_ready(max_retries=20):
        # IPv6 不可达，进入重新认证回退循环（最多 2 轮）
        ipv6_ready = False
        for retry in range(2):
            if _check_cancel(): return False, "已取消"
            _push_auth_progress(5, 5, f'IPv6未就绪，重新认证以获取IPv6（第{retry+1}轮）...')
            logger.info(f"IPv6 not ready, re-authenticating to trigger IPv6 assignment (round {retry+1}/2)")
            # 临时启用 IPv4 以恢复网络连接
            if not enable_ipv4(interface_name):
                logger.warning("Failed to temporarily enable IPv4 during re-auth retry")
            if _check_cancel(): return False, "已取消"
            # 注销当前会话
            portal_logout()
            if not _interruptible_sleep(2): return False, "已取消"
            if _check_cancel(): return False, "已取消"
            # 重新认证
            success, msg = portal_login()
            if not success:
                _push_auth_progress(5, 5, f'重新认证失败: {msg}', 'error')
                logger.error(f"Re-auth failed during IPv6 retry: {msg}")
                return False, f"重新认证失败: {msg}"
            if _check_cancel(): return False, "已取消"
            # 再次禁用 IPv4
            if not disable_ipv4(interface_name):
                _push_auth_progress(5, 5, '禁用IPv4失败', 'error')
                return False, "禁用IPv4失败"
            if _check_cancel(): return False, "已取消"
            # 等待 IPv6 就绪
            if _wait_for_ipv6_ready(max_retries=20):
                ipv6_ready = True
                logger.info(f"IPv6 ready after re-auth round {retry+1}")
                break
            logger.warning(f"IPv6 still not ready after re-auth round {retry+1}")
        if not ipv6_ready:
            # 2 轮重新认证后 IPv6 仍不可达，恢复 IPv4 并返回失败
            enable_ipv4(interface_name)
            _push_auth_progress(5, 5, 'IPv6网络不可用，已尝试重新认证', 'error')
            return False, "IPv6网络不可用，无法连接WARP（已尝试重新认证）"
    if _check_cancel(): return False, "已取消"
    # IPv6 可达，清空 conf.json 的 IPv4 端点以强制 WARP 走 IPv6
    if not _set_warp_endpoint_ipv6(True):
        logger.warning("_set_warp_endpoint_ipv6(True) failed, continuing with default endpoints")
    if _check_cancel(): return False, "已取消"
    run_command('sc config "CloudflareWARP" start= auto')
    code, svc_output, _ = run_command('sc query "CloudflareWARP"')
    if 'RUNNING' not in svc_output:
        logger.info("Starting WARP service for MASQUE config...")
        run_command('net start "CloudflareWARP"')
        time.sleep(3)
    warp_cli = get_warp_cli()
    _set_warp_masque_mode(warp_cli, True)
    if not connect_warp():
        # WARP 连接失败：记录诊断日志
        logger.error("WARP connection failed. Diagnostics:")
        try:
            code, status_output, _ = run_command([warp_cli, 'status'], shell=False)
            logger.error(f"warp-cli status:\n{status_output}")
        except Exception as diag_e:
            logger.error(f"Failed to get warp-cli status: {diag_e}")
        has_v6, v6_addr = has_public_ipv6()
        logger.error(f"Public IPv6: {has_v6}, addr={v6_addr}")
        code, route_output, _ = run_command('netsh interface ipv6 show route')
        logger.error(f"IPv6 routes (first 500 chars):\n{route_output[:500]}")
        # 恢复配置
        _set_warp_masque_mode(warp_cli, False)
        _set_warp_endpoint_ipv6(False)
        _push_auth_progress(5, 5, 'WARP连接超时，请手动检查', 'error')
        return False, "WARP连接超时，请手动检查"
    # WARP 连接成功，恢复配置
    _set_warp_masque_mode(warp_cli, False)
    _set_warp_endpoint_ipv6(False)
    logger.info("=" * 60)
    logger.info("Authentication completed successfully")
    logger.info("=" * 60)
    # 认证成功后，根据配置决定是否重新启用 IPv4
    # auto_enable_ipv4=True：启用 IPv4（同时保持 WARP，用户可同时访问 IPv4 和 IPv6）
    # auto_enable_ipv4=False：保持 IPv4 禁用，所有流量走 WARP（IPv6）
    if CONFIG.get('auto_enable_ipv4', True):
        logger.info("auto_enable_ipv4=True, re-enabling IPv4 after auth")
        if enable_ipv4(interface_name):
            logger.info("IPv4 re-enabled after auth")
        else:
            logger.warning("Failed to re-enable IPv4 after auth")
    else:
        logger.info("auto_enable_ipv4=False, keeping IPv4 disabled (all traffic via WARP)")
    _push_auth_progress(5, 5, '认证成功', 'success')
    return True, "认证成功"
```

- [ ] **Step 4: 从 `tray_app.py` 导入中删除 `_set_warp_endpoint_ipv6`**

找到 `tray_app.py` 第 29-33 行的导入块：

```python
from core.warp_manager import (
    get_warp_cli, connect_warp, disconnect_warp,
    _set_warp_masque_mode, _set_warp_endpoint_ipv6,
    update_tray_icon, update_tray_icon_restore,
)
```

替换为（删除 `_set_warp_endpoint_ipv6`）：

```python
from core.warp_manager import (
    get_warp_cli, connect_warp, disconnect_warp,
    _set_warp_masque_mode,
    update_tray_icon, update_tray_icon_restore,
)
```

- [ ] **Step 5: 验证 `core/auth.py` 导入正常**

Run: `python -c "import core.auth; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 验证 `tray_app.py` 导入正常**

Run: `python -c "import tray_app; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 7: 验证 `_set_warp_endpoint_ipv6` 已从 tray_app.py 导入中移除**

Run: `python -c "src = open('tray_app.py', encoding='utf-8').read(); assert '_set_warp_endpoint_ipv6' not in src, 'tray_app.py 仍引用 _set_warp_endpoint_ipv6'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 8: 验证 `core/auth.py` 已导入 `_set_warp_endpoint_ipv6`**

Run: `python -c "import core.auth; assert hasattr(core.auth, '_set_warp_endpoint_ipv6'), 'core.auth 未导入 _set_warp_endpoint_ipv6'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 9: 验证 `run_auth_task` 中包含重新认证回退逻辑**

Run: `python -c "import inspect, core.auth; src = inspect.getsource(core.auth.run_auth_task); assert 're-authenticating to trigger IPv6' in src, '缺少重新认证回退逻辑'; assert '_set_warp_endpoint_ipv6(True)' in src, '缺少 _set_warp_endpoint_ipv6(True) 调用'; assert '_set_warp_endpoint_ipv6(False)' in src, '缺少 _set_warp_endpoint_ipv6(False) 调用'; assert 'WARP connection failed. Diagnostics' in src, '缺少诊断日志'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 10: 验证所有模块联合导入**

Run: `python -c "import tray_app; import core.auth; import core.network; from core.network import has_public_ipv6; from core.auth import run_auth_task; print('ALL OK')"`
Expected: 输出 `ALL OK`

- [ ] **Step 11: 提交**

```bash
git add core/auth.py tray_app.py
git commit -m "feat: WARP 连接优化——IPv6 不可达时自动重新认证，恢复 _set_warp_endpoint_ipv6 调用"
```

---

### Task 4: 最终验证

**Files:**
- 无文件修改，仅验证

- [ ] **Step 1: 验证所有模块导入**

Run: `python -c "import tray_app; import warp_exclusion; import traffic_monitor; import core.state; import core.command; import core.webview; import core.network; import core.warp_manager; import core.auth; import core.startup; print('ALL IMPORTS OK')"`
Expected: 输出 `ALL IMPORTS OK`

- [ ] **Step 2: 验证 `has_public_ipv6` 可调用**

Run: `python -c "from core.network import has_public_ipv6; found, addr = has_public_ipv6(); print(f'found={found}, addr={addr}')"`
Expected: 输出 `found=True/False, addr=...`（无报错）

- [ ] **Step 3: 验证 `_wait_for_ipv6_ready` 默认重试次数为 20**

Run: `python -c "import inspect, core.network; sig = inspect.signature(core.network._wait_for_ipv6_ready); assert sig.parameters['max_retries'].default == 20, f'默认重试次数错误: {sig}'; print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 验证 `run_auth_task` 包含所有关键特性**

Run: `python -c "import inspect, core.auth; src = inspect.getsource(core.auth.run_auth_task); assert 're-authenticating to trigger IPv6' in src, '缺少重新认证回退'; assert '_set_warp_endpoint_ipv6(True)' in src, '缺少清空端点调用'; assert '_set_warp_endpoint_ipv6(False)' in src, '缺少恢复端点调用'; assert 'WARP connection failed. Diagnostics' in src, '缺少诊断日志'; assert 'has_public_ipv6()' in src, '缺少 IPv6 诊断'; print('ALL FEATURES OK')"`
Expected: 输出 `ALL FEATURES OK`

- [ ] **Step 5: 验证安全修复仍有效（无回归）**

Run: `python -c "import inspect, core.auth, core.startup, tray_app, re; assert 'password_prefix' not in inspect.getsource(core.auth); assert '_pwd_encrypted' not in inspect.getsource(core.auth); assert 'os._exit' not in inspect.getsource(core.startup); assert 'os._exit' not in inspect.getsource(tray_app); assert not re.search(r'except\s*:', inspect.getsource(tray_app)); print('SECURITY OK')"`
Expected: 输出 `SECURITY OK`

- [ ] **Step 6: 最终提交（如有清理）**

如果前面步骤中有未提交的清理，在此提交。否则跳过。

```bash
git add -A
git commit -m "refactor: WARP 连接优化完成" || echo "无需提交"
```

---

## 验证总结

### 功能验证清单

完成所有任务后，手动验证以下功能（需要 Windows 环境和管理员权限）：

- [ ] `python tray_app.py` 能正常启动
- [ ] 在 IPv6 正常环境下，认证流程顺利完成，WARP 连接成功
- [ ] 在无 IPv6 环境下，观察日志中是否触发"重新认证以获取IPv6"回退逻辑
- [ ] WARP 连接失败时，日志中是否包含诊断信息（warp-cli status、IPv6 地址、IPv6 路由）
- [ ] 用户取消操作在回退循环中正常工作

### 预期结果对比

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| IPv6 检测方式 | TCP 连接 Cloudflare 端点 | 2001 开头公网 IPv6 地址 |
| IPv6 等待时长 | 约 16 秒（8 次×2 秒） | 约 60 秒（20 次×3 秒） |
| IPv6 不可达处理 | 直接返回失败 | 自动重新认证 2 轮 |
| `_set_warp_endpoint_ipv6` | 死代码（未调用） | 恢复调用 |
| WARP 失败诊断 | 无 | warp-cli status + IPv6 地址 + 路由表 |
| 失败率 | 10-20% | 目标 <5% |
