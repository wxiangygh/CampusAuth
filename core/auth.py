"""认证流程模块。

包含校园网门户认证、IPv4 禁用/启用、认证任务编排等功能。
"""
import json
import time
import threading
import logging

import core.state
from core.state import _auth_cancelled
from core.command import run_command, run_elevated_powershell
from core.network import (
    get_wifi_interface_name, get_local_ip, get_mac_address,
    wait_for_network_ready, _wait_for_ipv6_ready, is_warp_connected,
    has_public_ipv6,
)
from core.warp_manager import (
    connect_warp, disconnect_warp, get_warp_cli,
    _set_warp_masque_mode, _set_warp_endpoint_ipv6,
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


def disable_ipv4(interface_name, timeout=15):
    """禁用指定接口的 IPv4，返回 bool 表示成功与否"""
    logger.info(f"disable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Disable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False, timeout=timeout)
    logger.debug(f"disable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name}")
        return True
    logger.warning(f"disable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd, timeout=timeout)
    logger.debug(f"disable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"disable_ipv4: IPv4 disabled on {interface_name} (elevated)")
        return True
    logger.error(f"disable_ipv4: failed to disable IPv4 on {interface_name} (both methods)")
    return False


def enable_ipv4(interface_name, timeout=15):
    """启用指定接口的 IPv4，返回 bool 表示成功与否"""
    logger.info(f"enable_ipv4: interface={interface_name!r}")
    ps_cmd = f'Enable-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip'
    code, output, err = run_command(['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], shell=False, timeout=timeout)
    logger.debug(f"enable_ipv4: direct PowerShell code={code}, err={err[:200]!r}")
    if code == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name}")
        return True
    logger.warning(f"enable_ipv4: direct PowerShell failed, trying elevated...")
    code2, output2, err2 = run_elevated_powershell(ps_cmd, timeout=timeout)
    logger.debug(f"enable_ipv4: elevated PowerShell code={code2}, err={err2[:200]!r}")
    if code2 == 0:
        logger.info(f"enable_ipv4: IPv4 enabled on {interface_name} (elevated)")
        return True
    logger.error(f"enable_ipv4: failed to enable IPv4 on {interface_name} (both methods)")
    return False


def portal_login(config=None, timeout=8):
    """Perform one bounded portal login attempt.

    Retry belongs to the workflow runner so the total duration remains
    predictable and every retry is visible to the user.
    """
    config = config or __import__('core.config', fromlist=['get_config']).get_config()
    username = config.get('username', '')
    password = config.get('password', '')
    portal_ip = config.get('portal_ip', '10.21.221.98')
    portal_port = config.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    if not username or not password:
        return False, "账号或密码未配置"
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    import re
    import urllib.error
    import urllib.parse
    import urllib.request

    params = {
        'callback': 'dr1003', 'login_method': '1',
        'user_account': username + '@campus', 'user_password': password,
        'wlan_user_ip': local_ip, 'wlan_user_ipv6': '', 'wlan_user_mac': mac_addr,
        'wlan_ac_ip': '', 'wlan_ac_name': '', 'jsVersion': '4.2.1',
        'terminal_type': '1', 'lang': 'zh-cn', 'v': '9171'
    }
    full_url = (f"http://{portal_addr}/eportal/portal/login?" +
                urllib.parse.urlencode(params))
    logger.info("Portal login: user=%r, password_len=%d, url_len=%d",
                username, len(password), len(full_url))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(full_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': f'http://{portal_addr}/eportal/portal.jsp',
        })
        response = opener.open(request, timeout=max(1, min(float(timeout), 8)))
        text = response.read().decode('utf-8', errors='replace')
        # Portal responses are commonly JSONP. Match fields instead of relying
        # on one exact spacing variant.
        if re.search(r'"result"\s*:\s*1', text):
            return True, '认证成功'
        lower = text.lower()
        if '已经在线' in text or 'already online' in lower or re.search(r'"ret_code"\s*:\s*2', text):
            return True, 'IP已经在线'
        if 'AC认证' in text or re.search(r'\bAC\b', text):
            return False, 'AC认证失败，请稍后重试'
        return False, f"认证被门户拒绝：{text[:120]}"
    except urllib.error.HTTPError as exc:
        retryable = exc.code in (408, 429, 500, 502, 503, 504)
        prefix = '门户暂时不可用' if retryable else '认证请求被拒绝'
        return False, f"{prefix}（HTTP {exc.code}）"
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        return False, f"认证服务器连接失败：{exc}"
    except Exception as exc:
        logger.exception('Unexpected portal login failure')
        return False, f"认证请求异常：{exc}"


def portal_logout(config=None, timeout=6):
    """Perform a bounded best-effort portal logout."""
    config = config or __import__('core.config', fromlist=['get_config']).get_config()
    username = config.get('username', '')
    password = config.get('password', '')
    portal_ip = config.get('portal_ip', '10.21.221.98')
    portal_port = config.get('portal_port', '801')
    portal_addr = f"{portal_ip}:{portal_port}" if portal_port else portal_ip
    import urllib.parse
    import urllib.request
    params = {
        'callback': 'dr1003', 'login_method': '1',
        'user_account': username + '@campus', 'user_password': password,
        'ac_logout': '0', 'register_mode': '0', 'wlan_user_ip': get_local_ip(),
        'wlan_user_ipv6': '', 'wlan_vlan_id': '0',
        'wlan_user_mac': get_mac_address(), 'wlan_ac_ip': '', 'wlan_ac_name': '',
        'jsVersion': '4.2.1', 'v': '7724', 'lang': 'zh'
    }
    full_url = (f"http://{portal_addr}/eportal/portal/logout?" +
                urllib.parse.urlencode(params))
    try:
        request = urllib.request.Request(full_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': f'http://{portal_addr}/eportal/portal.jsp',
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(request, timeout=max(1, min(float(timeout), 6))).read()
        return True
    except Exception as exc:
        logger.warning('Portal logout failed: %s', exc)
        return False

def _push_auth_progress(step, total, message, status='running', action='auth'):
    """推送认证进度到前端（带操作纪元，前端只接受最新纪元的进度）"""
    try:
        from core.app_state import app_state
        op_id = app_state.snapshot().get('operation', {}).get('operation_id', 0)
        if core.state._tray_app_instance and core.state._tray_app_instance.settings_window:
            js_code = (f"onAuthProgress({{step:{step}, total:{total}, message:{_js_escape(message)}, "
                       f"status:{_js_escape(status)}, action:{_js_escape(action)}, operationId:{op_id}}})")
            core.state._tray_app_instance.settings_window.evaluate_js(js_code)
            logger.debug(f"push_auth_progress: step={step}/{total}, status={status}, action={action}, op={op_id}, msg={message}")
    except Exception as e:
        logger.error(f"push_auth_progress failed: {e}")


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
    # 3 个固定阶段，total 恒为 3，进度单调递增不回退：
    # 1=断开WARP+启用IPv4  2=验证IPv4地址  3=验证互联网连通
    _push_auth_progress(1, 3, '断开 WARP 并启用 IPv4...', action='restore')
    logger.info("[1/3] Disconnecting WARP and enabling IPv4 in parallel...")

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
        _push_auth_progress(1, 3, '启用IPv4失败，已恢复WARP', 'error', action='restore')
        return False, "启用IPv4失败，已恢复WARP"
    _push_auth_progress(2, 3, '验证 IPv4 地址...', action='restore')
    logger.info("[2/3] Verifying network...")
    if not _interruptible_sleep(3): return False, "已取消"
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.'])
    if not has_ipv4:
        logger.warning("No valid IPv4 address found after enabling")
        _push_auth_progress(2, 3, 'IPv4未获取到有效地址', 'error', action='restore')
        return False, "IPv4未获取到有效地址"
    try:
        import urllib.request
        req = urllib.request.Request('http://www.baidu.com', method='HEAD')
        urllib.request.urlopen(req, timeout=5)
        logger.info("Network connectivity verified")
        _push_auth_progress(3, 3, '网络已恢复正常模式', 'success', action='restore')
        return True, "网络已恢复正常模式"
    except Exception as e:
        logger.warning(f"IPv4 has IP but no internet: {e}")
        _push_auth_progress(3, 3, 'IPv4已启用，但可能需要Portal认证', 'success', action='restore')
        return True, "IPv4已启用，但可能需要Portal认证"


def run_auth_task():
    """Run the configured authentication workflow."""
    from core.auth_workflow import run_auth_workflow
    return run_auth_workflow()
