"""统一命令执行模块。

提供三种命令执行方式：
- run_command: 完整执行，支持取消，用临时文件捕获输出
- run_elevated_powershell: 提权执行 PowerShell
- run_powershell_simple: 简单执行（无取消），供 traffic_monitor 和 warp_exclusion 使用
"""
import os
import sys
import time
import uuid
import ctypes
import logging
import tempfile
import subprocess

from core.state import _auth_cancelled

logger = logging.getLogger('wifi_tray')


def run_command(cmd, shell=True, timeout=30):
    """Execute a bounded command and return ``(code, stdout, stderr)``.

    List commands always bypass the shell, so arguments such as SSIDs and file
    paths cannot be reinterpreted as command operators. Temporary binary files
    avoid pipe deadlocks and work reliably in elevated/windowless processes.
    """
    effective_shell = bool(shell and isinstance(cmd, str))
    display = cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    timed_out = False
    cancelled = False
    exit_code = -1
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            proc = subprocess.Popen(
                cmd, shell=effective_shell, stdout=stdout_file, stderr=stderr_file,
                startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + max(0.1, float(timeout))
            while proc.poll() is None:
                if _auth_cancelled.is_set():
                    cancelled = True
                    proc.kill()
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    proc.kill()
                    break
                time.sleep(0.1)
            try:
                exit_code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait(timeout=2)
        except Exception as exc:
            logger.error('run_command failed (%s): %s', str(display)[:120], exc)
            return -1, '', str(exc)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = _decode_output(stdout_file.read())
        stderr = _decode_output(stderr_file.read())
    if cancelled:
        return -1, stdout, stderr or 'Command cancelled'
    if timed_out:
        return -1, stdout, stderr or f'Command timed out after {timeout:g}s'
    return exit_code, stdout, stderr


def _decode_output(data):
    if not data:
        return ''
    for encoding in ('utf-8', 'gbk', 'mbcs'):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', errors='replace')

def run_elevated_powershell(ps_command, timeout=30):
    logger.info(f"run_elevated_powershell: cmd={ps_command[:120]!r}")
    tmp_out = os.path.join(tempfile.gettempdir(), f'ipv6_elev_{os.getpid()}_{int(time.time()*1000)}.txt')
    tmp_err = os.path.join(tempfile.gettempdir(), f'ipv6_elev_err_{os.getpid()}_{int(time.time()*1000)}.txt')
    tmp_done = os.path.join(tempfile.gettempdir(), f'ipv6_elev_done_{os.getpid()}_{int(time.time()*1000)}.txt')
    wrapped = (
        f'$ErrorActionPreference="Stop"; '
        f'try {{ {ps_command}; "0" | Out-File -FilePath "{tmp_done}" -Encoding utf8 }} '
        f'catch {{ "1" | Out-File -FilePath "{tmp_done}" -Encoding utf8; $_.Exception.Message | Out-File -FilePath "{tmp_err}" -Encoding utf8 }}'
    )
    full_cmd = f'-ExecutionPolicy Bypass -Command "{wrapped}"'
    logger.debug(f"run_elevated_powershell: full_cmd={full_cmd[:200]!r}")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", full_cmd, None, 0
    )
    logger.debug(f"run_elevated_powershell: ShellExecuteW returned {ret}")
    if ret <= 32:
        logger.error(f"run_elevated_powershell: ShellExecuteW failed with code {ret}")
        for f in [tmp_out, tmp_err, tmp_done]:
            try: os.remove(f)
            except Exception: pass
        return -1, "", f"ShellExecuteW failed with code {ret}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(tmp_done):
            break
        time.sleep(0.3)
    else:
        logger.error(f"run_elevated_powershell: timed out after {timeout}s")
        for f in [tmp_out, tmp_err, tmp_done]:
            try: os.remove(f)
            except Exception: pass
        return -1, "", "Command timed out"
    time.sleep(0.2)
    out_text = ""
    err_text = ""
    try:
        with open(tmp_done, 'r', encoding='utf-8', errors='ignore') as f:
            exit_flag = f.read().strip()
    except Exception:
        exit_flag = "1"
    try:
        if os.path.exists(tmp_err):
            with open(tmp_err, 'r', encoding='utf-8', errors='ignore') as f:
                err_text = f.read().strip()
    except Exception:
        pass
    code = 0 if exit_flag == "0" else 1
    logger.debug(f"run_elevated_powershell: code={code}, err={err_text[:200]!r}")
    for f in [tmp_out, tmp_err, tmp_done]:
        try: os.remove(f)
        except Exception: pass
    return code, out_text, err_text


def run_powershell_simple(cmd, timeout=15):
    """简单执行 PowerShell 命令（无取消、无临时文件）。
    合并 traffic_monitor._run_ps 和 warp_exclusion._run_command。
    返回 (exit_code, stdout, stderr)。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    try:
        result = subprocess.run(
            ['powershell', '-Command', cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.returncode, result.stdout or '', result.stderr or ''
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'
    except Exception as e:
        return -1, '', str(e)


def run_command_simple(cmd, shell=False, timeout=15):
    """执行命令并返回 (exit_code, stdout, stderr)，避免窗口弹窗。
    通用命令执行，支持 shell 参数和 list 形式的 cmd。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    try:
        result = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout,
            startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.returncode, result.stdout or '', result.stderr or ''
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'
    except Exception as e:
        return -1, '', str(e)
