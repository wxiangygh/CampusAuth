# CampusAuth - 校园网认证助手

Windows 托盘应用，提供校园网自动认证、WARP 排除管理、流量监控等功能。

## 功能

- **校园网自动认证**：检测网络状态并自动登录校园网
- **WARP 排除管理**：配置指定域名不走 WARP，支持 IPv4/IPv6 路由选择
- **流量监控**：实时分析每个 TCP 连接的实际走向（IPv4/IPv6 直连或 WARP 隧道）

## 环境要求

- Windows 10/11
- Python 3.12
- [Cloudflare WARP](https://1.1.1.1/) 客户端（使用 WARP 排除功能时需要）

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

认证流程现在可在设置页调整步骤顺序、开关、超时、重试和总时限。配置采用原子保存与修订号同步，密码使用 Windows DPAPI 加密；窗口与托盘订阅同一实时状态。架构和安全验收说明见 [docs/REFACTORING.md](docs/REFACTORING.md)。