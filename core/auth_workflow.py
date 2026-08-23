"""Authentication actions plugged into :mod:`core.workflow`."""
from __future__ import annotations

import logging
import time

import core.state
from core.app_state import app_state
from core.command import run_command
from core.config import DEFAULT_AUTH_WORKFLOW, get_config
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
    },
    'prepare_network': {
        'name': '准备校园网认证环境',
        'description': '暂时断开 WARP，并确保校园网 IPv4 可用。',
    },
    'portal_login': {
        'name': '校园网 Portal 认证',
        'description': '向校园网认证服务器提交账号并解析结果。',
    },
    'configure_ipv6': {
        'name': '获取并验证公网 IPv6',
        'description': '设置 IPv6 DNS、禁用 IPv4，并等待公网 IPv6。',
    },
    'configure_warp': {
        'name': '准备 Cloudflare WARP',
        'description': '临时启用 IPv6 端点与 MASQUE，并启动服务。',
    },
    'connect_warp': {
        'name': '连接 Cloudflare WARP',
        'description': '在硬超时内连接，并按可恢复错误重试。',
    },
    'finalize': {
        'name': '完成与清理',
        'description': '恢复临时 WARP 设置，并按偏好恢复 IPv4。',
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
        _, output, _ = run_command(['netsh', 'wlan', 'show', 'interfaces'], shell=False, timeout=_command_timeout(context, 4))
        if wifi_name in output and ('已连接' in output or 'connected' in output.lower()):
            return StepResult.ok(f'已连接 {wifi_name}')
    return StepResult.fail(f'连接后仍未检测到 {wifi_name}', code='wifi_not_ready', retryable=True)


def _prepare_network(context: WorkflowContext, step: StepSpec) -> StepResult:
    disable_ipv4, enable_ipv4, _, _ = _auth_helpers()
    interface_name = get_wifi_interface_name()
    if not interface_name:
        return StepResult.fail('无法获取 WiFi 接口名称', code='interface_missing', retryable=True)
    context.data['interface_name'] = interface_name
    if is_warp_connected():
        context.data['already_connected'] = True
        return StepResult.ok('WARP 已连接，跳过重复认证')
    disconnect_warp(full=False, timeout=_command_timeout(context, 5))
    _, service_output, _ = run_command('sc query "CloudflareWARP"', timeout=_command_timeout(context, 5))
    context.data['warp_service_was_running'] = 'RUNNING' in service_output
    if not enable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
        return StepResult.fail('无法启用校园网 IPv4', code='enable_ipv4_failed')

    def restore_ipv4():
        enable_ipv4(interface_name)
        run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp', timeout=5)
    context.add_rollback('restore_ipv4', restore_ipv4)
    context.data['ipv4_rollback_registered'] = True
    return StepResult.ok('认证网络环境已准备')


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


def _configure_ipv6(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('沿用当前 WARP 网络')
    disable_ipv4, enable_ipv4, portal_login, portal_logout = _auth_helpers()
    interface_name = context.data.get('interface_name') or get_wifi_interface_name()
    if not interface_name:
        return StepResult.fail('无法获取 WiFi 接口名称', code='interface_missing')
    if context.current_attempt > 1:
        enable_ipv4(interface_name, timeout=_command_timeout(context, 6))
        portal_logout(context.config, timeout=min(4, context.remaining()))
        success, message = portal_login(context.config, timeout=min(7, context.remaining()))
        if not success:
            return StepResult.fail(f'刷新 IPv6 租约前重新认证失败：{message}',
                                   code='ipv6_reauth_failed', retryable=False)
    primary = f'netsh interface ipv6 set dnsservers "{interface_name}" static 2606:4700:4700::1111 primary'
    secondary = f'netsh interface ipv6 add dnsservers "{interface_name}" 2606:4700:4700::1001 index=2'
    code, _, error = run_command(primary, timeout=_command_timeout(context, 5))
    if code != 0:
        return StepResult.fail(f'IPv6 DNS 设置失败：{error.strip()[:120]}',
                               code='ipv6_dns_failed', retryable=True)
    run_command(secondary, timeout=_command_timeout(context, 5))
    if not context.data.get('ipv4_rollback_registered'):
        def restore_ipv4():
            enable_ipv4(interface_name)
            run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp', timeout=5)
        context.add_rollback('restore_ipv4', restore_ipv4)
        context.data['ipv4_rollback_registered'] = True
    if not disable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
        return StepResult.fail('禁用 IPv4 失败', code='disable_ipv4_failed')
    while context.remaining() > 0.2 and not context.cancelled():
        found, address = has_public_ipv6()
        if found:
            context.data['ipv6_address'] = address
            return StepResult.ok(f'公网 IPv6 已就绪：{address}')
        if not _wait(context, 1.2):
            break
    return StepResult.fail('在限定时间内未获取到公网 IPv6', code='ipv6_timeout', retryable=True)


def _configure_warp(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('沿用当前 WARP 配置')
    warp_cli = get_warp_cli()
    if not warp_cli:
        return StepResult.fail('未找到 Cloudflare WARP，请检查安装或 warp-cli 路径',
                               code='warp_cli_missing')
    context.data['warp_cli'] = warp_cli
    endpoint_changed = _set_warp_endpoint_ipv6(True)
    run_command('sc config "CloudflareWARP" start= auto', timeout=_command_timeout(context, 5))
    _, service_output, _ = run_command('sc query "CloudflareWARP"', timeout=_command_timeout(context, 5))
    if 'RUNNING' not in service_output:
        code, output, error = run_command('net start "CloudflareWARP"',
                                          timeout=_command_timeout(context, 8))
        if code != 0:
            if endpoint_changed:
                _set_warp_endpoint_ipv6(False)
            return StepResult.fail(f'Cloudflare WARP 服务启动失败：{(error or output).strip()[:140]}',
                                   code='warp_service_failed', retryable=True)
    _set_warp_masque_mode(warp_cli, True, timeout=_command_timeout(context, 5))
    context.data['warp_configured'] = True

    def restore_warp_settings():
        _set_warp_masque_mode(warp_cli, False)
        if endpoint_changed:
            _set_warp_endpoint_ipv6(False)
    context.add_rollback('restore_warp_settings', restore_warp_settings)
    return StepResult.ok('WARP 连接参数已准备')


def _connect_warp(context: WorkflowContext, step: StepSpec) -> StepResult:
    if _skip_if_ready(context):
        return StepResult.ok('WARP 已连接')
    result = connect_warp_result(timeout=min(step.timeout, context.remaining()), max_attempts=1)
    if result.success:
        return StepResult.ok(result.message, attempts=result.attempts, elapsed=result.elapsed)
    return StepResult.fail(result.message, code=result.code, retryable=result.retryable,
                           attempts=result.attempts, elapsed=result.elapsed,
                           status_output=result.status_output[:200])


def _finalize(context: WorkflowContext, step: StepSpec) -> StepResult:
    disable_ipv4, enable_ipv4, _, _ = _auth_helpers()
    if context.data.get('warp_configured'):
        warp_cli = context.data.get('warp_cli')
        _set_warp_masque_mode(warp_cli, False, timeout=_command_timeout(context, 4))
        _set_warp_endpoint_ipv6(False)
    interface_name = context.data.get('interface_name') or get_wifi_interface_name()
    if interface_name and context.config.get('auto_enable_ipv4', True):
        if not enable_ipv4(interface_name, timeout=_command_timeout(context, 8)):
            return StepResult.fail('WARP 已连接，但恢复 IPv4 失败', code='finalize_ipv4_failed')
    try:
        from core.status import network_status
        network_status.request_refresh()
    except Exception:
        logger.exception('Could not request final status refresh')
    return StepResult.ok('认证完成')


def _wait(context: WorkflowContext, seconds: float) -> bool:
    deadline = min(time.monotonic() + seconds,
                   context.deadline if context.deadline is not None else float('inf'))
    while time.monotonic() < deadline:
        if context.cancelled():
            return False
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return not context.cancelled()


ACTIONS = {
    'ensure_wifi': _ensure_wifi,
    'prepare_network': _prepare_network,
    'portal_login': _portal_login,
    'configure_ipv6': _configure_ipv6,
    'configure_warp': _configure_warp,
    'connect_warp': _connect_warp,
    'finalize': _finalize,
}
RUNNER = WorkflowRunner(ACTIONS, WORKFLOW_CATALOG)


def validate_auth_workflow(workflow):
    return RUNNER.validate(workflow)


def workflow_catalog():
    return [{'id': step_id, **metadata} for step_id, metadata in WORKFLOW_CATALOG.items()]


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
    frontend_status = 'running' if status in ('running', 'retrying', 'success') else status
    _push_auth_progress(event.get('step', 0), event.get('total', 1),
                        event.get('message', ''), frontend_status, 'auth')


def run_auth_workflow(config=None, workflow=None):
    config = config or get_config()
    workflow = workflow or config.get('auth_workflow') or DEFAULT_AUTH_WORKFLOW
    core.state._auth_cancelled.clear()
    app_state.update_operation(kind='auth', status='running', step=0,
                               total=sum(1 for item in workflow if item.get('enabled', True)),
                               message='准备认证')
    context = WorkflowContext(config=config,
                              cancelled=core.state._auth_cancelled.is_set,
                              publish=_publish,
                              overall_timeout=config.get('auth_total_timeout', 90))
    try:
        result = RUNNER.run(workflow, context)
    except ValueError as exc:
        app_state.update_operation(kind='auth', status='error', message=str(exc))
        return False, f'工作流配置无效：{exc}'
    final_status = 'success' if result.success else ('cancelled' if result.code == 'cancelled' else 'error')
    app_state.update_operation(kind='auth', status=final_status,
                               message=result.message,
                               details={'code': result.code, 'failed_step': result.failed_step,
                                        'elapsed': round(result.elapsed, 2)})
    try:
        from core.status import network_status
        network_status.request_refresh()
    except Exception:
        logger.exception('Could not refresh status after workflow')
    return result.success, result.message
