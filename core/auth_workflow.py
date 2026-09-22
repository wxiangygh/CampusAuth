"""Authentication actions plugged into :mod:`core.workflow`."""
from __future__ import annotations

import logging
import time

import core.state
from core.app_state import app_state
from core.command import run_command
from core.dns_settings import dns_settings
from core.config import (
    DEFAULT_AUTH_WORKFLOW, DEFAULT_PORTAL_LOGOUT_WORKFLOW,
    DEFAULT_REAUTH_WORKFLOW, DEFAULT_RESTART_WARP_WORKFLOW,
    DEFAULT_RESTORE_WORKFLOW, get_config,
)
from core.network import (apply_link_exclusive, detect_link_type,
                          get_wifi_interface_name, get_wired_interface_name,
                          has_ipv6_gateway, has_public_ipv6, is_warp_connected,
                          prepare_wifi_connection)
from core.warp_manager import (
    _set_warp_endpoint_ipv6, _set_warp_masque_mode, connect_warp_result,
    disconnect_warp, get_warp_cli,
)
from core.workflow import StepResult, StepSpec, WorkflowContext, WorkflowRunner

logger = logging.getLogger('wifi_tray')

WORKFLOW_CATALOG = {
    'ensure_wifi': {
        'name': '检查并连接目标 WiFi',
        'description': '确认目标 SSID，必要时发起 WiFi 连接。',
        'group': '基础网络',
    },
    'detect_warp_state': {
        'name': '检测 WARP 当前状态',
        'description': '已连接时跳过重复认证准备步骤。',
        'group': '基础网络',
    },
    'disconnect_warp': {
        'name': '断开 Cloudflare WARP',
        'description': '只断开 WARP 连接，不停止或禁用系统服务。',
        'group': 'Cloudflare WARP',
    },
    'prepare_network': {
        'name': '[复合] 准备认证网络',
        'description': '兼容旧流程：断开 WARP、记录服务状态并启用 IPv4。',
        'group': '兼容复合节点',
    },
    'enable_ipv4': {
        'name': '启用 IPv4',
        'description': '在指定 WiFi 接口上启用 IPv4 绑定。',
        'group': 'IPv4 / IPv6',
    },
    'disable_ipv4': {
        'name': '禁用 IPv4',
        'description': '在获取公网 IPv6 后关闭校园网 IPv4。',
        'group': 'IPv4 / IPv6',
    },
    'configure_ipv6_dns': {
        'name': '设置 IPv6 DNS',
        'description': '使用设置页配置的 IPv6 DNS；留空时使用自动获取。',
        'group': 'IPv4 / IPv6',
    },
    'reset_ipv6_dns': {
        'name': '重置 IPv6 DNS 为 DHCP',
        'description': '移除静态 IPv6 DNS，恢复系统自动获取。',
        'group': 'IPv4 / IPv6',
    },
    'wait_public_ipv6': {
        'name': '等待公网 IPv6',
        'description': '轮询本机网卡，直到获得公网 IPv6 或超时。',
        'group': 'IPv4 / IPv6',
    },
    'configure_ipv6': {
        'name': '[复合] 获取并验证公网 IPv6',
        'description': '兼容旧流程：设置 DNS、禁用 IPv4 并等待地址。',
        'group': '兼容复合节点',
    },
    'portal_login': {
        'name': '校园网 Portal 认证',
        'description': '向校园网认证服务器提交账号并解析结果。',
        'group': 'Portal 认证',
        'configurable': True,
        'config_kind': 'portal',
    },
    'portal_logout': {
        'name': '校园网 Portal 注销',
        'description': '按自定义步骤列表依次点击注销页面按钮（如"注销→确定"），步骤间等待页面变化。',
        'group': 'Portal 认证',
        'configurable': True,
        'config_kind': 'portal',
    },
    'set_warp_endpoint_ipv6': {
        'name': '切换 WARP 到 IPv6 端点',
        'description': '备份并清空 Cloudflare WARP 的 IPv4 endpoints。',
        'group': 'Cloudflare WARP',
    },
    'reset_warp_endpoint_ipv6': {
        'name': '恢复 WARP 原始端点',
        'description': '从备份恢复 Cloudflare WARP endpoints。',
        'group': 'Cloudflare WARP',
    },
    'set_warp_masque': {
        'name': '启用 WARP MASQUE',
        'description': '设置 MASQUE 与 h3-with-h2-fallback 隧道协议。',
        'group': 'Cloudflare WARP',
    },
    'reset_warp_masque': {
        'name': '重置 WARP 隧道协议',
        'description': '恢复 Cloudflare WARP 默认隧道协议。',
        'group': 'Cloudflare WARP',
    },
    'warp_underlay_unpin': {
        'name': '移除 WARP 底层 IPv6 pin',
        'description': '删除 CampusAuth_WARPv6Underlay 防火墙规则，恢复 WARP 端点自由选择。',
        'group': 'Cloudflare WARP',
    },
    'start_warp_service': {
        'name': '启动 WARP 服务',
        'description': '设置 CloudflareWARP 自动启动并在未运行时启动。',
        'group': 'Cloudflare WARP',
    },
    'stop_warp_service': {
        'name': '停止 WARP 服务',
        'description': '独立停止 CloudflareWARP 系统服务。',
        'group': 'Cloudflare WARP',
    },
    'restart_warp_service': {
        'name': '重启 WARP 服务',
        'description': '独立停止并启动 CloudflareWARP，可作为单独工作流节点。',
        'group': 'Cloudflare WARP',
    },
    'configure_warp': {
        'name': '[复合] 准备 WARP',
        'description': '兼容旧流程：端点、服务与 MASQUE 一次性配置。',
        'group': '兼容复合节点',
    },
    'connect_warp': {
        'name': '连接 Cloudflare WARP',
        'description': '按官方文档的模式链连接：默认先「流量和DNS」（warp，连通性检查经隧道内 DNS 代理，成功率高），失败再退「仅流量」（tunnel_only）兜底；节点参数 warp_mode 可指定单模式。',
        'group': 'Cloudflare WARP',
    },
    'refresh_status': {
        'name': '刷新网络状态',
        'description': '通知状态监控重新检测当前网络。',
        'group': '状态与清理',
    },
    'finalize': {
        'name': '[复合] 完成与清理',
        'description': '兼容旧流程：恢复 WARP 设置、IPv4 并刷新状态。',
        'group': '状态与清理',
    },
}


def _auth_helpers():
    # Deferred import avoids auth -> auth_workflow -> auth at module import.
    from core.auth import disable_ipv4, enable_ipv4, portal_login, portal_logout
    return disable_ipv4, enable_ipv4, portal_login, portal_logout


def _command_timeout(context: WorkflowContext, maximum=8):
    return max(1, min(maximum, int(context.remaining(maximum))))


def _skip_if_ready(context: WorkflowContext):
    return bool(context.data.get('already_connected'))


