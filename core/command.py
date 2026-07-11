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

logger = logging.getLogger('tray_app')


def run_command(cmd, shell=True, timeout=30):
    """
    执行命令并返回结果。
    使用临时文件来捕获输出，避免 Windows Store 版 Python 在管理员权限下的 subprocess 管道问题。
    使用 CREATE_NO_WINDOW + SW_HIDE 彻底避免命令行窗口弹窗。
    """
    import tempfile
    import uuid

    # 构建命令字符串
    if isinstance(cmd, list):
        cmd_parts = []
        for part in cmd:
            if ' ' in part or '\t' in part:
                cmd_parts.append(f'"{part}"')
            else:
                cmd_parts.append(part)
        cmd_str = ' '.join(cmd_parts)
    else:
        cmd_str = cmd

    # 创建临时文件（使用唯一标识符避免冲突）
    unique_id = uuid.uuid4().hex
    tmp_out = os.path.join(tempfile.gettempdir(), f'cmd_out_{os.getpid()}_{unique_id}.txt')
    tmp_err = os.path.join(tempfile.gettempdir(), f'cmd_err_{os.getpid()}_{unique_id}.txt')

    # 构建重定向命令
    redirect_cmd = f'chcp 65001 >nul & {cmd_str} > "{tmp_out}" 2> "{tmp_err}"'

    # 使用 subprocess.Popen 避免窗口弹窗
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            redirect_cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            poll_interval = 0.5
            elapsed = 0.0
            exit_code = None
            while elapsed < timeout:
                exit_code = proc.poll()
                if exit_code is not None:
                    break
                if _auth_cancelled.is_set():
                    proc.kill()
                    logger.info(f"run_command: killed due to cancellation: {cmd_str[:80]}")
                    exit_code = -1
                    break
                time.sleep(poll_interval)
                elapsed += poll_interval
            if exit_code is None:
                proc.kill()
                exit_code = -1
        except Exception as e2:
            logger.error(f"run_command wait error: {e2}")
            try:
                proc.kill()
            except Exception:
                pass
            exit_code = -1
    except Exception as e:
        logger.error(f"run_command Popen error: {e}")
        exit_code = -1

    # 读取输出文件
    stdout = ''
    stderr = ''
    for _attempt in range(3):
        try:
            if os.path.exists(tmp_out):
                with open(tmp_out, 'r', encoding='utf-8', errors='replace') as f:
                    stdout = f.read()
                if '\ufffd' in stdout:
                    try:
                        with open(tmp_out, 'r', encoding='gbk', errors='replace') as f:
                            stdout = f.read()
                    except Exception:
                        pass
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass
            break
        except Exception as e:
            if _attempt < 2:
                time.sleep(0.3)
            else:
                logger.debug(f"run_command: failed to read stdout: {e}")

    for _attempt in range(3):
        try:
            if os.path.exists(tmp_err):
                with open(tmp_err, 'r', encoding='utf-8', errors='replace') as f:
                    stderr = f.read()
                if '\ufffd' in stderr:
                    try:
                        with open(tmp_err, 'r', encoding='gbk', errors='replace') as f:
                            stderr = f.read()
                    except Exception:
                        pass
                try:
                    os.remove(tmp_err)
                except Exception:
                    pass
            break
        except Exception as e:
            if _attempt < 2:
                time.sleep(0.3)
            else:
                logger.debug(f"run_command: failed to read stderr: {e}")

    if exit_code == -1:
        stderr = "Command timed out" if not stderr else stderr

    return exit_code, stdout, stderr


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
