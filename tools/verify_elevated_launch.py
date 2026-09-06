"""提权链路验证（诊断脚本，一次性）：
1) 列出并提权清理残留的 tray_app.py 实例（UAC #1）；
2) 以真实路径 `python tray_app.py` 启动（内部 ShellExecuteW runas，UAC #2）；
3) 每秒探测 CampusAuth 窗口可见性 15 秒，验证修复后窗口不再闪退。
"""
import ctypes
import subprocess
import sys
import threading
import time

user32 = ctypes.windll.user32


def list_instances():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -match 'tray_app[.]py' } | "
          "Select-Object ProcessId | Format-List")
    out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                         capture_output=True, timeout=30)
    text = out.stdout.decode('gbk', errors='replace')
    pids = [line.split(':')[-1].strip() for line in text.splitlines()
            if line.strip().startswith('ProcessId')]
    return [p for p in pids if p.isdigit()]


def elevated_kill(pids):
    if not pids:
        print('无残留实例，跳过清理')
        return
    arg = f"-NoProfile -Command \"Stop-Process -Id {','.join(pids)} -Force\""
    ret = user32.ShellExecuteW(None, 'runas', 'powershell.exe', arg, None, 0)
    print(f'清理 {pids}，UAC ShellExecuteW ret={ret}')
    time.sleep(5)


def probe(seconds=15):
    def run():
        for i in range(seconds):
            time.sleep(1)
            hwnd = user32.FindWindowW(None, 'CampusAuth')
            vis = bool(user32.IsWindowVisible(hwnd)) if hwnd else None
            print(f'probe {i+1}s: hwnd={hwnd or 0} visible={vis}', flush=True)
    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    pids = list_instances()
    print('残留 tray_app 实例 pid:', pids)
    elevated_kill(pids)
    print('=== 以真实提权路径启动（UAC #2）===')
    probe()
    done = subprocess.run([sys.executable, 'tray_app.py'],
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=40)
    print('launcher exit:', done.returncode)
    time.sleep(3)
    print('=== 验证结束（若期间 visible 一直 True 且保持，即修复生效）===')
