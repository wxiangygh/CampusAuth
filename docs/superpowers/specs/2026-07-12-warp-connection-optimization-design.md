# WARP 连接失败优化设计

## 背景

CampusAuth 应用在认证流程的最后一步（连接 Cloudflare WARP）存在 10-20% 的失败率。用户遇到失败时需要手动换 WiFi + 注销重新认证，体验不佳。

## 根本原因分析

通过日志分析和代码审查，确认根本原因：

**校园网有时不分配 2001 开头的公网 IPv6 地址。** 当前流程在禁用 IPv4 后依赖 IPv6 连接 WARP，IPv6 不可达时 WARP 的 happy eyeballs 必然失败（IPv4 已禁用、IPv6 也不通）。

具体问题点：

1. **IPv6 就绪检测不足**：`_wait_for_ipv6_ready()` 仅重试 8 次（约 16 秒），通过 TCP 连接 Cloudflare 端点检测，但校园网分配 IPv6 的延迟可能更长。
2. **IPv6 不可达时仍强行连接 WARP**：`run_auth_task` 步骤 5 不检查 IPv6 实际可达性就调用 `connect_warp()`。
3. **回归 bug**：`_set_warp_endpoint_ipv6()`（清空 conf.json 的 IPv4 端点，强制 WARP 走 IPv6）在代码结构重构后成了死代码——仅被 `tray_app.py` 导入，从未调用。重构前的日志显示该函数是被调用的。
4. **WARP 连接失败后无恢复策略**：仅简单重试 2 次，无诊断、无回退。
5. **缺少"IPv6 不可达→重新认证"的回退机制**：用户手动做的事情（注销重认证以触发 IPv6 重新分配）没有被自动化。

## 设计目标

- 将 WARP 连接失败率从 10-20% 降至 5% 以下
- IPv6 不可达时自动尝试恢复，减少用户手动干预
- 修复 `_set_warp_endpoint_ipv6` 回归 bug
- 保留现有 API 接口和 HTML 前端不变

## 方案选择

**方案 A：增强 IPv6 就绪检查 + 自动重新认证**（已选定）

在连接 WARP 前严格验证 IPv6 可达性（2001 开头公网地址）；不可达时自动重新认证以触发 IPv6 分配。

## 详细设计

### 1. 新增 `has_public_ipv6()` 到 `core/network.py`

**功能**：检测本机是否获取到 2001 开头的公网 IPv6 地址。

**实现**：
- 调用 `ipconfig` 命令
- 解析输出，查找 IPv6 地址行
- 过滤条件：以 `2001` 开头（公网 IPv6 地址段）
- 排除 `2001:db8::/32`（文档保留地址）和 `2001::/32`（ORCHIDv1，已废弃）
- 返回 `(bool, str)`：是否找到，找到的第一个地址（或空字符串）

**签名**：
```python
def has_public_ipv6() -> tuple[bool, str]:
    """检测本机是否获取到 2001 开头的公网 IPv6 地址。

    Returns:
        (是否找到, 地址字符串) — 地址为空字符串表示未找到
    """
```

**排除的地址范围**：
- `fe80::/10`（链路本地）
- `fc00::/7`（ULA 本地唯一）
- `::1`（环回）
- `2001:db8::/32`（文档保留）
- `2001::/32`（ORCHIDv1，已废弃）

**保留的地址范围**：
- `2001:200::/23` ~ `2001:da8::/32` 等实际公网 IPv6 段（简化判断：以 `2001` 开头且不在上述排除列表中）

### 2. 重构 `_wait_for_ipv6_ready()`

**改动点**：
- 检测标准从"TCP 连接到 Cloudflare 端点"改为"本机有 2001 开头的公网 IPv6 地址"
- 重试次数：8 → 20（间隔 3 秒，总时长约 60 秒）
- 每次重试记录检测到的 IPv6 地址状态（便于诊断）
- 保留 TCP 连接检测作为辅助验证（可选，不阻塞）

**新签名**：
```python
def _wait_for_ipv6_ready(max_retries=20) -> bool:
    """等待本机获取到 2001 开头的公网 IPv6 地址。

    Args:
        max_retries: 最大重试次数，默认 20（约 60 秒）

    Returns:
        bool: 是否在重试次数内获取到公网 IPv6 地址
    """
```

### 3. `run_auth_task` 步骤 4-5 重构

**当前流程**（步骤 4-5）：
```
步骤 4: 设置 IPv6 DNS
步骤 5: 禁用 IPv4 → _wait_for_ipv6_ready → MASQUE → connect_warp
```

**新流程**（步骤 4-5）：
```
步骤 4: 设置 IPv6 DNS
步骤 5:
  5a. 禁用 IPv4
  5b. 等待 IPv6 就绪（2001 开头，最多 60 秒）
  5c. 如果 IPv6 不可达，进入重新认证回退循环：
      for retry in range(2):
          - 启用 IPv4（临时恢复网络）
          - portal_logout()
          - 等待 2 秒
          - portal_login()
          - 如果 portal_login 失败，终止并返回认证失败
          - 禁用 IPv4
          - 等待 IPv6 就绪（最多 60 秒）
          - 如果 IPv6 可达，跳出循环
      如果 2 轮重新认证后 IPv6 仍不可达：
          - 启用 IPv4（恢复网络）
          - 返回失败："IPv6网络不可用，无法连接WARP（已尝试重新认证）"
  5d. 调用 _set_warp_endpoint_ipv6(True) — 清空 conf.json 的 IPv4 端点
  5e. 设置 MASQUE h3-with-h2-fallback 模式
  5f. connect_warp()
      - 成功：_set_warp_endpoint_ipv6(False) 恢复配置，返回成功
      - 失败：记录诊断日志，_set_warp_endpoint_ipv6(False) 恢复配置，返回失败
```