def _interface(context: WorkflowContext, step: StepSpec | None = None):
    """选择本节点应操作的网卡，返回 (interface_name, error)。

    解析顺序（后两者支持在节点 params.link_mode 里显式指定，实现按节点
    灵活控制走有线/无线，例如恢复流程中的「启用 IPv4」强制走有线）：
    1. 节点 params.link_mode 显式为 wired/wireless → 直接选对应网卡；
    2. context.data['link_type']（工作流预扫描的链路类型）→ wired 用有线
       网卡，无线/未指定沿用 WiFi 网卡逻辑（兼容旧工作流）。
    """
    params = getattr(step, 'params', None) or {}
    link_mode = str(params.get('link_mode') or 'auto').strip().lower()
    if link_mode == 'wired':
        interface_name = get_wired_interface_name()
        if not interface_name:
            return None, StepResult.fail('未找到有线网卡', code='interface_missing',
                                         retryable=True)
        context.data['interface_name'] = interface_name
        return interface_name, None
    if link_mode == 'wireless':
        interface_name = get_wifi_interface_name()
        if not interface_name:
            interface_name = get_wired_interface_name()
            if not interface_name:
                return None, StepResult.fail(
                    '未找到可用网卡：无线接口不存在，也未检测到有线网卡',
                    code='interface_missing', retryable=True)
            logger.info('[interface] 指定无线但系统无可用 WLAN，按有线兜底：%s', interface_name)
        context.data['interface_name'] = interface_name
        return interface_name, None
    # auto：按工作流预扫描的链路类型
    if context.data.get('link_type') == 'wired':
        interface_name = get_wired_interface_name()
        if not interface_name:
            return None, StepResult.fail('未找到有线网卡', code='interface_missing',
                                         retryable=True)
    else:
        interface_name = context.data.get('interface_name') or get_wifi_interface_name()
        if not interface_name:
            # 链路被判为 wireless 但系统没有可用 WLAN 接口（纯有线机器
            # 检测失误时的自愈）：按有线兜底，而不是报"无法获取 WiFi 接口名称"
            interface_name = get_wired_interface_name()
            if not interface_name:
                return None, StepResult.fail(
                    '未找到可用网卡：无线接口不存在，也未检测到有线网卡',
                    code='interface_missing', retryable=True)
            logger.info('[interface] 链路判定 wireless 但无可用 WLAN，按有线兜底：%s',
                        interface_name)
    context.data['interface_name'] = interface_name
    return interface_name, None


def _reset_ipv6_dns_command(interface_name: str, timeout: float = 5):
    return run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp',
                       timeout=timeout)


def _ensure_wifi(context: WorkflowContext, step: StepSpec) -> StepResult:
    # 有线链路下 WiFi 连接节点无意义：直接跳过（不算失败，链路隔离已保证走有线）
    if context.data.get('link_type') == 'wired':
        return StepResult.ok('有线链路，跳过 WiFi 连接')
    wifi_name = str(context.config.get('wifi_name', '')).strip()
    if not wifi_name:
        return StepResult.fail('WiFi 名称未配置', code='wifi_not_configured')
    if not context.config.get('username') or not context.config.get('password'):
        return StepResult.fail('账号或密码未配置', code='credentials_missing')
    timeout = _command_timeout(context)
    code, output, error = run_command(['netsh', 'wlan', 'show', 'interfaces'], shell=False, timeout=timeout)
    connected = wifi_name in output and ('已连接' in output or 'connected' in output.lower())
    if connected:
        return StepResult.ok(f'已连接 {wifi_name}')
    # 连接前的准备：网卡被禁用则启用、软件无线电关闭则自动打开
    # （飞行模式/Fn 键软关是 netsh wlan connect 报"无线电已关闭"直接失败的常见原因）
    ok, prepare_msg = prepare_wifi_connection(timeout=15)
    if not ok:
        return StepResult.fail(f'WiFi 准备失败：{prepare_msg}', code='wifi_prepare_failed')
    code, output, error = run_command(['netsh', 'wlan', 'connect', f'name={wifi_name}'], shell=False, timeout=timeout)
    if code != 0:
        detail = (error or output).strip()[:160] or f'返回码 {code}'
        return StepResult.fail(f'WiFi 连接失败：{detail}', code='wifi_connect_failed', retryable=True)
    while context.remaining() > 0.5 and not context.cancelled():
        if not _wait(context, 0.8):
            break
        _, output, _ = run_command(['netsh', 'wlan', 'show', 'interfaces'], shell=False,
                                   timeout=_command_timeout(context, 4))
        if wifi_name in output and ('已连接' in output or 'connected' in output.lower()):
            return StepResult.ok(f'已连接 {wifi_name}')
    return StepResult.fail(f'连接后仍未检测到 {wifi_name}', code='wifi_not_ready', retryable=True)


def _detect_warp_state(context: WorkflowContext, step: StepSpec) -> StepResult:
    if context.data.get('strict_full_run'):
        # 严格模式：不做"WARP 已连接就跳过准备步骤"的优化，完整重走认证流程。
        # 调用方（手动点击认证、开机与 WiFi 事件守卫）已经判定本次需要认证；
        # 若此处再根据连接状态跳过，上次连接失败回滚后残留的
        # "IPv4 未禁用 + WARP 经 IPv4 连接"状态将永远得不到修复
        # （2026-09-01 日志中该状态就一路沿用下来）。
        context.data['already_connected'] = False
        return StepResult.ok('严格模式：完整重走认证流程')
    connected = is_warp_connected()
    context.data['already_connected'] = connected
    return StepResult.ok('WARP 已连接，将跳过重复准备步骤' if connected else 'WARP 未连接，继续完整流程')


def _disconnect_warp_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，保持当前连接')
    ok = disconnect_warp(full=False, timeout=_command_timeout(context, 8))
    if context.cancelled():
        return StepResult.fail('已取消', code='cancelled')
    return StepResult.ok('WARP 已断开') if ok else StepResult.fail('WARP 断开失败',
                                                                    code='warp_disconnect_failed',
                                                                    retryable=True)


def _prepare_network(context: WorkflowContext, step: StepSpec) -> StepResult:
    disable_ipv4, enable_ipv4, _, _ = _auth_helpers()
    interface_name, error = _interface(context, step)
    if error:
        return error
    if is_warp_connected() and not context.data.get('strict_full_run'):
        context.data['already_connected'] = True
        return StepResult.ok('WARP 已连接，跳过重复认证')
    disconnect_warp(full=False, timeout=_command_timeout(context, 5))
    _, service_output, _ = run_command('sc query "CloudflareWARP"', timeout=_command_timeout(context, 5))
    context.data['warp_service_was_running'] = 'RUNNING' in service_output
    if not enable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
        return StepResult.fail('无法启用校园网 IPv4', code='enable_ipv4_failed')

    def restore_ipv4():
        enable_ipv4(interface_name)
        _reset_ipv6_dns_command(interface_name)
    context.add_rollback('restore_ipv4', restore_ipv4)
    context.data['ipv4_rollback_registered'] = True
    return StepResult.ok('认证网络环境已准备')


def _enable_ipv4_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    _, enable_ipv4, _, _ = _auth_helpers()
    if _skip_if_ready(context) and not context.data.get('warp_connected'):
        return StepResult.ok('WARP 已连接，跳过准备阶段启用 IPv4')
    interface_name, error = _interface(context, step)
    if error:
        return error
    if not enable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
        return StepResult.fail('无法启用 IPv4', code='enable_ipv4_failed')
    context.remove_rollback('restore_ipv4')
    return StepResult.ok('IPv4 已启用')


def _disable_ipv4_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，跳过禁用 IPv4')
    disable_ipv4, _, _, _ = _auth_helpers()
    interface_name, error = _interface(context, step)
    if error:
        return error
    if not disable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
        return StepResult.fail('禁用 IPv4 失败', code='disable_ipv4_failed')

    def restore_ipv4():
        _, enable_ipv4, _, _ = _auth_helpers()
        enable_ipv4(interface_name)
    context.add_rollback('restore_ipv4', restore_ipv4)
    return StepResult.ok('IPv4 已禁用')


