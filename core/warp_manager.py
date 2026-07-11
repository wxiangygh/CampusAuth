"""WARP 连接管理模块。

包含 WARP 客户端的连接、断开、MASQUE 模式配置、IPv6 端点设置等功能。
"""
import json
import logging
import os
import time

import core.state
from core.command import run_command, run_elevated_powershell

logger = logging.getLogger('wifi_tray')


def get_warp_cli():
    """查找 warp-cli 可执行文件路径。

    优先使用 CONFIG['warp_cli_path'] 自定义路径，其次查找 PATH，
    最后尝试默认安装路径。返回路径字符串或 None。
    """
    # CONFIG 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import CONFIG
    custom = CONFIG.get('warp_cli_path', '').strip()
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
    code, _, _ = run_command('warp-cli --version')
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


def disconnect_warp(full=True):
    """断开 WARP 连接。

    Args:
        full: True 时同时停止并禁用 WARP 服务自启；False 仅断开连接。

    Returns:
        bool: 操作是否成功完成（未被取消）。
    """
    # _check_cancel / _interruptible_sleep 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import _check_cancel, _interruptible_sleep
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command([warp_cli, 'status'], shell=False)
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            logger.info("Disconnecting WARP...")
            run_command([warp_cli, 'disconnect'], shell=False)
            if not _interruptible_sleep(1): return False
    if _check_cancel(): return False
    if full:
        logger.info("Stopping WARP service...")
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' in svc_output:
            run_command('net stop "CloudflareWARP"')
        if _check_cancel(): return False
        logger.info("Disabling WARP service auto-start...")
        run_command('sc config "CloudflareWARP" start= disabled')
        if warp_cli:
            run_command([warp_cli, 'set-mode', 'warp+doh'], shell=False)
            run_command([warp_cli, 'disable-wifi'], shell=False)
            run_command([warp_cli, 'disable-ethernet'], shell=False)
        logger.info("Waiting for WARP interface to disappear...")
        for i in range(10):
            if _check_cancel(): return False
            code, output, _ = run_command('netsh interface ipv4 show interfaces')
            if 'CloudflareWARP' not in output:
                logger.info(f"WARP interface disappeared ({i+1} checks)")
                return True
            if not _interruptible_sleep(1): return False
        logger.info("WARP interface still exists, trying to disable...")
        run_command('netsh interface ipv4 set interface "CloudflareWARP" disabled')
        run_command('netsh interface set interface "CloudflareWARP" disable')
        _interruptible_sleep(2)
    return True


def _set_warp_masque_mode(warp_cli, enable):
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
            run_command([warp_cli, 'tunnel', 'protocol', 'set', 'MASQUE'], shell=False)
            run_command([warp_cli, 'tunnel', 'masque-options', 'set', 'h3-with-h2-fallback'], shell=False)
            logger.info("MASQUE h3-with-h2-fallback mode set (QUIC/UDP:443 with TCP/443 fallback)")
        else:
            logger.info("Resetting WARP tunnel protocol to default...")
            run_command([warp_cli, 'tunnel', 'protocol', 'reset'], shell=False)
            run_command([warp_cli, 'tunnel', 'masque-options', 'reset'], shell=False)
            logger.info("WARP tunnel protocol reset to default")
        return True
    except Exception as e:
        logger.error(f"Failed to set MASQUE mode: {e}")
        return False


def _set_warp_endpoint_ipv6(enable):
    """修改 WARP conf.json 端点配置以支持 IPv6。

    启用时清空 IPv4 端点并备份原始配置；禁用时恢复备份或重建默认 IPv4 端点。
    备份存储在 core.state._conf_json_backup。

    Args:
        enable: True 清空 IPv4 端点；False 恢复/重建 IPv4 端点

    Returns:
        bool: 操作是否成功。
    """
    conf_path = os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'),
                              'Cloudflare', 'conf.json')
    try:
        if not os.path.exists(conf_path):
            logger.warning(f"WARP conf.json not found at {conf_path}")
            return False
        with open(conf_path, 'r', encoding='utf-8') as f:
            conf = json.load(f)
        if enable:
            core.state._conf_json_backup = json.dumps(conf)
            if 'endpoints' in conf:
                for ep in conf['endpoints']:
                    ep['v4'] = ''
                logger.info(f"Cleared IPv4 endpoints in conf.json ({len(conf['endpoints'])} endpoints)")
            else:
                logger.warning("No endpoints found in conf.json")
        else:
            if core.state._conf_json_backup:
                conf = json.loads(core.state._conf_json_backup)
                core.state._conf_json_backup = None
                logger.info("Restored original conf.json endpoints")
            else:
                if 'endpoints' in conf:
                    for ep in conf['endpoints']:
                        if not ep.get('v4'):
                            ep['v4'] = '162.159.198.2:443'
                    logger.info("Reconstructed IPv4 endpoints in conf.json")
        with open(conf_path, 'w', encoding='utf-8') as f:
            json.dump(conf, f)
        return True
    except Exception as e:
        logger.error(f"Failed to update WARP conf.json: {e}")
        return False


