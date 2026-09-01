"""Authentication actions plugged into :mod:`core.workflow`."""
from __future__ import annotations

import logging
import time

import core.state
from core.app_state import app_state
from core.command import run_command
from core.config import (
    DEFAULT_AUTH_WORKFLOW, DEFAULT_PORTAL_LOGOUT_WORKFLOW,
    DEFAULT_REAUTH_WORKFLOW, DEFAULT_RESTART_WARP_WORKFLOW, get_config,
)
from core.network import get_wifi_interface_name, has_public_ipv6, is_warp_connected
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
        'description': '将 WiFi 接口 DNS 设置为 Cloudflare IPv6 地址。',
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
    },
    'portal_logout': {
        'name': '校园网 Portal 注销',
        'description': '注销当前 Portal 会话，可放在重认证或独立工作流开头。',
        'group': 'Portal 认证',
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
        'description': '在硬超时内连接，并按可恢复错误重试。',
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


def _interface(context: WorkflowContext):
    interface_name = context.data.get('interface_name') or get_wifi_interface_name()
    if not interface_name:
        return None, StepResult.fail('无法获取 WiFi 接口名称', code='interface_missing',
                                     retryable=True)
    context.data['interface_name'] = interface_name
    return interface_name, None


def _reset_ipv6_dns_command(interface_name: str, timeout: float = 5):
    return run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp',
                       timeout=timeout)


def _ensure_wifi(context: WorkflowContext, step: StepSpec) -> StepResult:
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
    interface_name, error = _interface(context)
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
    interface_name, error = _interface(context)
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
    interface_name, error = _interface(context)
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
    interface_name, error = _interface(context)
    if error:
        return error
    primary = f'netsh interface ipv6 set dnsservers "{interface_name}" static 2606:4700:4700::1111 primary'
    secondary = f'netsh interface ipv6 add dnsservers "{interface_name}" 2606:4700:4700::1001 index=2'
    code, _, error_text = run_command(primary, timeout=_command_timeout(context, 5))
    if code != 0:
        return StepResult.fail(f'IPv6 DNS 设置失败：{error_text.strip()[:120]}',
                               code='ipv6_dns_failed', retryable=True)
    run_command(secondary, timeout=_command_timeout(context, 5))

    def restore_dns():
        _reset_ipv6_dns_command(interface_name)
    context.add_rollback('restore_ipv6_dns', restore_dns)
    return StepResult.ok('IPv6 DNS 已设置')


def _reset_ipv6_dns_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    interface_name, error = _interface(context)
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
    return StepResult.fail('在限定时间内未获取到公网 IPv6', code='ipv6_timeout', retryable=True)


def _portal_login(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接，无需重复 Portal 认证')
    _, _, portal_login, portal_logout = _auth_helpers()
    if context.current_attempt > 1:
        portal_logout(context.config, timeout=min(4, context.remaining()))
    success, message = portal_login(context.config, timeout=min(8, context.remaining()))
    if success:
        return StepResult.ok(message)
    retryable = any(token in message for token in
                    ('暂时不可用', '连接失败', 'AC认证失败', 'HTTP 5', '请求异常'))
    return StepResult.fail(message, code='portal_failed', retryable=retryable)


def _portal_logout_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    _, _, _, portal_logout = _auth_helpers()
    success = portal_logout(context.config, timeout=min(6, context.remaining()))
    if context.cancelled():
        return StepResult.fail('已取消', code='cancelled')
    if success:
        return StepResult.ok('Portal 已注销')
    # Logout is best-effort in a re-auth workflow; login can still replace the session.
    return StepResult.ok('Portal 注销未确认，继续执行后续步骤', code='logout_unconfirmed')


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


def _connect_warp(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        context.data['warp_connected'] = True
        return StepResult.ok('WARP 已连接')
    result = connect_warp_result(timeout=min(step.timeout, context.remaining()), max_attempts=1)
    if result.success:
        context.data['warp_connected'] = True
        return StepResult.ok(result.message, attempts=result.attempts, elapsed=result.elapsed)
    # 连接失败：中止可能仍在后台建立的连接。否则随后的回滚会恢复 IPv4
    # 端点并重新启用 IPv4，WARP 会在几秒后经 IPv4 连上，留下
    # "IPv4 开启 + WARP 走 IPv4"的脏状态（2026-09-01 日志即此情形）
    warp_cli = context.data.get('warp_cli') or get_warp_cli()
    if warp_cli:
        run_command([warp_cli, 'disconnect'], shell=False,
                    timeout=_command_timeout(context, 5))
    return StepResult.fail(result.message, code=result.code, retryable=result.retryable,
                           attempts=result.attempts, elapsed=result.elapsed,
                           status_output=result.status_output[:200])


def _refresh_status_action(context: WorkflowContext, step: StepSpec) -> StepResult:
    try:
        from core.status import network_status
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
    interface_name = context.data.get('interface_name') or get_wifi_interface_name()
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
                          DEFAULT_REAUTH_WORKFLOW, DEFAULT_RESTART_WARP_WORKFLOW):
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
    from core.auth import _push_auth_progress
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
    core.state._auth_cancelled.clear()
    app_state.update_operation(kind='auth', status='running', step=0,
                               total=sum(1 for item in workflow if item.get('enabled', True)),
                               message=f'准备执行：{workflow_name}')
    context = WorkflowContext(config=config,
                              cancelled=core.state._auth_cancelled.is_set,
                              publish=_publish,
                              overall_timeout=config.get('auth_total_timeout', 90))
    context.data['strict_full_run'] = bool(strict)
    try:
        result = RUNNER.run(workflow, context, success_message=f'{workflow_name}完成')
    except ValueError as exc:
        app_state.update_operation(kind='auth', status='error', message=str(exc))
        return False, f'工作流配置无效：{exc}'
    if workflow_key:
        _record_step_stats(workflow_key, result, config)
    final_status = 'success' if result.success else ('cancelled' if result.code == 'cancelled' else 'error')
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
    before = {str(step.get('id', '')): (float(step.get('timeout', 15) or 15),
                                        int(step.get('retries', 0) or 0))
              for step in steps}
    if not get_tuning_store().apply_to_workflow(workflow_id, steps):
        return []
    updated = _copy.deepcopy(definition)
    updated['steps'] = steps
    if updated.get('built_in'):
        updated['customized'] = True
    new_workflows = _copy.deepcopy(workflows)
    new_workflows[workflow_id] = updated
    config_store.patch({'workflows': new_workflows})
    changes = []
    for step in steps:
        step_id = str(step.get('id', ''))
        old_timeout, old_retries = before.get(step_id, (step['timeout'], step['retries']))
        if step['timeout'] != old_timeout or step['retries'] != old_retries:
            changes.append({'id': step_id,
                            'timeout_from': old_timeout, 'timeout': step['timeout'],
                            'retries_from': old_retries, 'retries': step['retries']})
    logger.info('[tuning] 已按运行数据自动调整工作流 %s：%s', workflow_id,
                '; '.join(f"{c['id']}(timeout {c['timeout_from']}→{c['timeout']}, "
                           f"retries {c['retries_from']}→{c['retries']})" for c in changes))
    return changes


def run_workflow_by_id(workflow_id: str, config=None, strict=True):
    return run_auth_workflow(config=config, workflow_id=workflow_id, strict=strict)