def _configure_ipv6_dns_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，跳过 IPv6 DNS 设置')
    interface_name, error = _interface(context, step)
    if error:
        return error
    servers = dns_settings(get_config())['ipv6_servers']
    if not servers:
        return _reset_ipv6_dns_action(context, step)
    primary = f'netsh interface ipv6 set dnsservers "{interface_name}" static {servers[0]} primary validate=no'
    code, _, error_text = run_command(primary, timeout=_command_timeout(context, 5))
    if code != 0:
        return StepResult.fail(f'IPv6 DNS 设置失败：{error_text.strip()[:120]}',
                               code='ipv6_dns_failed', retryable=True)
    def restore_dns():
        _reset_ipv6_dns_command(interface_name)
    context.add_rollback('restore_ipv6_dns', restore_dns)
    for index, server in enumerate(servers[1:], 2):
        secondary = f'netsh interface ipv6 add dnsservers "{interface_name}" {server} index={index} validate=no'
        code, _, error_text = run_command(secondary, timeout=_command_timeout(context, 5))
        if code != 0:
            return StepResult.fail(f'备用 IPv6 DNS 设置失败：{error_text.strip()[:120]}',
                                   code='ipv6_dns_failed', retryable=True)
    return StepResult.ok('IPv6 DNS 已设置')


def _reset_ipv6_dns_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    interface_name, error = _interface(context, step)
    if error:
        return error
    code, _, error_text = _reset_ipv6_dns_command(
        interface_name, timeout=_command_timeout(context, 5))
    context.remove_rollback('restore_ipv6_dns')
    if code != 0:
        return StepResult.fail(f'IPv6 DNS 重置失败：{error_text.strip()[:120]}',
                               code='ipv6_dns_reset_failed', retryable=True)
    return StepResult.ok('IPv6 DNS 已恢复 DHCP')


def _wait_public_ipv6_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('沿用当前 WARP 网络')
    while context.remaining() > 0.2 and not context.cancelled():
        found, address = has_public_ipv6()
        if found:
            context.data['ipv6_address'] = address
            return StepResult.ok(f'公网 IPv6 已就绪：{address}')
        if not _wait(context, 1.2):
            break
    if context.cancelled():
        return StepResult.fail('已取消', code='cancelled')
    # 超时后区分网络侧的两种形态：完全没发 IPv6 vs 路由器宣告了网关但不分配
    # 地址（后者校园 AP 上游断供时常见，认证页仍会显示历史租约的 IPv6）
    try:
        gateway_ok, _gateway = has_ipv6_gateway()
    except Exception:
        logger.exception('has_ipv6_gateway failed')
        gateway_ok = False
    if gateway_ok:
        return StepResult.fail(
            '已收到 IPv6 路由器宣告，但该网络未分配公网 IPv6 地址（前缀/DHCPv6 无下发，'
            '多为此网络上游 IPv6 断供；认证页显示的 IPv6 可能是历史租约）',
            code='ipv6_timeout', retryable=True)
    return StepResult.fail('在限定时间内未获取到公网 IPv6', code='ipv6_timeout', retryable=True)


def _server_to_ip_port(server: str) -> dict:
    """把 "ip:port" / "域名:port" / "域名" 服务器串拆成覆盖项；空串返回 {}。

    端口可选：不带端口时 portal_port 置空（core.auth 会直接用 host 拼地址），
    同时避免全局 portal_port 误加到用户填的域名上。
    """
    server = str(server or '').strip()
    if not server:
        return {}
    if '://' in server:
        server = server.split('://', 1)[1]
    server = server.split('/', 1)[0].strip()
    if not server:
        return {}
    if ':' in server:
        ip, _, port = server.rpartition(':')
        return {'portal_ip': ip.strip(), 'portal_port': port.strip()}
    return {'portal_ip': server, 'portal_port': ''}


def _portal_variant_configured(variant) -> bool:
    """按认证方式判断变体是否实际配置过。

    web：认证网址 / 按钮名 / 点击步骤 任一非空；http：服务器非空
    （http 的空服务器表示"回退全局服务器"，在自动检测场景视为未配置，
     否则自动检测会选中一套空 HTTP 配置跑错方式——正是"自动检测不生效"
     的根因：用户配置在 wired 变体，运行时选中了只有旧按钮名的 wireless）。
    """
    if not isinstance(variant, dict):
        return False
    method = str(variant.get('method') or 'http').lower()
    if method == 'web':
        return bool(str(variant.get('auth_url') or '').strip()
                    or str(variant.get('button_name') or '').strip()
                    or variant.get('click_steps'))
    return bool(str(variant.get('server') or '').strip())


def _resolve_portal_params(step: StepSpec, context, *,
                           follow_current_link: bool = False) -> dict:
    """按 link_mode 选出有线/无线变体并补齐默认值。

    - link_mode 显式 wired/wireless 时优先采用（两套都配置时起区分作用）；
    - auto 时优先用工作流预扫描写入 context.data['link_type'] 的结果，
      避免同一工作流内二次探测导致前后不一致；无预扫描再现场检测；
    - follow_current_link=True（Portal 注销专用）：无视 link_mode 与预扫描，
      一律现场检测「当前实际链路」并选其变体——Portal 会话绑定在当前链路
      的 IP 上，注销当前会话只能用当前链路的注销配置（用户有线在线时选了
      无线工作流，注销执行的也必须是有线的注销）；
    - 选中的变体缺失**或实际未配置**（空 HTTP/无网址无按钮无步骤）时回退
      另一套已配置变体——不论 link_mode 是否显式。"自动检测选中一套空
      配置"正是回退要修的 bug（用户配置在有线，无线只有旧按钮名残留，
      运行时被跑成对全局服务器的 HTTP 注销超时）；
    - method 缺省为 http、server 缺省为空（由调用方回退全局 portal_ip/portal_port），
      因此空 params 的内置工作流行为与改造前完全一致。
    """
    params = getattr(step, 'params', None) or {}
    data = getattr(context, 'data', None) or {}
    link_mode = str(params.get('link_mode') or 'auto').strip().lower()
    if follow_current_link:
        link = None  # 统一走下方现场检测
    elif link_mode in ('wired', 'wireless'):
        link = link_mode
    else:
        link = data.get('link_type')
        if link not in ('wired', 'wireless'):
            link = None
    if link is None:
        try:
            link = detect_link_type()
        except Exception:
            logger.exception('detect_link_type failed, fallback to wireless')
            link = 'wireless'
    variant = params.get(link) if isinstance(params.get(link), dict) else None
    if not _portal_variant_configured(variant):
        other_link = 'wireless' if link == 'wired' else 'wired'
        other = params.get(other_link)
        if _portal_variant_configured(other):
            logger.info('[portal] 链路 %s 的变体未配置，回退使用 %s 变体配置',
                        link, other_link)
            variant = other
    if not isinstance(variant, dict):
        variant = {}
    method = str(variant.get('method') or 'http').lower()
    if method not in ('web', 'http'):
        method = 'http'
    from core.portal_web import normalize_click_steps
    click_steps = normalize_click_steps(variant.get('click_steps'))
    resolved = {
        'link': link,
        'method': method,
        'auth_url': str(variant.get('auth_url') or '').strip(),
        'button_name': str(variant.get('button_name') or '').strip(),
        'click_steps': click_steps,
        'user_selector': str(variant.get('user_selector') or '').strip(),
        'pass_selector': str(variant.get('pass_selector') or '').strip(),
        'success_keyword': str(variant.get('success_keyword') or '').strip(),
        'fail_keyword': str(variant.get('fail_keyword') or '').strip(),
        'server': str(variant.get('server') or '').strip(),
    }
    logger.info('[portal] 解析配置：链路=%s%s 方法=%s 网址=%s 点击步骤=%d',
                resolved['link'], '（按当前链路）' if follow_current_link else '',
                resolved['method'],
                resolved['auth_url'] or '(空)', len(click_steps))
    return resolved


