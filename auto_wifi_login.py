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
    """断开 Cloudflare WARP 连接"""
    warp_cli = get_warp_cli()
    if warp_cli:
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            print("  正在断开 Cloudflare WARP...")
            run_command(warp_cli + ' disconnect')
            time.sleep(2)
            return True
    return False

def get_local_ip():
    """获取本机IPv4地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
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
    
    # Find the interface index
    code, output, _ = run_command('netsh interface ipv4 show interfaces')
    interface_idx = None
    for line in output.split('\n'):
        line_stripped = line.strip()
        if interface_name in line_stripped:
            parts = line_stripped.split()
            if parts and parts[0].isdigit():
                interface_idx = parts[0]
                print(f"  找到接口索引: {interface_idx}")
                break
    
    if not interface_idx:
        print(f"  无法找到接口索引，尝试直接使用名称...")
        interface_idx = interface_name
    
    progress.update()
    
    # Check if IPv4 is enabled
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_idx}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
    
    if has_ipv4:
        progress.update()
        progress.finish()
        print("  IPv4已启用")
        return True
    
    # IPv4 not enabled, try to enable it
    progress.update()
    print(f"\n  IPv4未启用，正在启用...")
    run_command(f'netsh interface ipv4 set interface name="{interface_idx}" enabled')
    time.sleep(3)
    
    # Check again
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_idx}"')
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
    
    if has_ipv4:
        progress.update()
        progress.finish()
        print("  IPv4已启用")
        return True
    else:
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
        'v': '7911',
        'lang': 'zh'
    }
    
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    
    try:
        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
        req.add_header('Referer', f'http://{PORTAL_SERVER}/eportal/portal.jsp')
        response = urllib.request.urlopen(req, timeout=10)
        result = response.read().decode('utf-8')
        print(f"  注销响应: {result}")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"  注销请求失败: {e}")
        return False

def portal_login():
    """进行Portal认证登录"""
    print(f"\n[步骤 3/5] 进行Portal认证...")
    print(f"  账号: {USERNAME}")
    print(f"  密码: {'*' * len(PASSWORD)}")
    
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
        # Check status
        code, output, _ = run_command(warp_cli + ' status')
        if code == 0 and ('Status update: Connected' in output or 'Network: healthy' in output):
            progress.update()
            progress.finish()
            print("  Cloudflare WARP 已连接且网络健康")
            return True
        else:
            progress.update()
            code, _, _ = run_command(warp_cli + ' connect')
            if code == 0:
                print("  等待WARP连接建立...")
                time.sleep(3)
                code, output, _ = run_command(warp_cli + ' status')
                if code == 0 and 'Network: healthy' in output:
                    progress.update()
                    progress.finish()
                    print("  Cloudflare WARP 连接成功，网络健康")
                    return True
                elif code == 0 and 'Status update: Connected' in output:
                    progress.update()
                    progress.finish()
                    print("  Cloudflare WARP 已连接")
                    return True
                else:
                    progress.finish()
                    print("  Cloudflare WARP 连接中，请稍等...")
                    return False
            else:
                progress.finish()
                print("  Cloudflare WARP 连接失败，请手动连接")
                return False
    else:
        progress.finish()
        print("  未找到 warp-cli，请确保 Cloudflare WARP 已安装")
        return False

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
    main()
