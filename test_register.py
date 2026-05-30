import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

def _build_schtasks_tr(extra_args=''):
    if getattr(sys, 'frozen', False):
        exe_path = sys.executable
        tr = f'"{exe_path}"'
        if extra_args:
            tr += f' {extra_args}'
    else:
        python_exe = sys.executable
        script = str(SCRIPT_DIR / 'tray_app.py')
        tr = f'"{python_exe}" "{script}"'
        if extra_args:
            tr += f' {extra_args}'
    return tr

si = subprocess.STARTUPINFO()
si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
si.wShowWindow = 0

tr_value = _build_schtasks_tr('--wifi-event')
event_filter = "*[System[Provider[@Name='Microsoft-Windows-WLAN-AutoConfig'] and EventID=8001]]"
cmd_list = ['schtasks', '/Create', '/TN', 'WiFiAutoAuthEvent', '/TR', tr_value, '/SC', 'ONEVENT', '/EC', 'Security', '/MO', event_filter, '/RL', 'HIGHEST', '/F']

lines = []
lines.append(f"TR: {tr_value}")
lines.append(f"cmd: {' '.join(cmd_list)}")

result = subprocess.run(
    cmd_list,
    capture_output=True, text=True,
    encoding='utf-8', errors='ignore',
    startupinfo=si,
    creationflags=subprocess.CREATE_NO_WINDOW
)
lines.append(f"Return code: {result.returncode}")
lines.append(f"stdout: {result.stdout}")
lines.append(f"stderr: {result.stderr}")

result2 = subprocess.run(
    ['schtasks', '/Query', '/TN', 'WiFiAutoAuthEvent', '/V', '/FO', 'LIST'],
    capture_output=True, text=True,
    encoding='utf-8', errors='ignore',
    startupinfo=si,
    creationflags=subprocess.CREATE_NO_WINDOW
)
lines.append(f"Query code: {result2.returncode}")
lines.append(f"Query stdout: {result2.stdout}")

with open(r'd:\project_code\ipv6\test_register_result.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
