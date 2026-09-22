# CampusAuth - 校园网认证助手

Windows 托盘应用，提供校园网自动认证、WARP 排除管理、流量监控等功能。
<img width="1511" height="961" alt="image" src="https://github.com/user-attachments/assets/8127e35c-1d8e-42ce-8a42-50cf63a9b59c" />
<img width="1511" height="961" alt="image" src="https://github.com/user-attachments/assets/69ab23c7-418d-4150-8c00-732d1dec4efa" />
<img width="1511" height="961" alt="image" src="https://github.com/user-attachments/assets/7aca09ac-76f1-4256-a03a-0415124d2f70" />

## 功能

- **校园网自动认证**：检测网络状态并自动登录校园网
- **多工作流管理**：可创建、命名、切换、删除多个独立工作流，并将任意工作流加入托盘菜单
- **细粒度节点编排**：Portal 登录/注销、IPv4/IPv6、WARP 端点、MASQUE、服务启动/停止/重启、连接和状态刷新均可独立组合
- **配置即时记忆**：窗口位置/尺寸、上次标签页、WiFi、服务器、视图偏好等自动保存
- **WARP 排除管理**：配置指定域名不走 WARP，支持 IPv4/IPv6 路由选择
- **流量监控**：实时分析每个 TCP 连接的实际走向（IPv4/IPv6 直连或 WARP 隧道）
- **DNS 设置**：设置页提供阿里云、北京联通、114 DNS、Google、Cloudflare 和自定义 IP 地址。分流解析使用保存的服务器；认证工作流的 IPv6 DNS 单独配置，留空使用自动获取。DNS Fallback 仍遵循本地网络的 DNS。
- **分流列表分页**：域名排除、IP 排除与 DNS Fallback 共用每页条数设置（10/20/50/100），各自保留页码。
- **分流配置兼容模式**：Windows 10 使用应用内弹窗显示当前配置，Windows 11 保留独立窗口；独立窗口创建报错时自动回退到应用内查看。

## 环境要求

- Windows 10/11
- Python 3.12
- [Cloudflare WARP](https://1.1.1.1/) 客户端

## 依赖安装

```powershell
pip install -r requirements.txt
```

## 构建

```powershell
Set-Location d:\project_code\ipv6
pyinstaller CampusAuth.spec --noconfirm
```

构建产物位于 `dist\CampusAuth.exe`。

## 运行

直接运行构建产物：

```powershell
.\dist\CampusAuth.exe
```

或在开发环境中运行：

```powershell
python tray_app.py
```

## 可配置工作流与实时状态

内置“完整校园网认证”“注销并重新认证”“注销校园网”“重启 Cloudflare WARP”等工作流；也可以在设置页把细粒度节点另存为新的独立工作流，并勾选显示到托盘菜单。每个节点可调整顺序、开关、超时、重试、重试延迟和失败后是否继续，工作流总时限可配置。旧版单一 `auth_workflow` 会自动迁移为自定义工作流。

配置采用原子保存与修订号同步，密码使用 Windows DPAPI 加密；窗口与托盘订阅同一实时状态。架构和安全验收说明见 [docs/REFACTORING.md](docs/REFACTORING.md)。
