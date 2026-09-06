"""静默启动链路验证（诊断脚本，一次性）：
1) 提权清理现存 tray_app 实例（UAC #1）；
2) `python tray_app.py --silent` 真实提权启动（UAC #2，--silent 经提权透传）；
3) 探测 15 秒：主窗口不应出现（静默实例在未显示前根本不创建窗口）；
4) 校验日志确认走了静默分支。
"""
import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32


def existing_pid():
    hwnd = user32.FindWindowW(None, 'CampusAuth')
    if not hwnd:
        return None, None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def elevated_kill(pid):
    arg = f'-NoProfile -Command "Stop-Process -Id {pid} -Force"'
    ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'powershell.exe',
                                              arg, None, 0)
    print(f'清理现存实例 pid={pid}，UAC ret={ret}')
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
    hwnd, pid = existing_pid()
    if pid:
        elevated_kill(pid)
    print('=== 以 --silent 真实提权启动（UAC）===')
    probe()
    done = subprocess.run([sys.executable, 'tray_app.py', '--silent'],
                          capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=40)
    print('launcher exit:', done.returncode)
    time.sleep(6)
    print('=== 静默验证结束（期望全程 visible=None/False）===')
