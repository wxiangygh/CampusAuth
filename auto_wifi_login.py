import subprocess
import json
import urllib.request
import urllib.parse
import re
import sys
import time
import socket
import ctypes
from config import USERNAME, PASSWORD, PORTAL_SERVER, WIFI_NAME

def check_single_instance():
    """使用Windows mutex确保只有一个实例运行"""
    mutex_name = "Global\\WiFiAutoAuthScript_Mutex"
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    last_error = ctypes.get_last_error()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        return False
    return mutex

class ProgressBar:
    """简单的命令行进度条"""
    def __init__(self, total, desc=""):
        self.total = total
        self.current = 0
        self.desc = desc
        
    def update(self, n=1):
        self.current += n
        percent = min((self.current / self.total) * 100, 100.0)
        filled = int(30 * min(self.current, self.total) // self.total)
        bar = '█' * filled + '░' * (30 - filled)
        sys.stdout.write(f'\r{self.desc} |{bar}| {percent:.1f}% ({min(self.current, self.total)}/{self.total})')
        sys.stdout.flush()
        
    def finish(self):
        self.current = self.total
        self.update(0)
        print()

def run_command(cmd, shell=True):
    """执行命令并返回输出"""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

def get_warp_cli():
    """查找 warp-cli 路径"""
    warp_cli_paths = [
        'warp-cli',
        r'"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"',
        r'"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe"'
    ]
    for path in warp_cli_paths:
        code, _, _ = run_command(path + ' --version')
        if code == 0:
            return path
    return None

def disconnect_warp():
    """断开 Cloudflare WARP 连接并终止进程"""
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            print("  正在断开 Cloudflare WARP...")
            run_command(warp_cli + ' disconnect')
            time.sleep(2)
    # Kill Cloudflare WARP process to prevent auto-reconnect
    print("  正在终止 Cloudflare WARP 进程...")
    run_command('taskkill /F /IM "Cloudflare WARP.exe" 2>nul')
    run_command('taskkill /F /IM "warp-svc.exe" 2>nul')
    run_command('taskkill /F /IM "Cloudflare WARP Notification.exe" 2>nul')
    time.sleep(3)
    
    # Wait for CloudflareWARP interface to disappear
    print("  等待WARP网络接口消失...")
    for i in range(10):
        code, output, _ = run_command('netsh interface ipv4 show interfaces')
        if 'CloudflareWARP' not in output:
            print(f"  WARP网络接口已消失（{i+1}次检测）")
            return True
        time.sleep(2)
    
    # If interface still exists, try to disable it
    print("  WARP接口仍存在，尝试强制禁用...")
    run_command('netsh interface ipv4 set interface "CloudflareWARP" disabled')
    run_command('netsh interface set interface "CloudflareWARP" disable')
    time.sleep(3)
    return True

def get_wifi_interface_name():
    """获取WiFi接口的实际名称（适配器名称）"""
    code, output, _ = run_command('netsh wlan show interfaces')
    for line in output.split('\n'):
        line = line.strip()
        # Look for "名称" (Chinese) or "Name" (English)
        if (line.startswith('名称') or line.startswith('Name')) and ':' in line:
            return line.split(':', 1)[1].strip()
    return None

def get_local_ip():
    """获取WiFi接口的IPv4地址（排除WARP等虚拟网卡）"""
    # Use ipconfig to get WiFi interface IP specifically
    wifi_name = get_wifi_interface_name()
    if wifi_name:
        code, output, _ = run_command('ipconfig')
        lines = output.split('\n')
        found_wifi = False
        for line in lines:
            line_stripped = line.strip()
            if wifi_name in line_stripped or '无线' in line_stripped or 'Wireless' in line_stripped:
                found_wifi = True
                continue
            if found_wifi and ('IPv4' in line_stripped or 'IPv4 地址' in line_stripped) and ':' in line_stripped:
                ip = line_stripped.split(':', 1)[1].strip()
                # Skip WARP IPs (172.16.x.x)
                if ip and not ip.startswith('172.16.'):
                    return ip
                # Found WiFi section but IP is WARP, continue looking
                continue
            if found_wifi and line_stripped == '':
                found_wifi = False
    
    # Fallback: use socket method but filter out WARP IPs
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith('172.16.'):
            return ''  # WARP IP, not useful
        return ip
    except:
        return ''

def get_mac_address():
    """获取本机MAC地址"""
    code, output, _ = run_command('getmac /fo csv /nh')
    for line in output.split('\n'):
        if line.strip():
            parts = line.split(',')
            mac = parts[0].strip().strip('"').replace('-', '')
            return mac
    return '000000000000'

def connect_wifi(wifi_name):
    """连接到指定WiFi"""
    print(f"\n[步骤 1/5] 正在连接到WiFi: {wifi_name}")
    progress = ProgressBar(3, "连接进度")
    
    progress.update()
    code, output, _ = run_command('netsh wlan show interfaces')
    if output and wifi_name in output and "已连接" in output:
        progress.finish()
        print(f"  已连接到 {wifi_name}")
        return True
    
    progress.update()
    code, output, _ = run_command(f'netsh wlan connect name="{wifi_name}"')
    if code == 0:
        time.sleep(2)
        progress.update()
        progress.finish()
        print(f"  成功连接到 {wifi_name}")
        return True
    else:
        progress.finish()
        print(f"  连接失败: {output}")
        return False

def get_wifi_interface():
    """获取WiFi接口名称（适配器名称，不是配置文件名称）"""
    code, output, _ = run_command('netsh wlan show interfaces')
    for line in output.split('\n'):
        line = line.strip()
        if (line.startswith('名称') or line.startswith('Name')) and ':' in line:
            return line.split(':', 1)[1].strip()
    return None

def check_ipv4_enabled(interface_name):
    """检查IPv4是否启用，未启用则自动开启"""
    print(f"\n[步骤 2/5] 检查 {interface_name} 的IPv4状态...")
    
    progress = ProgressBar(4, "检查进度")
    progress.update()
    
    # Check IPv4 binding status using PowerShell
    code, output, _ = run_command(f'powershell -Command "(Get-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip).Enabled"')
    
    if 'True' in output:
        progress.update()
        progress.update()
        progress.update()
        progress.finish()
        print("  IPv4已启用")
        return True
    
    # IPv4 is disabled (binding unchecked), enable it
    progress.update()
    print(f"\n  IPv4未启用（协议绑定未勾选），正在启用...")
    
    # Use PowerShell to enable IPv4 binding (check the box in network properties)
    code, output, err = run_command(f'powershell -Command "Enable-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip"')
    
    progress.update()
    if code == 0:
        print("  IPv4协议绑定已启用，等待网络重新配置...")
        time.sleep(5)
        
        # Check again to get the interface index
        code, output, _ = run_command('netsh interface ipv4 show interfaces')
        interface_idx = None
        for line in output.split('\n'):
            line_stripped = line.strip()
            if interface_name in line_stripped:
                parts = line_stripped.split()
                if parts and parts[0].isdigit():
                    interface_idx = parts[0]
                    break
        
        if interface_idx:
            code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_idx}"')
            has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
            if has_ipv4:
                progress.update()
                progress.finish()
                print("  IPv4已启用且获取到IP地址")
                return True
        
        # If no IP yet, try renewing
        run_command(f'ipconfig /release "{interface_name}"')
        time.sleep(2)
        run_command(f'ipconfig /renew "{interface_name}"')
        time.sleep(5)
        
        code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
        has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
        
        if has_ipv4:
            progress.update()
            progress.finish()
            print("  IPv4已启用")
            return True
        else:
            progress.finish()
            print("  IPv4绑定已启用，但未获取到IP地址")
            return True  # Binding is enabled, IP may come later
    
    progress.finish()
    print("  IPv4启用失败")
    return False

