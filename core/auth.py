"""认证流程模块。

包含校园网门户认证、IPv4 禁用/启用、认证任务编排等功能。
"""
import json
import time
import threading
import logging

import core.state
from core.state import _auth_cancelled, _auth_lock
from core.command import run_command, run_elevated_powershell
from core.network import (
    get_wifi_interface_name, get_local_ip, get_mac_address,
    wait_for_network_ready, _wait_for_ipv6_ready, is_warp_connected,
)
from core.warp_manager import (
    connect_warp, disconnect_warp, get_warp_cli,
    _set_warp_masque_mode, update_tray_icon, update_tray_icon_restore,
)

logger = logging.getLogger('wifi_tray')


def _js_escape(s):
    """JS 字符串转义"""
    return json.dumps(str(s), ensure_ascii=False)


def _is_cancelled():
    """检查认证是否已取消"""
    return _auth_cancelled.is_set()


def _check_cancel():
    """检查取消状态，如果已取消返回 True"""
    if _auth_cancelled.is_set():
        logger.info("Operation cancelled by user")
        return True
    return False


def _interruptible_sleep(seconds, check_interval=0.5):
    """可中断的睡眠，被取消时返回 False"""
    elapsed = 0.0
    while elapsed < seconds:
        if _auth_cancelled.is_set():
            return False
        sleep_time = min(check_interval, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    return True


def disable_ipv4(interface_name):
    """禁用指定接口的 IPv4，返回 bool 表示成功与否"""
    logger.info(f"disable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Disable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False)
    logger.debug(f"disable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name}")
        return True
    logger.warning(f"disable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd)
    logger.debug(f"disable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name} (elevated)")
        return True
    logger.error(f"disable_ipv4: failed to disable IPv4 on {interface_name} (both methods)")
    return False


def enable_ipv4(interface_name):
    """启用指定接口的 IPv4，返回 bool 表示成功与否"""
    logger.info(f"enable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Enable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False)
    logger.debug(f"enable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name}")
        return True
    logger.warning(f"enable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd)
    logger.debug(f"enable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name} (elevated)")
        return True
    logger.error(f"enable_ipv4: failed to enable IPv4 on {interface_name} (both methods)")
    return False


def portal_login():
    """执行门户认证，返回 (bool, str) 表示 (是否成功, 消息)"""
    # CONFIG 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import CONFIG
    username = CONFIG.get('username', '')
    password = CONFIG.get('password', '')
    portal_ip = CONFIG.get('portal_ip', '10.21.221.98')
    portal_port = CONFIG.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    wait_for_network_ready(portal_ip, portal_port)
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    import urllib.request
    import urllib.parse
    url = f"http://{portal_addr}/eportal/portal/login"
    full_account = username + "@campus"
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': full_account,
        'user_password': password,
        'wlan_user_ip': local_ip,
        'wlan_user_ipv6': '',
        'wlan_user_mac': mac_addr,
        'wlan_ac_ip': '',
        'wlan_ac_name': '',
        'jsVersion': '4.2.1',
        'terminal_type': '1',
        'lang': 'zh-cn',
        'v': '9171'
    }
    # 安全修复：仅记录密码长度，不记录密码前缀、加密状态、含密码的 URL
    logger.info(f"portal_login: username='{username}', password_len={len(password)}")
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    logger.info(f"Login URL length: {len(full_url)}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _attempt in range(3):
        try:
            req = urllib.request.Request(full_url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
            req.add_header('Referer', f'http://{portal_addr}/eportal/portal.jsp')
            response = opener.open(req, timeout=10)
            result = response.read().decode('utf-8')
            logger.info(f"Response: {result}")
            if '"result":1' in result or '"result": 1' in result:
                return True, "认证成功"
            elif '已经在线' in result or 'already online' in result or '"ret_code":2' in result:
                return True, "IP已经在线"
            elif 'AC' in result:
                return False, "AC认证失败"
            else:
                return False, f"认证失败: {result[:100]}"
        except urllib.error.HTTPError as e:
            if e.code in (502, 503) and _attempt < 2:
                logger.warning(f"Portal login got HTTP {e.code}, retrying ({_attempt+1}/3)...")
                if not _interruptible_sleep(3): return False, "已取消"
                continue
            return False, f"认证请求失败: HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            if _attempt < 2 and ('timed out' in str(e) or 'Connection' in str(e)):
                logger.warning(f"Portal login error, retrying ({_attempt+1}/3): {e}")
                if not _interruptible_sleep(3): return False, "已取消"
                continue
            return False, f"认证请求失败: {e}"


def portal_logout():
    """执行门户注销，返回 bool 表示成功与否"""
    # CONFIG 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import CONFIG
    username = CONFIG.get('username', '')
    password = CONFIG.get('password', '')
    portal_ip = CONFIG.get('portal_ip', '10.21.221.98')
    portal_port = CONFIG.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    import urllib.request
    import urllib.parse
    url = f"http://{portal_addr}/eportal/portal/logout"
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': username + "@campus",
        'user_password': password,
        'ac_logout': '0',
        'register_mode': '0',
        'wlan_user_ip': local_ip,
        'wlan_user_ipv6': '',
        'wlan_vlan_id': '0',
        'wlan_user_mac': mac_addr,
        'wlan_ac_ip': '',
        'wlan_ac_name': '',
        'jsVersion': '4.2.1',
        'v': '7724',
        'lang': 'zh'
    }
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    logger.info(f"Logout URL: {full_url[:80]}...")
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Accept', '*/*')
        req.add_header('Referer', f'http://{portal_addr}/eportal/portal.jsp')
        req.add_header('Connection', 'keep-alive')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = opener.open(req, timeout=15)
        result = response.read().decode('utf-8')
        logger.info(f"Logout response: {result}")
        _interruptible_sleep(3)
        return True
    except Exception as e:
        logger.error(f"Logout failed: {e}")
        return False


def _push_auth_progress(step, total, message, status='running', action='auth'):
    """推送认证进度到前端"""
    try:
        if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
            js_code = f"onAuthProgress({{step:{step}, total:{total}, message:{_js_escape(message)}, status:{_js_escape(status)}, action:{_js_escape(action)}}})"
            core.state._tray_app_instance.settings_window.evaluate_js(js_code)
            logger.debug(f"push_auth_progress: step={step}/{total}, status={status}, action={action}, msg={message}")
    except Exception as e:
        logger.error(f"push_auth_progress failed: {e}")


def run_auth_task():
    """执行认证任务（完整流程），返回 (bool, str) 表示 (是否成功, 消息)"""
    # CONFIG 仍由 tray_app.py 持有，使用延迟导入避免循环依赖
    from tray_app import CONFIG
    logger.info("=" * 60)
    logger.info("Starting authentication process")
    logger.info("=" * 60)
    _auth_cancelled.clear()
    wifi_name = CONFIG.get('wifi_name', '')
    if not wifi_name:
        logger.error("WiFi name not configured")
        return False, "WiFi名称未配置"
    if not CONFIG.get('username') or not CONFIG.get('password'):
        logger.error("Credentials not configured")
        return False, "账号或密码未配置"
    _push_auth_progress(0, 5, '检查WiFi连接...')
    logger.info(f"[0/5] Checking WiFi connection to: {wifi_name}")
    code, output, err = run_command('netsh wlan show interfaces')
    logger.debug(f"netsh wlan show interfaces output:\n{output[:500]}")
    wifi_connected = wifi_name in output and ("已连接" in output or "connected" in output.lower())
    if not wifi_connected:
        logger.info(f"Not connected to {wifi_name}, attempting to connect...")
        code, output, err = run_command(f'netsh wlan connect name="{wifi_name}"')
        if code != 0:
            error_detail = output.strip() or err.strip() or f"返回码={code}"
            logger.error(f"Failed to connect to WiFi: {error_detail}")
            _push_auth_progress(0, 5, f'WiFi连接失败: {error_detail}', 'error')
            return False, f"WiFi连接失败: {error_detail}"
        for retry in range(8):
            if not _interruptible_sleep(2): return False, "已取消"
            code, output, _ = run_command('netsh wlan show interfaces')
            if wifi_name in output and ("已连接" in output or "connected" in output.lower()):
                wifi_connected = True
                break
            logger.debug(f"WiFi not ready yet (check {retry+1}/8)")
        if not wifi_connected:
            logger.error(f"WiFi connected but target SSID not confirmed")
            _push_auth_progress(0, 5, f'未检测到目标网络 {wifi_name}', 'error')
            return False, f"WiFi连接后未检测到目标网络 {wifi_name}"
    if _check_cancel(): return False, "已取消"
    logger.info(f"Connected to target WiFi: {wifi_name}")
    interface_name = get_wifi_interface_name()
    if not interface_name:
        logger.error("Cannot get WiFi interface name")
        _push_auth_progress(0, 5, '无法获取WiFi接口名称', 'error')
        return False, "无法获取WiFi接口名称"
    if is_warp_connected():
        logger.info("WARP already connected, checking IPv4 status...")
        ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
        code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False)
        if 'False' in output:
            logger.info("WARP connected and IPv4 disabled, no need to re-authenticate")
            _push_auth_progress(5, 5, '已认证，WARP已连接', 'success')
            return True, "已认证，WARP已连接"
        logger.info("WARP connected but IPv4 still enabled, disabling IPv4...")
        if disable_ipv4(interface_name):
            _push_auth_progress(5, 5, '已认证，WARP已连接', 'success')
            return True, "已认证，WARP已连接"
        else:
            _push_auth_progress(5, 5, 'WARP已连接，但IPv4禁用失败', 'error')
            return False, "WARP已连接，但IPv4禁用失败"
    if _check_cancel(): return False, "已取消"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(1, 5, '断开WARP...')
    logger.info("[1/5] Checking WARP...")
    disconnect_warp(full=False)
    code, svc_output, _ = run_command('sc query "CloudflareWARP"')
    warp_service_was_running = 'RUNNING' in svc_output
    if warp_service_was_running:
        logger.info("Disabling WARP virtual adapter to release network filter...")
        run_command('netsh interface set interface "CloudflareWARP" disable')
        _interruptible_sleep(1)
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(2, 5, '启用IPv4...')
    logger.info("[2/5] Checking IPv4 status...")
    ps_cmd = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
    code, output, _ = run_command(['powershell', '-Command', ps_cmd], shell=False)
    if 'True' not in output:
        logger.info("IPv4 disabled, enabling...")
        if not enable_ipv4(interface_name):
            _push_auth_progress(2, 5, 'IPv4启用失败', 'error')
            return False, "IPv4启用失败"
        for ip_retry in range(6):
            if _check_cancel(): return False, "已取消"
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1)
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
                if ip and not ip.startswith('127.'):
                    logger.info(f"IPv4 address obtained: {ip}")
                    break
            except Exception:
                pass
            if not _interruptible_sleep(1): return False, "已取消"
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(3, 5, 'Portal认证...')
    logger.info("[3/5] Portal authentication...")
    success, msg = portal_login()
    if not success:
        if 'AC' in msg:
            logger.info("AC auth failed, logging out and retrying...")
            portal_logout()
            if not _interruptible_sleep(2): return False, "已取消"
            if _check_cancel(): return False, "已取消"
            success, msg = portal_login()
        if not success:
            _push_auth_progress(3, 5, msg, 'error')
            logger.warning("Portal auth failed, rolling back: reconnecting WARP")
            disable_ipv4(interface_name)
            if warp_service_was_running:
                run_command('netsh interface set interface "CloudflareWARP" enable')
            connect_warp()
            return False, msg
    if _check_cancel(): return False, "已取消"
    _push_auth_progress(4, 5, '设置IPv6 DNS...')
    logger.info("[4/5] Setting IPv6 DNS before disabling IPv4...")
    run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" static 2606:4700:4700::1111 primary')
    run_command(f'netsh interface ipv6 add dnsservers "{interface_name}" 2606:4700:4700::1001 index=2')
    logger.info("IPv6 DNS set to Cloudflare (2606:4700:4700::1111, 2606:4700:4700::1001)")
    if _check_cancel(): return False, "已取消"
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


def run_restore_task():
    """执行恢复任务，返回 (bool, str) 表示 (是否成功, 消息)"""
    logger.info("=" * 60)
    logger.info("Restoring normal network mode")
    logger.info("=" * 60)
    _auth_cancelled.clear()
    interface_name = get_wifi_interface_name()
    if not interface_name:
        interface_name = "WLAN"
    logger.info(f"WiFi interface: {interface_name}")
    _push_auth_progress(1, 2, '恢复网络...', action='restore')
    logger.info("[1/2] Disconnecting WARP and enabling IPv4 in parallel...")

    warp_result = [None]
    ipv4_result = [None]

    def _disconnect_warp_thread():
        warp_result[0] = disconnect_warp()

    def _enable_ipv4_thread():
        ipv4_result[0] = enable_ipv4(interface_name)
        run_command(f'netsh interface ipv6 set dnsservers "{interface_name}" dhcp')

    t_warp = threading.Thread(target=_disconnect_warp_thread, daemon=True)
    t_ipv4 = threading.Thread(target=_enable_ipv4_thread, daemon=True)
    t_warp.start()
    t_ipv4.start()
    t_warp.join()
    t_ipv4.join()

    if _check_cancel(): return False, "已取消"
    if not ipv4_result[0]:
        logger.warning("enable_ipv4 failed, rolling back: reconnecting WARP")
        connect_warp()
        _push_auth_progress(2, 2, '启用IPv4失败，已恢复WARP', 'error', action='restore')
        return False, "启用IPv4失败，已恢复WARP"
    _push_auth_progress(2, 2, '验证网络...', action='restore')
    logger.info("[2/2] Verifying network...")
    if not _interruptible_sleep(3): return False, "已取消"
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.'])
    if not has_ipv4:
        logger.warning("No valid IPv4 address found after enabling")
        _push_auth_progress(2, 2, 'IPv4未获取到有效地址', 'error', action='restore')
        return False, "IPv4未获取到有效地址"
    try:
        import urllib.request
        req = urllib.request.Request('http://www.baidu.com', method='HEAD')
        urllib.request.urlopen(req, timeout=5)
        logger.info("Network connectivity verified")
        _push_auth_progress(2, 2, '网络已恢复正常模式', 'success', action='restore')
        return True, "网络已恢复正常模式"
    except Exception as e:
        logger.warning(f"IPv4 has IP but no internet: {e}")
        _push_auth_progress(3, 3, 'IPv4已启用，但可能需要Portal认证', 'success', action='restore')
        return True, "IPv4已启用，但可能需要Portal认证"
