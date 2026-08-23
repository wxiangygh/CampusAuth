# CampusAuth 重构说明

## 目标

本次重构集中解决以下问题：固定且耗时不可控的认证流程、Cloudflare WARP 失败反馈不清晰、窗口和托盘状态不同步、配置并发保存覆盖，以及核心模块反向依赖主程序。

## 新的模块边界

- `core/config.py`：唯一配置仓库。字段级更新、线程锁、修订号、同目录临时文件加 `os.replace` 原子落盘。
- `core/secrets.py`：使用当前 Windows 用户的 DPAPI 加密密码；原明文配置在首次加载时自动迁移。
- `core/app_state.py`：窗口、托盘、认证线程共享的可观察状态快照。
- `core/status.py`：合并重复状态查询，空闲时每 6 秒、操作中每 2 秒刷新，并允许操作完成后立即唤醒。
- `core/workflow.py`：通用工作流执行器，支持开关、排序、单步超时、有限重试、指数退避、取消和逆序回滚。
- `core/auth_workflow.py`：认证动作注册表；只编排低层网络能力，不依赖 UI。
- `core/warp_manager.py`：WARP 状态被分类为 `connected`、`registration_required`、`manual_disconnection`、`no_network`、`cli_error`、`timeout` 等可处理结果。

`core/` 已不再导入 `tray_app`。主程序只负责 Windows 托盘、WebView 桥接和应用生命周期。

## 自定义工作流

设置页可调整认证步骤的顺序、启用状态、超时和重试次数，并可配置 30–300 秒的认证总时限。默认工作流：

1. 检查并连接目标 WiFi
2. 准备校园网认证环境
3. 校园网 Portal 认证
4. 获取并验证公网 IPv6
5. 准备 Cloudflare WARP
6. 连接 Cloudflare WARP
7. 完成与清理

每个步骤的持久化结构如下：

```json
{
  "id": "connect_warp",
  "enabled": true,
  "timeout": 15,
  "retries": 1,
  "retry_delay": 2.0,
  "continue_on_error": false
}
```

启用的最后一步必须是 `finalize`，以保证临时 MASQUE/端点配置得到恢复。失败、取消或总超时会执行逆序回滚，优先恢复 IPv4、DNS 与 WARP 临时设置。

## 配置同步

前端自动保存按提交顺序串行执行，后端返回单调递增修订号。后端只更新本次提交的字段，不再使用“读取整份旧配置、修改、整份覆盖”的方式。窗口位置、UI 偏好、开机自启和认证后 IPv4 偏好也已改为字段级更新。

## 状态同步

认证步骤、恢复操作、后台 WiFi 事件和定期网络探测都写入 `AppStateHub`。托盘和 WebView 订阅同一份带修订号的快照，因此操作进度、最终结果和网络状态不再各自维护。

## 安全验证方式

自动测试全部使用临时目录或模拟命令结果，不触碰网卡、路由、DNS、WARP 服务或 Portal：

```powershell
python -m unittest discover -s tests -v
```

由于真实认证会改变当前网络，本次未自动启动应用。请在可接受短时断网的窗口手动验收：

- 打开设置后修改账号、工作流或 UI 偏好，重新打开窗口确认即时同步。
- 观察认证进度时托盘提示是否同步变化；完成后状态应在数秒内一致。
- 模拟 WARP 未注册、无网络或连接超时，确认提示包含明确处理建议且总时间不超过配置值。
- 在步骤失败或取消后，确认 IPv4、DNS 和 WARP 临时协议设置已回滚。
- 切换目标/非目标 WiFi，确认自动认证和自动恢复各只触发一次。
