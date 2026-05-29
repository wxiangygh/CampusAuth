import subprocess
import json
import urllib.request
import urllib.parse
import re
import sys
import time
import socket
from config import USERNAME, PASSWORD, PORTAL_SERVER, WIFI_NAME

class ProgressBar:
    """简单的命令行进度条"""
    def __init__(self, total, desc=""):
        self.total = total
        self.current = 0
        self.desc = desc
        
    def update(self, n=1):
        self.current += n
        percent = (self.current / self.total) * 100
        filled = int(30 * self.current // self.total)
        bar = '█' * filled + '░' * (30 - filled)
        sys.stdout.write(f'\r{self.desc} |{bar}| {percent:.1f}% ({self.current}/{self.total})')
        sys.stdout.flush()
        
    def finish(self):
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
    """获取WiFi接口名称"""
    code, output, _ = run_command('netsh wlan show interfaces')
    for line in output.split('\n'):
        if '名称' in line or 'Name' in line:
            return line.split(':')[1].strip()
    return None

def check_ipv4_enabled(interface_name):
    """检查IPv4是否启用"""
    print(f"\n[步骤 2/5] 检查 {interface_name} 的IPv4状态...")
    progress = ProgressBar(3, "检查进度")
    
    progress.update()
    code, output, _ = run_command(f'netsh interface ipv4 show config name="{interface_name}"')
    
    has_ipv4 = any(ip in output for ip in ['192.168.', '10.', '172.'])
    
    if has_ipv4:
        progress.finish()
        print("  IPv4已启用")
        return True
    else:
        progress.update()
        print("  IPv4未启用，正在启用...")
        run_command(f'netsh interface ipv4 set interface "{interface_name}" disabled')
        time.sleep(1)
        code, _, _ = run_command(f'netsh interface ipv4 set interface "{interface_name}" enabled')
        progress.update()
        if code == 0:
            progress.finish()
            print("  IPv4已启用")
            return True
        else:
            progress.finish()
            print("  IPv4启用失败")
            return False

def check_portal_status():
    """检查是否需要Portal认证"""
    print(f"\n[步骤 3/5] 检查Portal认证状态...")
    progress = ProgressBar(2, "检测进度")
    
    progress.update()
    try:
        req = urllib.request.Request('http://1.1.1.1/generate_204', method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0')
        response = urllib.request.urlopen(req, timeout=5)
        
        if response.status == 204:
            progress.update()
            progress.finish()
            print("  网络已认证，无需登录")
            return True, "authenticated"
        else:
            progress.update()
            progress.finish()
            print("  需要Portal认证")
            return False, "need_login"
    except urllib.error.HTTPError as e:
        progress.update()
        progress.finish()
        print(f"  需要Portal认证 (HTTP {e.code})")
        return False, "need_login"
    except Exception as e:
        progress.update()
        progress.finish()
        print(f"  需要Portal认证 (连接异常: {type(e).__name__})")
        return False, "need_login"

def portal_login():
    """进行Portal认证登录"""
    print(f"\n[步骤 4/5] 进行Portal认证...")
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
        else:
            progress.finish()
            print("  Portal认证失败")
            return False
            
    except Exception as e:
        progress.finish()
        print(f"  认证请求失败: {e}")
        return False

def disable_ipv4(interface_name):
    """禁用IPv4协议绑定（取消勾选TCP/IPv4）"""
    print(f"\n[步骤 5/5] 禁用WiFi网络的IPv4协议绑定...")
    progress = ProgressBar(2, "禁用进度")
    
    cmd = f'powershell -Command "Disable-NetAdapterBinding -Name \\"{interface_name}\\" -ComponentID ms_tcpip"'
    code, output, err = run_command(cmd)
    
    progress.update()
    if code == 0:
        progress.update()
        progress.finish()
        print("  IPv4协议已成功取消勾选")
        return True
    else:
        progress.finish()
        print(f"  禁用IPv4失败: {err}")
        print("  请尝试手动: 打开'网络连接' -> 右键WiFi -> 属性 -> 取消勾选'Internet协议版本4(TCP/IPv4)'")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("           校园网自动认证脚本 v1.0")
    print("=" * 60)
    print(f"目标WiFi: {WIFI_NAME}")
    print(f"认证账号: {USERNAME}")
    print("=" * 60)
    
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
    
    # 直接进行Portal认证（不检测状态，确保每次都认证）
    if not portal_login():
        print("\nPortal认证失败，脚本终止")
        sys.exit(1)
    
    if not disable_ipv4(interface_name):
        print("\n禁用IPv4失败")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("           所有操作完成！")
    print("=" * 60)

if __name__ == "__main__":
    main()