**进度推送**：
- 5b: `等待IPv6地址...`（status=running）
- 5c 每轮重新认证：
  - `IPv6未就绪，重新认证以获取IPv6（第N轮）...`（status=running）
- 5f: `连接WARP...`（status=running）

### 4. 恢复 `_set_warp_endpoint_ipv6` 调用

**调用位置**：
- `run_auth_task` 步骤 5d：`_set_warp_endpoint_ipv6(True)` — IPv6 可达后，清空 IPv4 端点
- `run_auth_task` 步骤 5f 成功后：`_set_warp_endpoint_ipv6(False)` — 恢复配置
- `run_auth_task` 步骤 5f 失败后：`_set_warp_endpoint_ipv6(False)` — 恢复配置

**导入**：
- `core/auth.py` 顶部添加 `_set_warp_endpoint_ipv6` 到 `from core.warp_manager import (...)` 列表
- 从 `tray_app.py` 的导入中删除 `_set_warp_endpoint_ipv6`（该函数仅在 auth.py 中使用）

### 5. WARP 连接失败诊断

在 `connect_warp()` 失败后（`run_auth_task` 步骤 5f 失败分支），记录以下诊断信息：

```python
logger.error("WARP connection failed. Diagnostics:")
# 1. warp-cli status 全文
code, status_output, _ = run_command([warp_cli, 'status'], shell=False)
logger.error(f"warp-cli status:\n{status_output}")
# 2. 当前 IPv6 地址状态
has_v6, v6_addr = has_public_ipv6()
logger.error(f"Public IPv6: {has_v6}, addr={v6_addr}")
# 3. IPv6 路由表摘要
code, route_output, _ = run_command('netsh interface ipv6 show route')
logger.error(f"IPv6 routes (first 500 chars):\n{route_output[:500]}")
```

这些诊断信息会写入 `tray_app.log`，便于后续问题排查。

## 数据流

```
[认证成功]
  ↓
[步骤 4: 设置 IPv6 DNS]
  ↓
[步骤 5a: 禁用 IPv4]
  ↓
[步骤 5b: 等待 IPv6 (2001 开头, 最多 60s)]
  ↓
[IPv6 可达?]
  ├─ 是 → [步骤 5d: 清空 IPv4 端点] → [步骤 5e: MASQUE 模式] → [步骤 5f: connect_warp]
  │       ├─ 成功 → [恢复 IPv4 端点] → [返回成功]
  │       └─ 失败 → [记录诊断] → [恢复 IPv4 端点] → [返回失败]
  └─ 否 → [步骤 5c: 重新认证回退循环]
          ↓
          [启用 IPv4] → [portal_logout] → [等待 2s] → [portal_login]
          ↓
          [portal_login 成功?]
          ├─ 否 → [返回认证失败]
          └─ 是 → [禁用 IPv4] → [等待 IPv6] → [IPv6 可达?]
                  ├─ 是 → 回到 [步骤 5d]
                  └─ 否 → [重试 < 2?] → 回到 [步骤 5c 循环]
                          ↓
                          [重试 >= 2?] → [启用 IPv4] → [返回失败: IPv6不可用]
```

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| `_wait_for_ipv6_ready` 超时 | 进入重新认证回退循环 |
| `portal_login` 在回退中失败 | 立即终止，返回认证失败，恢复 IPv4 |
| `_set_warp_endpoint_ipv6(True)` 失败 | 记录警告但继续（不阻塞主流程） |
| `connect_warp` 失败 | 记录诊断日志，`_set_warp_endpoint_ipv6(False)` 恢复配置 |
| 用户取消操作 | 各步骤保留 `_check_cancel()` 检查 |

## 变更文件

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `core/network.py` | 新增函数 | `has_public_ipv6()` |
| `core/network.py` | 重构 | `_wait_for_ipv6_ready()` 检测标准改为 2001 开头 |
| `core/auth.py` | 重构 | `run_auth_task` 步骤 5 增加回退循环 |
| `core/auth.py` | 恢复调用 | `_set_warp_endpoint_ipv6(True/False)` |
| `tray_app.py` | 清理导入 | 删除未使用的 `_set_warp_endpoint_ipv6` 导入 |

## 测试策略

### 单元测试
- `has_public_ipv6()` 解析各种 ipconfig 输出格式（有/无 IPv6、临时地址、多种前缀）
- `_wait_for_ipv6_ready()` 在无 IPv6 环境下的超时行为

### 手动测试
- 在无 IPv6 环境下验证回退流程触发重新认证
- 在 IPv6 不稳定环境下验证自动恢复
- 验证诊断日志输出完整
- 验证用户取消操作在回退循环中正常工作

## 非目标

- 不修改 WARP 的 MASQUE 模式选择逻辑（保持 h3-with-h2-fallback）
- 不修改 HTML 前端
- 不修改 ApiBridge 接口
- 不优化 `disconnect_warp` 或 `connect_warp` 内部实现
- 不处理 WiFi 事件触发场景（仅处理认证流程中的 WARP 连接）