def connect_warp(force_restart=False):
    """连接 WARP。

    Args:
        force_restart: True 时强制重启 WARP 服务以应用配置变更。

    Returns:
        bool: 是否最终连接成功。
    """
    logger.info("Connecting to WARP...")
    warp_cli = get_warp_cli()
    if not warp_cli:
        logger.error("warp-cli not found")
        return False
    run_command('sc config "CloudflareWARP" start= auto')
    if force_restart:
        logger.info("Force restarting WARP service to apply config changes...")
        run_command('net stop "CloudflareWARP"')
        time.sleep(2)
        run_command('net start "CloudflareWARP"')
        time.sleep(3)
    return _connect_warp_inner(warp_cli)


def _connect_warp_inner(warp_cli):
    """WARP 连接内部实现，含最多 2 次重试。

    Args:
        warp_cli: warp-cli 可执行文件路径

    Returns:
        bool: 是否连接成功。
    """
    # _check_cancel / _interruptible_sleep 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import _check_cancel, _interruptible_sleep
    for attempt in range(2):
        if _check_cancel(): return False
        code, svc_output, _ = run_command('sc query "CloudflareWARP"')
        if 'RUNNING' not in svc_output:
            logger.info(f"Starting WARP service (attempt {attempt+1})...")
            run_command('net start "CloudflareWARP"')
            if not _interruptible_sleep(5): return False
            code, svc_output, _ = run_command('sc query "CloudflareWARP"')
            if 'RUNNING' not in svc_output:
                logger.warning(f"WARP service failed to start, retrying...")
                if not _interruptible_sleep(3): return False
                continue
        code, output, _ = run_command([warp_cli, 'status'], shell=False)
        logger.debug(f"connect_warp: initial status: {output.strip()[:150]}")
        if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
            logger.info("WARP already connected")
            return True
        if 'Manual Disconnection' in output or 'Account is disconnected' in output:
            logger.info("WARP in disconnected state, re-enabling networks...")
            run_command([warp_cli, 'enable-wifi'], shell=False)
            run_command([warp_cli, 'enable-ethernet'], shell=False)
            if not _interruptible_sleep(1): return False
            if attempt > 0:
                logger.info("Still stuck in Manual Disconnection, restarting WARP service...")
                run_command('net stop "CloudflareWARP"')
                if not _interruptible_sleep(3): return False
                run_command('net start "CloudflareWARP"')
                if not _interruptible_sleep(5): return False
                code, output, _ = run_command([warp_cli, 'status'], shell=False)
                logger.debug(f"connect_warp: status after restart: {output.strip()[:150]}")
                if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                    logger.info("WARP connected after service restart")
                    return True
        if _check_cancel(): return False
        logger.info("WARP not connected, issuing connect command...")
        run_command([warp_cli, 'connect'], shell=False)
        logger.info("Waiting for WARP connection...")
        for i in range(10):
            if not _interruptible_sleep(3): return False
            code, output, _ = run_command([warp_cli, 'status'], shell=False)
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                logger.info(f"WARP connected ({i+1} checks)")
                return True
            if 'Manual Disconnection' in output or 'Account is disconnected' in output:
                logger.info(f"WARP fell back to disconnected state ({i+1}/10), breaking inner loop")
                break
            if 'Unable' in output:
                if 'No Network' in output:
                    logger.info(f"WARP reports No Network ({i+1}/10), waiting for IPv6...")
                    continue
                logger.info(f"WARP unable to connect ({i+1}/10), breaking inner loop")
                break
            if i % 3 == 2:
                logger.info(f"WARP still connecting... ({i+1}/10 checks, status: {output.strip()[:100]})")
        logger.warning(f"WARP connection timeout (attempt {attempt+1})")
    logger.warning("WARP connection failed after 2 attempts")
    return False


def update_tray_icon(success, message=''):
    """更新托盘图标状态（连接结果）。

    Args:
        success: True 显示橙色已连接图标；False 显示红色失败图标
        message: 图标 tooltip 文本，为空时使用默认文案
    """
    # create_icon 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import create_icon
    try:
        if core.state._tray_app_instance and core.state._tray_app_instance.icon:
            if success:
                core.state._tray_app_instance.icon.icon = create_icon('orange')
                core.state._tray_app_instance.icon.title = message or 'WARP已连接'
            else:
                core.state._tray_app_instance.icon.icon = create_icon('red')
                core.state._tray_app_instance.icon.title = message or '认证失败'
    except Exception as e:
        logger.error(f"update_tray_icon failed: {e}")


def update_tray_icon_restore(success, message=''):
    """更新托盘图标状态（恢复结果）。

    Args:
        success: True 显示绿色已恢复图标；False 显示红色失败图标
        message: 图标 tooltip 文本，为空时使用默认文案
    """
    # create_icon 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import create_icon
    try:
        if core.state._tray_app_instance and core.state._tray_app_instance.icon:
            if success:
                core.state._tray_app_instance.icon.icon = create_icon('green')
                core.state._tray_app_instance.icon.title = message or '已恢复正常'
            else:
                core.state._tray_app_instance.icon.icon = create_icon('red')
                core.state._tray_app_instance.icon.title = message or '恢复失败'
    except Exception as e:
        logger.error(f"update_tray_icon_restore failed: {e}")
