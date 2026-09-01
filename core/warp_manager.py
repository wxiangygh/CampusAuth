"""WARP 连接管理模块。

包含 WARP 客户端的连接、断开、MASQUE 模式配置、IPv6 端点设置等功能。
"""
from dataclasses import dataclass, field
import json
import logging
import os
import time
from pathlib import Path

import core.state
from core.command import run_command, run_elevated_powershell
from core.config import get_config
from core.app_state import app_state

logger = logging.getLogger('wifi_tray')

@dataclass
class WarpConnectResult:
    success: bool
    code: str
    message: str
    retryable: bool = False
    attempts: int = 1
    elapsed: float = 0.0
    status_output: str = ''
    details: dict = field(default_factory=dict)


def _cancelled():
    return core.state._auth_cancelled.is_set()


def _sleep(seconds, interval=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _cancelled():
            return False
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return not _cancelled()


def get_warp_cli():
    """查找 warp-cli 可执行文件路径。

    优先使用 CONFIG['warp_cli_path'] 自定义路径，其次查找 PATH，
    最后尝试默认安装路径。返回路径字符串或 None。
    """
    custom = get_config().get('warp_cli_path', '').strip()
    if custom:
        if os.path.isfile(custom):
            logger.debug(f"get_warp_cli: using custom path: {custom}")
            return custom
        if os.path.isdir(custom):
            candidate = os.path.join(custom, 'warp-cli.exe')
            if os.path.isfile(candidate):
                logger.debug(f"get_warp_cli: using custom dir: {custom}")
                return candidate
        logger.warning(f"get_warp_cli: custom path not found: {custom}")
    code, _, _ = run_command('warp-cli --version', timeout=3)
    if code == 0:
        logger.debug("get_warp_cli: found in PATH")
        return 'warp-cli'
    default_paths = [
        r'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe',
        r'C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe',
    ]
    for p in default_paths:
        if os.path.isfile(p):
            logger.debug(f"get_warp_cli: found at {p}")
            return p
    logger.warning("get_warp_cli: warp-cli not found")
    return None


def disconnect_warp(full=True, timeout=12):
    """断开 WARP 连接。

    Args:
        full: True 时同时停止并禁用 WARP 服务自启；False 仅断开连接。

    Returns:
        bool: 操作是否成功完成（未被取消）。
    """
    # 记录主动断开：自动重连看门狗据此跳过"用户主动断开"场景，
    # 避免刚点完"恢复网络"又被看门狗立刻拉回 WARP。
    core.state.mark_warp_manual_disconnect()
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command([warp_cli, 'status'], shell=False, timeout=min(timeout, 4))
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            logger.info("Disconnecting WARP...")
            run_command([warp_cli, 'disconnect'], shell=False, timeout=min(timeout, 5))
            if not _sleep(1): return False
    if _cancelled(): return False
    if full:
        logger.info("Stopping WARP service...")
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' in svc_output:
            run_command('net stop "CloudflareWARP"')
        if _cancelled(): return False
        logger.info("Disabling WARP service auto-start...")
        run_command('sc config "CloudflareWARP" start= disabled')
        if warp_cli:
            run_command([warp_cli, 'set-mode', 'warp+doh'], shell=False)
            run_command([warp_cli, 'disable-wifi'], shell=False)
            run_command([warp_cli, 'disable-ethernet'], shell=False)
        logger.info("Waiting for WARP interface to disappear...")
        for i in range(10):
            if _cancelled(): return False
            code, output, _ = run_command('netsh interface ipv4 show interfaces')
            if 'CloudflareWARP' not in output:
                logger.info(f"WARP interface disappeared ({i+1} checks)")
                return True
            if not _sleep(1): return False
        logger.info("WARP interface still exists, trying to disable...")
        run_command('netsh interface ipv4 set interface "CloudflareWARP" disabled')
        run_command('netsh interface set interface "CloudflareWARP" disable')
        _sleep(2)
    return True


def _set_warp_masque_mode(warp_cli, enable, timeout=6):
    """设置 WARP MASQUE 隧道协议模式。

    Args:
        warp_cli: warp-cli 可执行文件路径
        enable: True 启用 MASQUE + h3-with-h2-fallback；False 重置为默认

    Returns:
        bool: 操作是否成功。
    """
    if not warp_cli:
        logger.warning("warp-cli not found, cannot set MASQUE mode")
        return False
    try:
        if enable:
            logger.info("Setting WARP tunnel protocol to MASQUE with h3-with-h2-fallback...")
            run_command([warp_cli, 'tunnel', 'protocol', 'set', 'MASQUE'], shell=False, timeout=timeout)
            run_command([warp_cli, 'tunnel', 'masque-options', 'set', 'h3-with-h2-fallback'], shell=False, timeout=timeout)
            logger.info("MASQUE h3-with-h2-fallback mode set (QUIC/UDP:443 with TCP/443 fallback)")
        else:
            logger.info("Resetting WARP tunnel protocol to default...")
            run_command([warp_cli, 'tunnel', 'protocol', 'reset'], shell=False, timeout=timeout)
            run_command([warp_cli, 'tunnel', 'masque-options', 'reset'], shell=False, timeout=timeout)
            logger.info("WARP tunnel protocol reset to default")
        return True
    except Exception as e:
        logger.error(f"Failed to set MASQUE mode: {e}")
        return False


def _write_json_atomic(path, value):
    path = Path(path)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            logger.warning('Could not remove temporary WARP config: %s', temp)


def _set_warp_endpoint_ipv6(enable):
    """Temporarily prefer IPv6 endpoints with a crash-recoverable backup."""
    conf_path = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'Cloudflare' / 'conf.json'
    backup_path = conf_path.with_suffix(conf_path.suffix + '.campusauth.bak')
    try:
        if not conf_path.exists():
            logger.warning('WARP conf.json not found at %s', conf_path)
            return False
        current = json.loads(conf_path.read_text(encoding='utf-8'))
        if enable:
            if backup_path.exists():
                original = json.loads(backup_path.read_text(encoding='utf-8'))
                logger.warning('Found a previous CampusAuth WARP backup; preserving it for recovery')
            else:
                original = current
                _write_json_atomic(backup_path, original)
            core.state._conf_json_backup = json.dumps(original)
            endpoints = current.get('endpoints') or []
            if not endpoints:
                logger.warning('No endpoints found in WARP conf.json')
                return False
            for endpoint in endpoints:
                endpoint['v4'] = ''
            _write_json_atomic(conf_path, current)
            logger.info('Temporarily cleared %d WARP IPv4 endpoints', len(endpoints))
            return True

        original = None
        if core.state._conf_json_backup:
            original = json.loads(core.state._conf_json_backup)
        elif backup_path.exists():
            original = json.loads(backup_path.read_text(encoding='utf-8'))
        if original is None:
            logger.info('No CampusAuth WARP endpoint backup to restore')
            return True
        _write_json_atomic(conf_path, original)
        core.state._conf_json_backup = None
        backup_path.unlink(missing_ok=True)
        logger.info('Restored original WARP endpoints')
        return True
    except Exception as exc:
        logger.error('Failed to update WARP conf.json: %s', exc)
        return False

def probe_warp_status(warp_cli=None, timeout=4):
    """Return a friendly, structured interpretation of ``warp-cli status``."""
    warp_cli = warp_cli or get_warp_cli()
    if not warp_cli:
        return WarpConnectResult(False, 'cli_missing',
                                 '未找到 Cloudflare WARP，请检查安装或配置 warp-cli 路径')
    code, output, error = run_command([warp_cli, 'status'], shell=False, timeout=timeout)
    text = (output or error or '').strip()
    lower = text.lower()
    if code == 0 and ('network: healthy' in lower or 'status update: connected' in lower):
        return WarpConnectResult(True, 'connected', 'WARP 已连接', status_output=text)
    if any(token in lower for token in ('registration missing', 'not registered',
                                         'account is not registered', 'no account')):
        return WarpConnectResult(False, 'registration_required',
                                 'WARP 尚未完成设备注册，请先打开 Cloudflare WARP 客户端完成注册',
                                 status_output=text)
    if 'manual disconnection' in lower or 'account is disconnected' in lower:
        return WarpConnectResult(False, 'manual_disconnection',
                                 'WARP 处于手动断开状态，正在重新启用网络', True,
                                 status_output=text)
    if 'no network' in lower:
        return WarpConnectResult(False, 'no_network',
                                 'WARP 暂未检测到可用网络', True, status_output=text)
    if 'dns lookup failed' in lower:
        # CF_DNS_LOOKUP_FAILURE：WARP 通过其内置 DNS 代理（DoH，端点
        # cloudflare-dns.com @ 162.159.36.1/46.1 等）解析域名失败。2026-09-01
        # 实测校园网（CMCC）会封锁对知名公共 DoH 服务器 IP 的 TCP 443，
        # 导致 WARP 连通性检查全部失败。此为网络环境限制，重试无益。
        return WarpConnectResult(False, 'dns_lookup_failed',
                                 'WARP DNS 解析失败（CF_DNS_LOOKUP_FAILURE）：当前网络可能封锁了 '
                                 'Cloudflare DoH 服务器（1.1.1.1 / 162.159.36.x）的 443 端口，'
                                 'WARP 无法通过其内置 DNS 代理解析域名。本机配置无异常，'
                                 '请更换网络环境或稍后重试',
                                 False, status_output=text)
    if 'unable' in lower or code != 0:
        detail = text[:180] if text else f'warp-cli 返回码 {code}'
        return WarpConnectResult(False, 'cli_error', f'WARP 状态检查失败：{detail}', True,
                                 status_output=text)
    return WarpConnectResult(False, 'connecting', 'WARP 正在连接', True, status_output=text)


def connect_warp_result(force_restart=False, timeout=25, max_attempts=1):
    """Connect WARP within a hard deadline and return actionable diagnostics.

    A single attempt is intentionally short. Workflow-level retry performs the
    exponential backoff, keeping the total connection time predictable.
    """
    started = time.monotonic()
    deadline = started + max(1.0, float(timeout))

    def command_timeout(maximum):
        return max(0.5, min(float(maximum), deadline - time.monotonic()))
    warp_cli = get_warp_cli()
    if not warp_cli:
        return probe_warp_status(None)
    run_command('sc config "CloudflareWARP" start= auto', timeout=command_timeout(5))
    last = WarpConnectResult(False, 'unknown', 'WARP 连接失败', True)
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        if _cancelled():
            return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt,
                                     elapsed=time.monotonic() - started)
        _, service_output, _ = run_command('sc query "CloudflareWARP"', timeout=command_timeout(5))
        if 'RUNNING' not in service_output:
            code, output, error = run_command('net start "CloudflareWARP"', timeout=command_timeout(8))
            if code != 0:
                last = WarpConnectResult(False, 'service_start_failed',
                                         f'Cloudflare WARP 服务启动失败：{(error or output).strip()[:160]}',
                                         True, attempt)
                continue
            if not _sleep(1.0):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)
        elif force_restart and attempt == 1:
            run_command('net stop "CloudflareWARP"', timeout=command_timeout(8))
            if not _sleep(0.5):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)
            run_command('net start "CloudflareWARP"', timeout=command_timeout(8))
            if not _sleep(1.0):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt)

        status = probe_warp_status(warp_cli, timeout=command_timeout(4))
        status.attempts = attempt
        if status.success:
            status.elapsed = time.monotonic() - started
            core.state.clear_warp_manual_disconnect()
            return status
        if status.code == 'registration_required':
            status.elapsed = time.monotonic() - started
            return status
        if status.code == 'manual_disconnection':
            run_command([warp_cli, 'enable-wifi'], shell=False, timeout=command_timeout(5))
            run_command([warp_cli, 'enable-ethernet'], shell=False, timeout=command_timeout(5))
        run_command([warp_cli, 'connect'], shell=False, timeout=command_timeout(5))

        poll_delay = 0.5
        connect_reissued = False
        while time.monotonic() < deadline:
            if not _sleep(min(poll_delay, max(0, deadline - time.monotonic()))):
                return WarpConnectResult(False, 'cancelled', '已取消', attempts=attempt,
                                         elapsed=time.monotonic() - started)
            status = probe_warp_status(warp_cli, timeout=command_timeout(3))
            status.attempts = attempt
            last = status
            if status.success:
                status.elapsed = time.monotonic() - started
                core.state.clear_warp_manual_disconnect()
                return status
            if status.code == 'registration_required':
                status.elapsed = time.monotonic() - started
                return status
            if status.code == 'dns_lookup_failed':
                status.elapsed = time.monotonic() - started
                return status
            if status.code == 'manual_disconnection' and not connect_reissued:
                # 服务刚启动时，connect 已发出但状态仍会短暂停留在"手动断开"。
                # 补发一次连接命令后继续等待，而不是立即放弃本轮（2026-09-01
                # 日志中 WARP 正是在放弃后几秒才真正连上）。
                connect_reissued = True
                run_command([warp_cli, 'enable-wifi'], shell=False, timeout=command_timeout(5))
                run_command([warp_cli, 'enable-ethernet'], shell=False, timeout=command_timeout(5))
                run_command([warp_cli, 'connect'], shell=False, timeout=command_timeout(5))
            poll_delay = min(poll_delay * 1.5, 2.0)
        if attempt < max_attempts and time.monotonic() < deadline:
            run_command('net stop "CloudflareWARP"', timeout=command_timeout(8))
            _sleep(0.5)
            run_command('net start "CloudflareWARP"', timeout=command_timeout(8))
    last.success = False
    last.code = 'timeout' if time.monotonic() >= deadline else last.code
    if last.code == 'timeout':
        last.message = f'WARP 在 {int(timeout)} 秒内未连接；请检查 IPv6、WARP 注册状态或防火墙'
    last.attempts = max(1, int(max_attempts))
    last.elapsed = time.monotonic() - started
    return last


def connect_warp(force_restart=False, timeout=25, max_attempts=1):
    """Compatibility wrapper returning only whether WARP connected."""
    return connect_warp_result(force_restart, timeout, max_attempts).success


def _connect_warp_inner(warp_cli):
    """Compatibility wrapper for older callers."""
    return connect_warp_result(timeout=25, max_attempts=1).success

def update_tray_icon(success, message=''):
    """Publish an authentication result; tray and UI subscribe to app_state."""
    if success:
        app_state.update_network('connected', message or 'WARP已连接', warp_connected=True)
    else:
        app_state.update_operation(kind='auth', status='error', message=message or '认证失败')


def update_tray_icon_restore(success, message=''):
    """Publish a restore result without reaching back into the tray module."""
    if success:
        app_state.update_network('normal', message or '已恢复正常', warp_connected=False,
                                 ipv4_disabled=False)
    else:
        app_state.update_operation(kind='restore', status='error', message=message or '恢复失败')