def _portal_login(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，无需重复 Portal 认证')
    _, _, portal_login, portal_logout = _auth_helpers()
    # 传 context（而非 context.config）：auto 模式优先用预扫描的链路类型，
    # 与 IPv4/IPv6 等节点的选卡逻辑保持一致，避免二次探测结果漂移
    resolved = _resolve_portal_params(step, context)
    if resolved['method'] == 'web':
        from core.portal_web import web_portal_submit
        success, message = web_portal_submit(
            resolved['auth_url'], context.config.get('username', ''),
            context.config.get('password', ''), resolved['button_name'],
            user_selector=resolved['user_selector'], pass_selector=resolved['pass_selector'],
            success_keyword=resolved['success_keyword'], fail_keyword=resolved['fail_keyword'],
            click_steps=resolved['click_steps'],
            timeout=min(step.timeout, context.remaining()), cancelled=context.cancelled)
    else:
        effective = {**context.config, **_server_to_ip_port(resolved['server'])}
        if context.current_attempt > 1:
            portal_logout(effective, timeout=min(4, context.remaining()))
        success, message = portal_login(effective, timeout=min(8, context.remaining()))
    if context.cancelled():
        return StepResult.fail('已取消', code='cancelled')
    if success:
        return StepResult.ok(message)
    retryable = any(token in message for token in
                    ('暂时不可用', '连接失败', 'AC认证失败', 'HTTP 5', '请求异常',
                     '打开认证网页失败', '无法交互', '未加载完成'))
    return StepResult.fail(message, code='portal_failed', retryable=retryable)


def _apply_deferred_link_exclusive(context: WorkflowContext) -> None:
    """注销完成后应用被推迟的链路隔离（见 run_auth_workflow 的推迟逻辑）。

    纯记录型：失败只记警告，不抛异常——注销已成功，认证节点的错误信息
    足以暴露链路不对的问题。
    """
    pending = context.data.pop('pending_link_exclusive', None)
    if pending not in ('wired', 'wireless'):
        return
    if context.cancelled():
        # 运行已取消：不再切换网卡，把机器留在当前链路上
        logger.info('运行已取消，丢弃被推迟的链路隔离（%s）', pending)
        return
    try:
        ok, msg = apply_link_exclusive(pending)
    except Exception:
        logger.exception('注销后应用链路隔离（%s）失败', pending)
        return
    logger.info('注销完成，应用链路隔离（%s）：%s', pending, msg)
    if not ok:
        logger.warning('注销后链路隔离失败：%s（后续节点按现状执行）', msg)


def _portal_logout_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    _, _, _, portal_logout = _auth_helpers()
    # 注销必须注销「当前正在使用的链路」上的会话：Portal 会话绑定当前链路
    # 的 IP，用工作流指定的另一条链路的变体去注销只会失败。因此这里无视
    # link_mode，一律按现场检测的当前链路选变体（有线在线 + 无线工作流
    # = 执行有线的注销，反之同理）。
    resolved = _resolve_portal_params(step, context, follow_current_link=True)
    try:
        if resolved['method'] == 'web':
            from core.portal_web import web_portal_submit
            success, _message = web_portal_submit(
                resolved['auth_url'], context.config.get('username', ''),
                context.config.get('password', ''), resolved['button_name'],
                user_selector=resolved['user_selector'], pass_selector=resolved['pass_selector'],
                success_keyword=resolved['success_keyword'], fail_keyword=resolved['fail_keyword'],
                click_steps=resolved['click_steps'],
                timeout=min(step.timeout, context.remaining()), cancelled=context.cancelled)
        else:
            effective = {**context.config, **_server_to_ip_port(resolved['server'])}
            success = portal_logout(effective, timeout=min(6, context.remaining()))
        if context.cancelled():
            return StepResult.fail('已取消', code='cancelled')
        if success:
            return StepResult.ok('Portal 已注销')
        # Logout is best-effort in a re-auth workflow; login can still replace the session.
        return StepResult.ok('Portal 注销未确认，继续执行后续步骤', code='logout_unconfirmed')
    finally:
        # 「注销 + 重新认证」且指定链路 ≠ 当前链路时，隔离被推迟到这里执行：
        # 注销先走当前链路，随后再切到工作流指定的链路做认证
        _apply_deferred_link_exclusive(context)


def _configure_ipv6(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('沿用当前 WARP 网络')
    dns_result = _configure_ipv6_dns_action(context, step)
    if not dns_result.success:
        return dns_result
    disable_result = _disable_ipv4_action(context, step)
    if not disable_result.success:
        return disable_result
    return _wait_public_ipv6_action(context, step)


def _resolve_warp_cli(context: WorkflowContext):
    warp_cli = context.data.get('warp_cli') or get_warp_cli()
    if not warp_cli:
        return None, StepResult.fail('未找到 Cloudflare WARP，请检查安装或 warp-cli 路径',
                                     code='warp_cli_missing')
    context.data['warp_cli'] = warp_cli
    return warp_cli, None


def _set_warp_endpoint_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，沿用当前端点配置')
    if not _set_warp_endpoint_ipv6(True):
        return StepResult.fail('WARP IPv6 端点切换失败', code='warp_endpoint_failed',
                               retryable=True)
    context.data['warp_endpoint_configured'] = True
    context.add_rollback('restore_warp_endpoint', lambda: _set_warp_endpoint_ipv6(False))
    return StepResult.ok('WARP 已切换到 IPv6 端点')


def _reset_warp_endpoint_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if not _set_warp_endpoint_ipv6(False):
        return StepResult.fail('WARP 原始端点恢复失败', code='warp_endpoint_reset_failed',
                               retryable=True)
    context.remove_rollback('restore_warp_endpoint')
    context.data['warp_endpoint_configured'] = False
    return StepResult.ok('WARP 端点已恢复')


def _set_warp_masque_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，沿用当前隧道协议')
    warp_cli, error = _resolve_warp_cli(context)
    if error:
        return error
    if not _set_warp_masque_mode(warp_cli, True, timeout=_command_timeout(context, 5)):
        return StepResult.fail('WARP MASQUE 启用失败', code='warp_masque_failed')
    context.data['warp_masque_configured'] = True
    context.add_rollback('restore_warp_masque',
                         lambda: _set_warp_masque_mode(warp_cli, False))
    return StepResult.ok('WARP MASQUE 已启用')


def _reset_warp_masque_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    warp_cli, error = _resolve_warp_cli(context)
    if error:
        return error
    if not _set_warp_masque_mode(warp_cli, False, timeout=_command_timeout(context, 5)):
        return StepResult.fail('WARP 隧道协议重置失败', code='warp_masque_reset_failed')
    context.remove_rollback('restore_warp_masque')
    context.data['warp_masque_configured'] = False
    return StepResult.ok('WARP 隧道协议已重置')


def _warp_underlay_unpin_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    """恢复正常模式时移除 WARP 底层 IPv6 pin（防火墙程序级规则）。

    经工作流执行的恢复（restore_button_workflow 绑定）必须走这一步，
    否则日后 WARP 再次连接时仍被强制锁定在 IPv6 端点上。
    """
    try:
        from warp_exclusion import remove_warp_underlay_ipv6_pin, is_warp_underlay_pinned
        if not is_warp_underlay_pinned():
            return StepResult.ok('WARP 底层 pin 不存在，无需移除')
        ok, msg = remove_warp_underlay_ipv6_pin()
        if not ok:
            return StepResult.fail(f'移除 WARP 底层 pin 失败：{msg}',
                                   code='warp_unpin_failed', retryable=True)
        return StepResult.ok('WARP 底层 IPv6 pin 已移除')
    except Exception as exc:
        logger.warning(f'warp_underlay_unpin error: {exc}')
        return StepResult.ok('WARP 底层 pin 状态未知，已跳过')


def _query_service(context: WorkflowContext):
    return run_command('sc query "CloudflareWARP"', timeout=_command_timeout(context, 5))


def _control_warp_service(context: WorkflowContext, action: str) -> StepResult:
    if action not in {'start', 'stop', 'restart'}:
        return StepResult.fail(f'未知服务操作: {action}', code='unknown_service_action')
    if action in {'start', 'restart'}:
        run_command('sc config "CloudflareWARP" start= auto',
                    timeout=_command_timeout(context, 5))
    _, before, _ = _query_service(context)
    was_running = 'RUNNING' in before
    if action in {'stop', 'restart'} and was_running:
        code, output, error = run_command('net stop "CloudflareWARP"',
                                          timeout=_command_timeout(context, 8))
        if code != 0:
            return StepResult.fail(f'Cloudflare WARP 服务停止失败：{(error or output).strip()[:140]}',
                                   code='warp_service_stop_failed', retryable=True)
        if action == 'stop':
            # 与 disconnect_warp(full=True) 对齐：恢复正常模式后
            # WARP 不应再随系统自启；start/restart 分支会重新设回 auto
            run_command('sc config "CloudflareWARP" start= disabled',
                        timeout=_command_timeout(context, 5))
        if not _wait(context, 0.8):
            return StepResult.fail('已取消', code='cancelled')
    if action in {'start', 'restart'}:
        _, current, _ = _query_service(context)
        if 'RUNNING' not in current:
            code, output, error = run_command('net start "CloudflareWARP"',
                                              timeout=_command_timeout(context, 8))
            if code != 0:
                return StepResult.fail(f'Cloudflare WARP 服务启动失败：{(error or output).strip()[:140]}',
                                       code='warp_service_start_failed', retryable=True)
            if not _wait(context, 0.8):
                return StepResult.fail('已取消', code='cancelled')
    context.data['warp_service_controlled'] = True
    labels = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    return StepResult.ok(f'Cloudflare WARP 服务已{labels[action]}')


def _start_warp_service(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，跳过服务启动')
    return _control_warp_service(context, 'start')


def _stop_warp_service(context: WorkflowContext, step: StepSpec) -> StepResult:
    return _control_warp_service(context, 'stop')


def _restart_warp_service(context: WorkflowContext, step: StepSpec) -> StepResult:
    return _control_warp_service(context, 'restart')


def _configure_warp(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('沿用当前 WARP 配置')
    endpoint_result = _set_warp_endpoint_action(context, step)
    if not endpoint_result.success:
        return endpoint_result
    service_result = _start_warp_service(context, step)
    if not service_result.success:
        return service_result
    masque_result = _set_warp_masque_action(context, step)
    if not masque_result.success:
        return masque_result
    context.data['warp_configured'] = True
    return StepResult.ok('WARP 连接参数已准备')


# 「仅流量」（tunnel_only）兜底模式的本网络失败记忆：网络指纹 → 失败时刻。
# 官方机制：Traffic only 的连通性检查依赖本地 DNS 代理的隧道外 DoH 直连，
# 校园网封锁该直连时此模式永远卡 Connecting。确认失败一次后 6 小时内不再
# 浪费兜底预算（换网络后指纹变化，记忆自动失效）。
_TUNNEL_ONLY_FAILED_AT: dict[str, float] = {}
_TUNNEL_ONLY_RETRY_AFTER = 6 * 3600.0


def _network_fingerprint() -> str:
    """当前网络的粗略指纹：公网 IPv6 的 /64 前缀（跨网络会变化）。"""
    try:
        from core.network import has_public_ipv6
        _, addr = has_public_ipv6()
        if addr:
            return 'v6:' + ':'.join(str(addr).split(':')[:4])
    except Exception:
        pass
    return 'unknown'


def _tunnel_only_known_bad() -> bool:
    failed_at = _TUNNEL_ONLY_FAILED_AT.get(_network_fingerprint())
    return failed_at is not None and (time.monotonic() - failed_at) < _TUNNEL_ONLY_RETRY_AFTER


def _mark_tunnel_only_failed() -> None:
    _TUNNEL_ONLY_FAILED_AT[_network_fingerprint()] = time.monotonic()


def _mark_tunnel_only_ok() -> None:
    _TUNNEL_ONLY_FAILED_AT.pop(_network_fingerprint(), None)


def _connect_warp(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        context.data['warp_connected'] = True
        return StepResult.ok('WARP 已连接')
    # 官方文档结论（Cloudflare One Client: Client modes / Known limitations）：
    # 客户端只有在本机 DNS 代理（127.0.2.2/3）成功解析专用主机
    # connectivity-check.warp-svc 并通过连通性检查后才显示「已连接」。
    # - warp（Traffic and DNS）：本地 DNS 代理的 DoH 上游经隧道转发，隧道一建
    #   立检查即可通过 → 校园网封锁 DoH 直连（162.159.36.x:443，见
    #   core/network.py 注释）时唯一可靠的连接模式；
    # - tunnel_only（Traffic only）：DNS 留在系统解析器，但连通性检查仍必须经
    #   本地 DNS 代理，其 DoH 上游走隧道外直连 → 被校园网封锁时检查永远无法
    #   通过，卡在 Connecting 直至超时（2026-09-06 多次实测，与官方机制一致）。
    # 因此默认 auto：先 warp（高成功率），失败再退 tunnel_only 兜底。
    warp_cli, cli_error = _resolve_warp_cli(context)
    mode_pref = str(step.params.get('warp_mode')
                    or context.config.get('warp_connect_mode') or 'auto').strip().lower()
    if mode_pref not in ('auto', 'warp', 'tunnel_only'):
        mode_pref = 'auto'
    modes = {'warp': ['warp'], 'tunnel_only': ['tunnel_only']}.get(
        mode_pref, ['warp', 'tunnel_only'])
    if cli_error is not None:
        modes = modes[:1]  # 找不到 warp-cli 时两种模式结局相同，不必重试
    mode_labels = {'warp': '流量和DNS', 'tunnel_only': '仅流量'}
    # 底层 IPv6 pin 必须在连接「前」生效：finalize 才建 pin 的话，IPv4 在线的
    # 首轮连接里 warp-svc 会直接挑 engage 的 IPv4 端点（A 记录），隧道走 IPv4。
    # 提前建好（幂等），warp-svc 只能走 IPv6 端点；IPv4 保持在线还保住了
    # 系统 DNS。范围不含 DoH（162.159.36.x），不影响本地 DNS 代理的上游。
    if context.config.get('warp_underlay_ipv6', True):
        try:
            from warp_exclusion import ensure_warp_underlay_ipv6_pin
            ok, pin_msg = ensure_warp_underlay_ipv6_pin()
            if ok:
                logger.info('[connect_warp] %s', pin_msg)
            else:
                logger.warning('[connect_warp] IPv6 pin 建立失败：%s（按当前底层继续连接）', pin_msg)
        except Exception as exc:
            logger.warning('[connect_warp] IPv6 pin error: %s', exc)
    total_budget = max(5.0, min(float(step.timeout), max(1.0, context.remaining())))
    last_result = None
    for idx, mode in enumerate(modes):
        if context.cancelled():
            return StepResult.fail('已取消', code='cancelled')
        label = mode_labels[mode]
        # auto 链兜底记忆：本网络已确认「仅流量」无法通过连通性检查 → 直接跳过
        if mode == 'tunnel_only' and mode_pref == 'auto' and _tunnel_only_known_bad():
            logger.info('[connect_warp] 跳过兜底「仅流量」模式：本网络此前已确认其连通性检查无法通过（DoH 直连被封）')
            continue
        if cli_error is None:
            code, _, err = run_command([warp_cli, 'mode', mode],
                                       shell=False, timeout=_command_timeout(context, 8))
            if code == 0:
                logger.info('[connect_warp] WARP 模式已切换：%s（%s）', label, mode)
            else:
                logger.warning('[connect_warp] 切换 %s 失败（%s），按当前模式继续连接',
                               label, (err or '').strip()[:120])
        if len(modes) == 1:
            budget = total_budget
        elif idx == 0:
            # 首选模式一次给够：总预算的 8 成（预留 ~12s 给兜底），不设 30s 上限。
            # 冷启动的隧道握手 + 隧道内连通性检查可能要 60s+，短预算截断会把
            # 快要完成的握手掐掉重来，反而拖长总时间（2026-09-06 日志实测）
            budget = min(total_budget,
                         max(15.0, min(total_budget - 12.0, total_budget * 0.8)))
        else:
            # 兜底只给 10s：机制性失败的网络里长预算只是白等；
            # 真正可用的网络上 10s 也足以完成握手
            budget = max(5.0, min(10.0, min(float(step.timeout), max(1.0, context.remaining()))))
        result = connect_warp_result(timeout=budget, max_attempts=1)
        if result.success:
            context.data['warp_connected'] = True
            if mode == 'tunnel_only' and mode_pref == 'auto':
                _mark_tunnel_only_ok()
            return StepResult.ok(f'{result.message}（{label}模式）',
                                 attempts=result.attempts, elapsed=result.elapsed)
        logger.warning('[connect_warp] %s 模式连接失败（预算 %.0fs）：%s',
                       label, budget, result.message[:120])
        last_result = result
        # 兜底失败且有真实等待（≥8s）才算确认：塌缩预算下的失败不作数
        if mode == 'tunnel_only' and mode_pref == 'auto' and budget >= 8.0:
            _mark_tunnel_only_failed()
        # 每次换模式前断开残留连接，避免上一次的半开隧道干扰下一次模式
        if cli_error is None:
            run_command([warp_cli, 'disconnect'], shell=False,
                        timeout=_command_timeout(context, 5))
    # 全部模式失败：中止可能仍在后台建立的连接。否则随后的回滚会恢复 IPv4
    # 端点并重新启用 IPv4，WARP 会在几秒后经 IPv4 连上，留下
    # "IPv4 开启 + WARP 走 IPv4"的脏状态（2026-09-01 日志即此情形）
    result = last_result
    if result is None:
        return StepResult.fail('无法连接 WARP', code='warp_connect_failed', retryable=True)
    return StepResult.fail(result.message, code=result.code, retryable=result.retryable,
                           attempts=result.attempts, elapsed=result.elapsed,
                           status_output=result.status_output[:200])


def _refresh_status_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    try:
        from core.status import network_status
        # 工作流刚改过网卡/WARP/防火墙：清探测缓存再刷新，状态立即跟上
        network_status.invalidate()
        network_status.request_refresh()
    except Exception:
        logger.exception('Could not request status refresh')
    return StepResult.ok('网络状态刷新已请求')


def _finalize(context: WorkflowContext, step: StepSpec) -> StepResult:
    disable_ipv4, enable_ipv4, _, _ = _auth_helpers()
    if context.data.get('warp_configured'):
        warp_cli = context.data.get('warp_cli')
        _set_warp_masque_mode(warp_cli, False, timeout=_command_timeout(context, 4))
        _set_warp_endpoint_ipv6(False)
    # 常驻 pin：阻止 warp-svc.exe 访问 Cloudflare 端点的 IPv4 地址段，
    # 防止 WARP 后续自行重连时（IPv4 已启用）底层从 IPv6 切到 IPv4。
    # 端点配置（conf.json）在连接后即恢复，无法约束未来的重连，必须靠
    # 这条程序级防火墙规则兜底。失败不阻断主流程；受 warp_underlay_ipv6 开关控制。
    if get_config().get('warp_underlay_ipv6', True):
        try:
            from warp_exclusion import ensure_warp_underlay_ipv6_pin
            ok, msg = ensure_warp_underlay_ipv6_pin()
            if not ok:
                logger.warning(f'WARP underlay IPv6 pin failed in finalize: {msg}')
        except Exception as exc:
            logger.warning(f'WARP underlay IPv6 pin error in finalize: {exc}')
    # finalize 兜底恢复 IPv4：优先用工作流中已确定的网卡，否则按链路类型现取
    # （单独运行收尾节点时 data 里可能没有 interface_name）
    if context.data.get('interface_name'):
        interface_name = context.data['interface_name']
    else:
        interface_name, _error = _interface(context, step)
    if interface_name and context.config.get('auto_enable_ipv4', True):
        if not enable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
            return StepResult.fail('WARP 已连接，但恢复 IPv4 失败', code='finalize_ipv4_failed')
    return _refresh_status_action(context, step)


def _wait(context: WorkflowContext, seconds: float) -> bool:
    deadline = min(time.monotonic() + seconds,
                   context.deadline if context.deadline is not None else float('inf'))
    while time.monotonic() < deadline:
        if context.cancelled():
            return False
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return not context.cancelled()


for _catalog_entry in WORKFLOW_CATALOG.values():
    _catalog_entry.setdefault('default_timeout', 15)
    _catalog_entry.setdefault('default_retries', 0)
    _catalog_entry.setdefault('default_retry_delay', 1.0)
for _default_workflow in (DEFAULT_AUTH_WORKFLOW, DEFAULT_PORTAL_LOGOUT_WORKFLOW,
                          DEFAULT_REAUTH_WORKFLOW, DEFAULT_RESTART_WARP_WORKFLOW,
                          DEFAULT_RESTORE_WORKFLOW):
    for _default_step in _default_workflow:
        _catalog_entry = WORKFLOW_CATALOG.get(_default_step['id'])
        if _catalog_entry:
            _catalog_entry['default_timeout'] = _default_step.get('timeout', 15)
            _catalog_entry['default_retries'] = _default_step.get('retries', 0)
            _catalog_entry['default_retry_delay'] = _default_step.get('retry_delay', 1.0)


ACTIONS = {
    'ensure_wifi': _ensure_wifi,
    'detect_warp_state': _detect_warp_state,
    'disconnect_warp': _disconnect_warp_action,
    'prepare_network': _prepare_network,
    'enable_ipv4': _enable_ipv4_action,
    'disable_ipv4': _disable_ipv4_action,
    'configure_ipv6_dns': _configure_ipv6_dns_action,
    'reset_ipv6_dns': _reset_ipv6_dns_action,
    'wait_public_ipv6': _wait_public_ipv6_action,
    'configure_ipv6': _configure_ipv6,
    'portal_login': _portal_login,
    'portal_logout': _portal_logout_action,
    'set_warp_endpoint_ipv6': _set_warp_endpoint_action,
    'reset_warp_endpoint_ipv6': _reset_warp_endpoint_action,
    'set_warp_masque': _set_warp_masque_action,
    'reset_warp_masque': _reset_warp_masque_action,
    'warp_underlay_unpin': _warp_underlay_unpin_action,
    'start_warp_service': _start_warp_service,
    'stop_warp_service': _stop_warp_service,
    'restart_warp_service': _restart_warp_service,
    'configure_warp': _configure_warp,
    'connect_warp': _connect_warp,
    'refresh_status': _refresh_status_action,
    'finalize': _finalize,
}
RUNNER = WorkflowRunner(ACTIONS, WORKFLOW_CATALOG)


def validate_auth_workflow(workflow):
    return RUNNER.validate(workflow)


def workflow_catalog():
    return [{'id': step_id, **metadata} for step_id, metadata in WORKFLOW_CATALOG.items()]


def resolve_workflow(config: dict, workflow_id: str | None = None) -> dict:
    workflows = config.get('workflows') or {}
    selected_id = workflow_id or config.get('active_workflow_id', 'default_auth')
    workflow = workflows.get(selected_id)
    if not workflow:
        if selected_id != 'default_auth':
            return resolve_workflow(config, 'default_auth')
        raise ValueError('默认工作流不存在')
    return workflow


def _publish(event):
    from core.auth import _push_auth_progress, push_runner_event
    status = event.get('status', 'running')
    operation_status = 'running' if status in ('running', 'retrying', 'success') else status
    app_state.update_operation(
        kind='auth', status=operation_status, step=event.get('step', 0),
        total=event.get('total', 0), step_id=event.get('step_id'),
        message=event.get('message', ''), details={
            'attempt': event.get('attempt', 1), 'code': event.get('code'),
        })
    # onAuthProgress 内部读取当前纪元并附带推送，前端按纪元过滤
    frontend_status = 'running' if status in ('running', 'retrying', 'success') else status
    _push_auth_progress(event.get('step', 0), event.get('total', 1),
                        event.get('message', ''), frontend_status, 'auth')
    # 「测试工作流」悬浮窗的详细事件流（窗口未打开时内部自行忽略）
    push_runner_event(event)


def _resolve_workflow_link(steps) -> tuple[str, bool]:
    """从 steps 提取「认证链路」，返回 (link_type, explicit)。

    以第一个 portal_login 节点的 params.link_mode 为准。注销节点不参与
    预扫描：注销一律按「当前实际链路」执行（见 _portal_logout_action），
    它的 link_mode 不再代表工作流的认证链路——否则「注销(auto) + 认证
    (无线)」的工作流会被预扫描成有线，既不做无线隔离，WiFi 连接也被跳过。
    - 显式 wired/wireless → (该值, True)；
    - auto / 无 params / 无 Portal 认证节点 → (detect_link_type(), False)。

    explicit=True 时调用方执行链路隔离（禁用另一类型网卡）；
    auto 仅用于 IPv4/IPv6 等节点选网卡，不动系统网卡状态（兼容旧工作流）。
    """
    for item in steps:
        if not isinstance(item, dict) or item.get('id') != 'portal_login':
            continue
        params = item.get('params')
        mode = str(params.get('link_mode') or '').strip().lower() \
            if isinstance(params, dict) else ''
        if mode in ('wired', 'wireless'):
            return mode, True
        break  # 以第一个 Portal 认证节点为准
    try:
        return detect_link_type(), False
    except Exception:
        logger.exception('detect_link_type failed, fallback to wireless')
        return 'wireless', False


def resolve_global_nodes(steps, node_globals):
    """把 node_mode='global' 的节点替换为其类型对应的全局配置。

    - node_globals: {step_id: {'config': {...}, 'source': {...}}}，见 core.config；
    - 全局配置覆盖 timeout/retries/retry_delay/continue_on_error/params
      （enabled 始终属于所在工作流，不参与全局共享）；
    - 某类型没有全局配置、或节点不是 global 模式时，原样返回该节点
      （回退到节点自身配置，保证旧配置完全兼容）。
    """
    import copy as _copy

    resolved = []
    for item in steps:
        step = dict(item) if isinstance(item, dict) else item
        if not isinstance(step, dict):
            resolved.append(step)
            continue
        mode = str(step.get('node_mode') or 'independent')
        entry = (node_globals or {}).get(str(step.get('id', '')))
        if mode == 'global' and isinstance(entry, dict) and isinstance(entry.get('config'), dict):
            merged = dict(step)
            for key in ('retries', 'timeout', 'retry_delay', 'continue_on_error', 'params'):
                if key in entry['config']:
                    merged[key] = _copy.deepcopy(entry['config'][key])
            merged['id'] = step.get('id')
            resolved.append(merged)
            continue
        resolved.append(step)
    return resolved


def _plan_link_isolation(workflow, link_type: str, link_explicit: bool,
                         current_link: str) -> str:
    """决定工作流开始时的链路隔离策略，返回 'now' | 'defer' | 'none'。

    - auto（非显式）：不动网卡（兼容旧行为）→ none；
    - 显式链路 + 纯注销工作流（无 portal_login）：隔离只会在注销前禁掉
      正在使用的网卡（有线在线 + 无线注销工作流 = 网线被禁，注销请求
      本身都发不出去），永不隔离 → none。注销节点自身按当前链路选变体；
    - 显式链路 + 注销 + 重新认证：指定链路与当前链路一致时照常立即隔离
      （now）；不一致时推迟到注销完成后应用（defer，由
      _portal_logout_action 末尾消费 pending_link_exclusive）——注销先
      走当前链路，认证部分再切到指定链路；
    - 显式链路 + 纯认证：立即隔离（now，与原行为一致）。
    """
    def _has(step_id: str) -> bool:
        return any(isinstance(item, dict) and item.get('id') == step_id
                   and item.get('enabled', True) for item in workflow)

    if not link_explicit:
        return 'none'
    has_logout, has_login = _has('portal_logout'), _has('portal_login')
    if has_logout and not has_login:
        return 'none'
    if has_logout:
        return 'defer' if current_link != link_type else 'now'
    return 'now'


def run_auth_workflow(config=None, workflow=None, workflow_id=None, strict=True):
    """执行认证工作流。

    strict（默认 True）：严格重走完整流程，不因"WARP 已连接"而跳过准备步骤。
    是否需要认证由调用方判断（手动点击、开机与 WiFi 事件守卫），
    一旦调用本函数就应把认证真正做完整，避免残留状态被反复沿用。
    """
    config = config or get_config()
    workflow_name = '自定义工作流'
    workflow_key = None  # 能确定工作流身份时才记录统计
    if workflow_id is not None or workflow is None:
        selected = workflow_id or config.get('active_workflow_id')
        definition = resolve_workflow(config, selected)
        workflow = definition.get('steps') or DEFAULT_AUTH_WORKFLOW
        workflow_name = definition.get('name', workflow_name)
        workflow_key = definition.get('id') or selected
    elif isinstance(workflow, dict):
        workflow_name = workflow.get('name', workflow_name)
        workflow = workflow.get('steps', DEFAULT_AUTH_WORKFLOW)
    # 全局节点解析：node_mode='global' 的节点使用该类型的全局配置
    workflow = resolve_global_nodes(workflow, config.get('node_globals'))
    logger.info('开始执行工作流「%s」', workflow_name)
    core.state._auth_cancelled.clear()
    app_state.update_operation(kind='auth', status='running', step=0,
                               total=sum(1 for item in workflow if item.get('enabled', True)),
                               message=f'准备执行：{workflow_name}')
    context = WorkflowContext(config=config,
                              cancelled=core.state._auth_cancelled.is_set,
                              publish=_publish,
                              overall_timeout=config.get('auth_total_timeout', 90))
    context.data['strict_full_run'] = bool(strict)
    # 预扫描链路类型：IPv4/IPv6 等节点据此选网卡；
    # 用户显式指定连接方式时先做链路隔离（只保留指定类型网卡工作），
    # 避免 WiFi+网线双开时流量走错网卡导致认证页打不开/填不进。
    # 例外见 _plan_link_isolation：注销按「当前链路」执行，因此纯注销
    # 不隔离；「注销+重认证」指定链路与当前链路不一致时，隔离推迟到
    # 注销完成后应用（否则会在注销前把正在使用的网卡禁掉）。
    link_type, link_explicit = _resolve_workflow_link(workflow)
    context.data['link_type'] = link_type
    current_link = link_type
    if link_explicit:
        try:
            current_link = detect_link_type()
        except Exception:
            logger.exception('detect_link_type failed, fallback to wireless')
            current_link = 'wireless'
    plan = _plan_link_isolation(workflow, link_type, link_explicit, current_link)
    if plan == 'now':
        link_ok, link_msg = apply_link_exclusive(link_type)
        if not link_ok:
            app_state.update_operation(kind='auth', status='error', message=link_msg)
            return False, link_msg
        logger.info('工作流链路=%s（用户指定），%s', link_type, link_msg)
    elif plan == 'defer':
        context.data['pending_link_exclusive'] = link_type
        logger.info('工作流链路=%s（用户指定），但当前实际链路=%s：'
                    '注销将按当前链路执行，网卡隔离推迟到注销完成后应用',
                    link_type, current_link)
    elif link_explicit:
        logger.info('工作流链路=%s（用户指定的纯注销流程）：不做网卡隔离，'
                    '注销按当前实际链路执行', link_type)
    else:
        logger.info('工作流链路=%s（自动检测，未做网卡隔离）', link_type)
    try:
        result = RUNNER.run(workflow, context, success_message=f'{workflow_name}完成')
    except ValueError as exc:
        app_state.update_operation(kind='auth', status='error', message=str(exc))
        return False, f'工作流配置无效：{exc}'
    if workflow_key:
        _record_step_stats(workflow_key, result, config)
    final_status = 'success' if result.success else ('cancelled' if result.code == 'cancelled' else 'error')
    # 保留最近一次运行结果（含每节点耗时/重试统计），供测试悬浮窗的
    # done 事件读取；仅供 UI 展示，不参与任何控制流判断
    core.state.last_workflow_result = result
    app_state.update_operation(kind='auth', status=final_status,
                               message=result.message,
                               details={'code': result.code, 'failed_step': result.failed_step,
                                        'elapsed': round(result.elapsed, 2),
                                        'workflow_name': workflow_name})
    try:
        from core.status import network_status
        network_status.request_refresh()
    except Exception:
        logger.exception('Could not refresh status after workflow')
    return result.success, result.message


def _record_step_stats(workflow_key: str, result, config: dict) -> None:
    """记录每个节点的耗时/重试样本；开启自动调优时把建议值写回工作流配置。"""
    # 用户取消的运行整体不作为样本：中断前后各节点的耗时/成败都失真，
    # 喂给调优算法会让超时越调越大、重试越调越多
    if result.code == 'cancelled':
        return
    try:
        from core.workflow_tuning import get_tuning_store
        store = get_tuning_store()
    except Exception:
        return
    for step_id, info in (result.step_stats or {}).items():
        if not info.get('executed', True):
            continue  # 因取消/总时限被跳过的节点没有真实执行数据
        try:
            store.record(workflow_key, step_id, info.get('elapsed', 0.0),
                         info.get('retries', 0), bool(info.get('success')),
                         timeout=info.get('timeout', 0.0))
        except Exception:
            logger.exception('[tuning] 记录节点统计失败: %s', step_id)
    if not config.get('auto_tune_workflow'):
        return
    try:
        apply_auto_tune(workflow_key)
    except Exception:
        logger.exception('[tuning] 应用自动调优失败')


def apply_auto_tune(workflow_id: str) -> list[dict]:
    """把调优建议写回该工作流的 steps；内置工作流需要标记 customized 才能持久化。

    全局节点（node_mode='global'）的建议写入该类型的全局配置 node_globals，
    而不是节点副本——否则运行时全局配置会覆盖调优结果，建议永远不生效。

    返回变更明细列表（每项含节点 id 与超时/重试的前后值），无建议时返回空列表。
    """
    import copy as _copy

    from core.config import get_config_store
    from core.workflow_tuning import get_tuning_store

    config_store = get_config_store()
    snapshot = config_store.snapshot()
    workflows = snapshot.get('workflows') or {}
    definition = workflows.get(workflow_id)
    if not definition:
        return []
    steps = _copy.deepcopy(definition.get('steps') or [])
    node_globals = _copy.deepcopy(snapshot.get('node_globals') or {})
    old_globals = _copy.deepcopy(node_globals)
    before = {str(step.get('id', '')): (float(step.get('timeout', 15) or 15),
                                        int(step.get('retries', 0) or 0))
              for step in steps}
    global_steps = [step for step in steps
                    if str(step.get('node_mode') or 'independent') == 'global']
    local_steps = [step for step in steps
                   if str(step.get('node_mode') or 'independent') != 'global']
    changed_local = get_tuning_store().apply_to_workflow(workflow_id, local_steps)

    # 全局节点：建议写入 node_globals[step_id].config（该类型共享一份）
    global_changes = []
    for step in global_steps:
        step_id = str(step.get('id', ''))
        store = get_tuning_store()
        current_timeout = float(step.get('timeout', 15) or 15)
        current_retries = int(step.get('retries', 0) or 0)
        new_timeout = store.suggest_timeout(workflow_id, step_id, current_timeout)
        new_retries = store.suggest_retries(workflow_id, step_id, current_retries)
        if new_timeout is None and new_retries is None:
            continue
        entry = node_globals.setdefault(step_id, {'config': {}, 'source': {}})
        cfg = entry.setdefault('config', {})
        prev_cfg = (old_globals.get(step_id) or {}).get('config') or {}
        old_timeout = float(prev_cfg.get('timeout', current_timeout))
        old_retries = int(prev_cfg.get('retries', current_retries))
        if new_timeout is not None:
            cfg['timeout'] = new_timeout
        if new_retries is not None:
            cfg['retries'] = new_retries
        global_changes.append({'id': step_id,
                               'timeout_from': old_timeout,
                               'timeout': float(cfg.get('timeout', old_timeout)),
                               'retries_from': old_retries,
                               'retries': int(cfg.get('retries', old_retries))})

    if not changed_local and not global_changes:
        return []

    updated = _copy.deepcopy(definition)
    updated['steps'] = steps
    if updated.get('built_in'):
        updated['customized'] = True
    new_workflows = _copy.deepcopy(workflows)
    new_workflows[workflow_id] = updated
    patch = {'workflows': new_workflows}
    if node_globals != (snapshot.get('node_globals') or {}):
        patch['node_globals'] = node_globals
    config_store.patch(patch)

    changes = []
    for step in local_steps:
        step_id = str(step.get('id', ''))
        old_timeout, old_retries = before.get(step_id, (step['timeout'], step['retries']))
        if step['timeout'] != old_timeout or step['retries'] != old_retries:
            changes.append({'id': step_id,
                            'timeout_from': old_timeout, 'timeout': step['timeout'],
                            'retries_from': old_retries, 'retries': step['retries']})
    changes.extend(global_changes)
    # 日志里用用户保存的工作流名，id 只作补充——否则配置里改过名的工作流
    # 在日志中显示的仍是旧 id，用户对不上号
    wf_name = str(definition.get('name') or workflow_id)
    wf_label = wf_name if wf_name == workflow_id else f'{wf_name}（{workflow_id}）'
    logger.info('[tuning] 已按运行数据自动调整工作流 %s：%s', wf_label,
                '; '.join(f"{c['id']}(timeout {c['timeout_from']}→{c['timeout']}, "
                           f"retries {c['retries_from']}→{c['retries']})" for c in changes))
    return changes


def run_workflow_by_id(workflow_id: str, config=None, strict=True):
    return run_auth_workflow(config=config, workflow_id=workflow_id, strict=strict)