def portal_logout():
    """注销Portal认证"""
    print("  检测到AC认证失败，正在注销...")
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    
    url = f"http://{PORTAL_SERVER}/eportal/portal/logout"
    
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': USERNAME + "@campus",
        'user_password': PASSWORD,
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
    
    print(f"  注销URL: {full_url}")
    
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Accept', '*/*')
        req.add_header('Referer', f'http://{PORTAL_SERVER}/eportal/portal.jsp')
        req.add_header('Connection', 'keep-alive')
        response = urllib.request.urlopen(req, timeout=15)
        result = response.read().decode('utf-8')
        print(f"  注销响应: {result}")
        time.sleep(3)
        return True
    except Exception as e:
        print(f"  注销请求失败: {e}")
        print("  注销失败不影响主流程，继续尝试重新认证...")
        return False  # Return False but caller should still retry

def wait_for_network_ready(portal_server, max_retries=5):
    """等待网络连通（能ping通Portal服务器）"""
    print("\n  等待网络连通...")
    for i in range(max_retries):
        code, _, _ = run_command(f'ping -n 1 -w 1000 {portal_server}')
        if code == 0:
            print(f"  网络已连通（{i+1}/{max_retries}次检测成功）")
            return True
        print(f"  等待中... ({i+1}/{max_retries})")
        time.sleep(3)
    print("  网络可能未完全连通，继续尝试认证...")
    return False

def portal_login():
    """进行Portal认证登录"""
    print(f"\n[步骤 3/5] 进行Portal认证...")
    print(f"  账号: {USERNAME}")
    print(f"  密码: {'*' * len(PASSWORD)}")
    
    # Wait for network to be ready before attempting auth
    wait_for_network_ready(PORTAL_SERVER)
    
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    print(f"  本机IP: {local_ip}")
    print(f"  MAC地址: {mac_addr}")
    
    progress = ProgressBar(3, "认证进度")
    
    url = f"http://{PORTAL_SERVER}/eportal/portal/login"
    full_account = USERNAME + "@campus"
    
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': full_account,
        'user_password': PASSWORD,
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
    
    progress.update()
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    
    print(f"  请求URL: {full_url[:80]}...")
    
    try:
        progress.update()
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Referer', f'http://{PORTAL_SERVER}/eportal/portal.jsp')
        response = urllib.request.urlopen(req, timeout=10)
        result = response.read().decode('utf-8')
        
        print(f"  服务器响应: {result}")
        
        if '"result":1' in result or '"result": 1' in result:
            progress.update()
            progress.finish()
            print("  Portal认证成功！")
            return True
        elif '已经在线' in result or 'already online' in result or '"ret_code":2' in result:
            progress.update()
            progress.finish()
            print("  IP已经在线，无需重复认证")
            return True
        elif 'AC' in result:
            # AC认证失败，先注销再重新认证
            progress.finish()
            print("  检测到AC认证失败，尝试注销后重新认证...")
            if portal_logout():
                print("  注销成功，正在重新认证...")
                time.sleep(2)
                # 递归调用重新认证（只递归一次）
                return portal_login_retry()
            else:
                print("  注销失败，重新认证...")
                return portal_login_retry()
        else:
            progress.finish()
            print("  Portal认证失败")
            return False
            
    except Exception as e:
        progress.finish()
        print(f"  认证请求失败: {e}")
        return False

def portal_login_retry():
    """重新进行Portal认证（注销后调用）"""
    time.sleep(2)  # Small delay after logout
    wait_for_network_ready(PORTAL_SERVER, max_retries=3)
    
    local_ip = get_local_ip()
    mac_addr = get_mac_address()
    
    url = f"http://{PORTAL_SERVER}/eportal/portal/login"
    full_account = USERNAME + "@campus"
    
    params = {
        'callback': 'dr1003',
        'login_method': '1',
        'user_account': full_account,
        'user_password': PASSWORD,
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
    
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    
    print(f"  重新认证请求URL: {full_url[:80]}...")
    
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Referer', f'http://{PORTAL_SERVER}/eportal/portal.jsp')
        response = urllib.request.urlopen(req, timeout=10)
        result = response.read().decode('utf-8')
        
        print(f"  重新认证响应: {result}")
        
        if '"result":1' in result or '"result": 1' in result:
            print("  Portal认证成功！")
            return True
        elif '已经在线' in result or 'already online' in result or '"ret_code":2' in result:
            print("  IP已经在线，无需重复认证")
            return True
        else:
            print("  重新认证失败")
            return False
            
    except Exception as e:
        print(f"  重新认证请求失败: {e}")
        return False

def disable_ipv4(interface_name):
    """禁用IPv4协议绑定（取消勾选TCP/IPv4）"""
    print(f"\n[步骤 4/5] 禁用WiFi网络的IPv4协议绑定...")
    
    progress = ProgressBar(2, "禁用进度")
    
    # Try PowerShell method first
    cmd = f'powershell -Command "Disable-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip"'
    code, output, err = run_command(cmd)
    
    progress.update()
    if code == 0:
        progress.update()
        progress.finish()
        print("  IPv4协议已成功取消勾选")
        return True
    else:
        # Fallback: try netsh method
        print(f"  PowerShell方法失败，尝试netsh...")
        code, output, _ = run_command(f'netsh interface ipv4 set interface "{interface_name}" disabled')
        if code == 0:
            progress.update()
            progress.finish()
            print("  IPv4已通过netsh禁用")
            return True
        else:
            progress.finish()
            print(f"  禁用IPv4失败")
            print("  请尝试手动: 打开'网络连接' -> 右键WiFi -> 属性 -> 取消勾选'Internet协议版本4(TCP/IPv4)'")
            return False

def connect_warp():
    """连接 Cloudflare WARP"""
    print("\n[步骤 5/5] 连接 Cloudflare WARP...")
    progress = ProgressBar(3, "WARP连接")
    progress.update()
    
    warp_cli = get_warp_cli()
    
    if warp_cli:
        progress.update()
        # Check if WARP service is running, start it if not
        code, svc_output, _ = run_command('sc query "warp-svc"')
        if 'RUNNING' not in svc_output:
            print("  正在启动 Cloudflare WARP 服务...")
            # Launch the WARP GUI app which auto-starts the service and auto-connects
            run_command(r'start "" "C:\Program Files\Cloudflare\Cloudflare WARP\Cloudflare WARP.exe"')
        
        # Poll for WARP connection status (don't call connect, let GUI auto-connect)
        print("  等待WARP自动连接...")
        for i in range(15):  # Up to 45 seconds
            time.sleep(3)
            code, output, _ = run_command(warp_cli + ' status')
            if code == 0 and ('Network: healthy' in output or 'Status update: Connected' in output):
                progress.update()
                progress.finish()
                print(f"  Cloudflare WARP 连接成功（{i+1}次检测）")
                return True
        
        progress.finish()
        print("  Cloudflare WARP 未在预期时间内连接，请手动检查")
        return True  # Don't fail the script
    else:
        progress.finish()
        print("  未找到 warp-cli，请确保 Cloudflare WARP 已安装")
        return False

def restore_normal():
    """恢复到正常模式：断开WARP，启用IPv4"""
    print("=" * 60)
    print("           恢复网络到正常模式")
    print("=" * 60)
    
    # Step 1: Disconnect WARP
    print("\n[步骤 1/3] 断开 Cloudflare WARP...")
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            print("  WARP已连接，正在断开...")
            run_command(warp_cli + ' disconnect')
            time.sleep(3)
            print("  WARP已断开")
        else:
            print("  WARP未连接，跳过")
    else:
        print("  未找到 warp-cli")
    
    # Step 2: Get WiFi interface name
    interface_name = get_wifi_interface()
    if not interface_name:
        print("\n无法获取WiFi接口名称，尝试使用WLAN...")
        interface_name = "WLAN"
    print(f"\n  WiFi接口: {interface_name}")
    
    # Step 3: Enable IPv4
    print("\n[步骤 2/3] 启用IPv4协议...")
    progress = ProgressBar(2, "启用进度")
    progress.update()
    
    # Try PowerShell method first
    cmd = f'powershell -Command "Enable-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip"'
    code, output, err = run_command(cmd)
    
    progress.update()
    if code == 0:
        progress.finish()
        print("  IPv4协议已启用（通过PowerShell）")
    else:
        # Fallback: try netsh method
        print(f"  PowerShell方法失败，尝试netsh...")
        code, output, _ = run_command(f'netsh interface ipv4 set interface "{interface_name}" enabled')
        if code == 0:
            progress.finish()
            print("  IPv4已通过netsh启用")
        else:
            progress.finish()
            print("  启用IPv4失败，请手动启用")
    
    # Step 4: Verify
    print("\n[步骤 3/3] 验证网络状态...")
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
    
    if has_ipv4:
        print("  IPv4已启用且获取到IP地址")
    else:
        print("  IPv4可能未正确配置，请检查网络连接")
    
    print("\n" + "=" * 60)
    print("           网络已恢复到正常模式！")
    print("=" * 60)

def main():
    """主函数"""
    if not check_single_instance():
        print("另一个实例正在运行，退出...")
        sys.exit(1)
    
    print("=" * 60)
    print("           校园网自动认证脚本 v1.0")
    print("=" * 60)
    print(f"目标WiFi: {WIFI_NAME}")
    print(f"认证账号: {USERNAME}")
    print("=" * 60)
    
    # Step 0: Disconnect WARP if connected
    print("\n[步骤 0/5] 检查并断开 Cloudflare WARP...")
    disconnect_warp()
    
    if not connect_wifi(WIFI_NAME):
        print("\nWiFi连接失败，脚本终止")
        sys.exit(1)
    
    interface_name = get_wifi_interface()
    if not interface_name:
        print("\n无法获取WiFi接口名称")
        sys.exit(1)
    print(f"\n  WiFi接口: {interface_name}")
    
    if not check_ipv4_enabled(interface_name):
        print("\nIPv4启用失败，脚本终止")
        sys.exit(1)
    
    print("\n  等待IP分配...")
    progress = ProgressBar(5, "分配进度")
    for i in range(5):
        progress.update()
        time.sleep(0.6)
    progress.finish()
    
    # Portal authentication
    if not portal_login():
        print("\nPortal认证失败，脚本终止")
        sys.exit(1)
    
    # Disable IPv4 (must do this before connecting WARP)
    if not disable_ipv4(interface_name):
        print("\n禁用IPv4失败")
        sys.exit(1)
    
    # Connect WARP
    if not connect_warp():
        print("\nWARP连接失败")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("           所有操作完成！")
    print("=" * 60)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == '--restore':
        restore_normal()
    else:
        main()
