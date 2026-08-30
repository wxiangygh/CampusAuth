"""应用自更新：基于 GitHub Releases（成熟稳定的现成方案）。

设计要点
--------
1. **版本检测**：调用 GitHub REST API `/repos/{owner}/{repo}/releases/latest`
   （业界通用做法，无需自建更新服务器；未认证配额 60 次/小时，足够）。
2. **版本比较**：优先 `packaging.version`（PEP 440 语义化版本，Python 生态标准库级依赖），
   缺失时退回数字元组比较，保证任何环境都能正确判断。
3. **下载**：`urllib.request`（标准库，零新增依赖；校园网认证前无网络时快速失败并静默）。
4. **覆盖安装**：生成 `.cmd` 更新脚本并以独立进程分离启动——脚本等主进程退出后
   把新 exe 覆盖到记录的原始安装目录并重启。脚本**只替换 exe 本身**，
   不删除、不触碰同目录的 `tray_config.json` / `warp_exclusion_config.json`，
   因此配置、分流规则、工作流全部保留。

所有失败路径（无网络、超时、限流、无资产、写盘失败）都返回 `available=False`
或抛异常由调用方静默处理，绝不影响应用正常启动与使用。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from core.version import APP_NAME, EXE_NAME, REPO_NAME, REPO_OWNER, __version__

logger = logging.getLogger('wifi_tray')

API_LATEST = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest'
USER_AGENT = f'{REPO_NAME}/{__version__}'
CHECK_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 30.0
DOWNLOAD_CHUNK = 64 * 1024

# 更新脚本等待主进程退出的最长时间（秒）。超时后不再等待，照常尝试安装，
# 避免目标进程因故不退出时脚本永远卡在等待循环里。
WAIT_TRIES = 60
# 更新脚本的运行日志（位于临时目录）。脚本隐藏运行，没有它出问题无从查起。
UPDATER_LOG_NAME = f'{REPO_NAME}_updater.log'
# 自动检测更新失败后的重试间隔（秒）
RETRY_INTERVAL = 60.0


# ---------------------------------------------------------------------------
# 版本号比较
# ---------------------------------------------------------------------------

def version_key(value: Any):
    """把版本号转成可比较对象。

    优先使用 packaging.version（PEP 440），不可用时退回数字元组——
    两种实现只在各自分支内部比较，不会混用。
    """
    text = str(value or '').strip().lstrip('vV')
    try:
        from packaging.version import Version  # type: ignore

        return Version(text)
    except Exception:
        parts = re.findall(r'\d+', text)
        return tuple(int(part) for part in parts[:4]) if parts else (0,)


def is_newer(candidate: str, current: str) -> bool:
    """candidate 是否严格新于 current（比较失败一律视为无更新）。"""
    try:
        return version_key(candidate) > version_key(current)
    except Exception:
        logger.debug('Version comparison failed: %s vs %s', candidate, current)
        return False


# ---------------------------------------------------------------------------
# Release 解析与检测
# ---------------------------------------------------------------------------

def parse_release(payload: dict[str, Any]) -> dict[str, Any] | None:
    """把 GitHub release JSON 收敛成前端需要的结构；无 exe 资产返回 None。"""
    if not isinstance(payload, dict):
        return None
    assets = payload.get('assets') or []
    exe = next((a for a in assets
                if str(a.get('name', '')).lower().endswith('.exe')), None)
    if not exe:
        return None
    tag = str(payload.get('tag_name') or '')
    return {
        'version': tag.lstrip('vV') or str(payload.get('name') or ''),
        'tag': tag,
        'name': str(payload.get('name') or tag),
        'notes': str(payload.get('body') or ''),
        'published_at': str(payload.get('published_at') or ''),
        'html_url': str(payload.get('html_url') or ''),
        'download_url': str(exe.get('browser_download_url') or ''),
        'size': int(exe.get('size') or 0),
    }


def fetch_latest_release(timeout: float = CHECK_TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(
        API_LATEST,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def check_for_update(current_version: str | None = None) -> dict[str, Any]:
    """检查是否有新版本。

    返回 `{'available': bool, 'current': str, 'latest': dict|None, 'reason': str}`。
    任何失败都返回 `available=False`，调用方静默处理。
    """
    current = str(current_version or __version__)
    try:
        release = parse_release(fetch_latest_release())
    except urllib.error.URLError as exc:
        logger.info('[updater] 更新检测失败（网络不可达）: %s', exc)
        return {'available': False, 'current': current, 'latest': None, 'reason': 'network'}
    except (TimeoutError, OSError) as exc:
        logger.info('[updater] 更新检测超时: %s', exc)
        return {'available': False, 'current': current, 'latest': None, 'reason': 'timeout'}
    except Exception as exc:  # 限流 / JSON 解析 / 未知响应
        logger.info('[updater] 更新检测异常: %s', exc)
        return {'available': False, 'current': current, 'latest': None, 'reason': 'error'}

    if not release or not release.get('download_url'):
        return {'available': False, 'current': current, 'latest': release, 'reason': 'no_asset'}
    available = is_newer(release.get('version', ''), current)
    logger.info('[updater] latest=%s current=%s available=%s',
                release.get('version'), current, available)
    return {'available': available, 'current': current, 'latest': release,
            'reason': 'ok' if available else 'up_to_date'}


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

class UpdateDownloader:
    """下载新版本 exe 到临时目录，并提供可轮询的进度快照。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {'status': 'idle', 'pct': 0,
                                      'message': '', 'error': '', 'file': ''}

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._state['status'] in {'downloading', 'installing'}

    def start(self, url: str, expected_size: int = 0,
              on_done: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if self.busy:
            return self.progress()
        if not url:
            self._set(status='error', message='下载地址无效', error='no_url')
            return self.progress()
        self._set(status='downloading', pct=0, message='正在下载更新…', error='', file='')
        self._thread = threading.Thread(
            target=self._run, args=(url, expected_size, on_done),
            name='update-download', daemon=True)
        self._thread.start()
        return self.progress()

    def _run(self, url: str, expected_size: int,
             on_done: Callable[[dict[str, Any]], None] | None) -> None:
        result: dict[str, Any]
        target = Path(tempfile.gettempdir()) / f'{REPO_NAME}_update_{int(time.time())}.exe'
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                total = int(response.headers.get('Content-Length') or expected_size or 0)
                received = 0
                last_pct = -1
                with open(target, 'wb') as stream:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        stream.write(chunk)
                        received += len(chunk)
                        if total:
                            pct = min(99, int(received * 100 / total))
                            if pct != last_pct:
                                last_pct = pct
                                self._set(pct=pct,
                                          message=f'正在下载更新… {pct}%')
            if received <= 1024 * 1024:  # 小于 1MB 基本可判定为下载失败/错误页
                raise OSError(f'download too small: {received} bytes')
            self._set(status='downloaded', pct=100, message='下载完成', file=str(target))
            result = {'success': True, 'status': 'downloaded', 'file': str(target)}
        except Exception as exc:
            logger.warning('[updater] 下载失败: %s', exc)
            self._set(status='error', pct=0, message=f'下载失败：{exc}',
                      error=str(exc), file='')
            result = {'success': False, 'status': 'error', 'error': str(exc)}
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if on_done:
            try:
                on_done(result)
            except Exception:
                logger.exception('[updater] update completion callback failed')


# ---------------------------------------------------------------------------
# 覆盖安装
# ---------------------------------------------------------------------------

def _ps_quote(text: str) -> str:
    """包成 PowerShell 单引号字面量：内部的单引号要写成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def _installer_script(new_exe: Path, install_dir: Path, pid: int,
                      restart: bool, version: str = '') -> Path:
    """生成 Windows 风格的图形安装器（PowerShell + WinForms）。

    为什么不再用 .cmd 脚本
    ----------------------
    用 `DETACHED_PROCESS` 启动脱离子进程是常见做法，但实测它会让子进程脱离控制台
    会话，进而导致：`tasklist` 查不到目标进程、管道命令（`| find`）直接挂死。
    表现就是"更新检测成功，但装不上，还卡着一个黑框"。cmd 也没有原生 GUI，
    要么黑框、要么完全隐藏，体验都不对。

    改用 PowerShell + WinForms：原生 Windows 外观、进度可见，且用 `Get-Process`
    判断进程存活比 `tasklist | find` 可靠得多。

    两个必须注意的点
    ----------------
    - ps1 必须以 UTF-8 **带 BOM** 写入，否则 Windows PowerShell 5.1 会按 ANSI(GBK)
      读取，中文乱码后连字符串引号一起破坏，报"字符串缺少终止符"。
    - 参数直接写进脚本而不是走命令行传参，避免命令行上的中文编码问题。
    - 等待有上限（WAIT_TRIES 秒），目标进程因故不退出时不会无限等下去。
    """
    script = Path(tempfile.gettempdir()) / f'{REPO_NAME}_installer.ps1'
    log = Path(tempfile.gettempdir()) / UPDATER_LOG_NAME
    destination = install_dir / EXE_NAME
    title = f'{APP_NAME} 更新'
    heading = f'正在更新 {APP_NAME}'
    if version:
        heading = f'正在更新 {APP_NAME} 到 {version}'
    hint = f'仅替换程序文件，{APP_NAME} 的账号、WiFi 与分流规则配置都会保留。'
    # PowerShell 里不能写 `Write-Log 'a' + $b`（会被当成三个参数），
    # 表达式必须先拼好再传，所以下面统一先算变量再调用。
    restart_literal = '$true' if restart else '$false'

    # 注意：下面刻意避开 PowerShell 的 -f 格式化和哈希表，
    # 以免出现 { } 与 Python f-string 的花括号冲突。
    lines = [
        'Add-Type -AssemblyName System.Windows.Forms',
        'Add-Type -AssemblyName System.Drawing',
        '',
        f'$logPath = {_ps_quote(log)}',
        f'$targetPid = {int(pid)}',
        f'$src = {_ps_quote(new_exe)}',
        f'$dst = {_ps_quote(destination)}',
        f'$appDir = {_ps_quote(install_dir)}',
        f'$shouldRestart = {restart_literal}',
        '',
        'function Write-Log($msg) {',
        '    try {',
        '        $stamp = Get-Date -Format "HH:mm:ss"',
        '        Add-Content -LiteralPath $logPath -Value ($stamp + " " + $msg) -Encoding UTF8',
        '    } catch { }',
        '}',
        '',
        "$msg = 'installer started, waiting for pid ' + $targetPid",
        'Write-Log $msg',
        '',
        '$form = New-Object System.Windows.Forms.Form',
        f'$form.Text = {_ps_quote(title)}',
        '$form.Size = New-Object System.Drawing.Size(460, 176)',
        "$form.StartPosition = 'CenterScreen'",
        '$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog',
        '$form.MaximizeBox = $false',
        '$form.MinimizeBox = $false',
        '$form.ShowInTaskbar = $true',
        '$form.TopMost = $true',
        '$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)',
        '',
        '$label = New-Object System.Windows.Forms.Label',
        '$label.Location = New-Object System.Drawing.Point(20, 20)',
        '$label.Size = New-Object System.Drawing.Size(404, 24)',
        "$label.Text = '准备更新…'",
        '$form.Controls.Add($label)',
        '',
        '$bar = New-Object System.Windows.Forms.ProgressBar',
        '$bar.Location = New-Object System.Drawing.Point(20, 52)',
        '$bar.Size = New-Object System.Drawing.Size(404, 24)',
        '$bar.Minimum = 0',
        '$bar.Maximum = 100',
        '$bar.Value = 0',
        '$form.Controls.Add($bar)',
        '',
        '$tip = New-Object System.Windows.Forms.Label',
        '$tip.Location = New-Object System.Drawing.Point(20, 90)',
        '$tip.Size = New-Object System.Drawing.Size(404, 36)',
        '$tip.ForeColor = [System.Drawing.Color]::Gray',
        f'$tip.Text = {_ps_quote(hint)}',
        '$form.Controls.Add($tip)',
        '',
        'function Set-Step($text, $pct) {',
        '    $label.Text = $text',
        '    $bar.Value = $pct',
        '    $form.Refresh()',
        '    [System.Windows.Forms.Application]::DoEvents()',
        '}',
        '',
        '$form.Show()',
        '$form.Refresh()',
        '',
        '# ---- 1. 等待主进程退出 ----',
        "Set-Step '正在等待程序退出…' 10",
        f'$deadline = (Get-Date).AddSeconds({int(WAIT_TRIES)})',
        'while ($true) {',
        '    $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue',
        '    if (-not $proc) { break }',
        '    if ((Get-Date) -gt $deadline) { Write-Log "wait timeout"; break }',
        '    Start-Sleep -Milliseconds 300',
        '    [System.Windows.Forms.Application]::DoEvents()',
        '}',
        'Start-Sleep -Milliseconds 500',
        '',
        '# ---- 2. 覆盖安装（只替换 exe）----',
        f"Set-Step {_ps_quote(heading)} 45",
        '$installed = $false',
        'try {',
        '    Copy-Item -LiteralPath $src -Destination $dst -Force',
        '    $installed = Test-Path -LiteralPath $dst',
        '} catch {',
        '    Write-Log ("copy failed: " + $_.Exception.Message)',
        '}',
        'Start-Sleep -Milliseconds 400',
        '',
        'if (-not $installed) {',
        "    Write-Log 'INSTALL FAILED'",
        f'    $errTitle = {_ps_quote(title)}',
        f'    $errLine1 = {_ps_quote("更新安装失败，程序未做任何改动。")}',
        f'    $errLine2 = {_ps_quote("请稍后重试，或到 GitHub Releases 手动下载覆盖安装。")}',
        '    $errText = $errLine1 + [Environment]::NewLine + [Environment]::NewLine + $errLine2',
        '    [System.Windows.Forms.MessageBox]::Show($errText, $errTitle, '
        '[System.Windows.Forms.MessageBoxButtons]::OK, '
        '[System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null',
        '    $form.Close()',
        '    exit 1',
        '}',
        "Write-Log 'installed'",
        "Set-Step '安装完成' 75",
        '',
        '# ---- 3. 重启 ----',
        'if ($shouldRestart) {',
        "    Set-Step '正在启动新版本…' 90",
        '    Start-Sleep -Milliseconds 400',
        '    try {',
        '        Set-Location -LiteralPath $appDir',
        '        Start-Process -FilePath $dst',
        "        Write-Log 'restarted'",
        '    } catch {',
        '        Write-Log ("restart failed: " + $_.Exception.Message)',
        '    }',
        '}',
        "Set-Step '更新完成' 100",
        'Start-Sleep -Milliseconds 700',
        '$form.Close()',
        '',
        'Remove-Item -LiteralPath $src -Force -ErrorAction SilentlyContinue',
        "Write-Log 'done'",
        'Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue',
        'exit 0',
    ]
    # UTF-8 带 BOM：不加 BOM，PowerShell 5.1 会按 GBK 读取中文并破坏语法
    script.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    return script


def install_update(new_exe: str | Path, install_dir: str | Path,
                   restart: bool = True, version: str = '') -> dict[str, Any]:
    """启动图形安装器完成覆盖安装。

    - 只替换 exe，绝不动同目录的配置文件（tray_config.json / warp_exclusion_config.json）
    - 安装器独立运行，等当前进程退出后再复制，避免文件占用
    - 返回 {'success': bool, 'message': str}
    """
    source = Path(new_exe)
    directory = Path(install_dir)
    try:
        if not source.exists():
            return {'success': False, 'message': '更新包不存在'}
        directory.mkdir(parents=True, exist_ok=True)
        # 预检：目标存在时确认可写（提前暴露权限问题，避免退出后无法安装）
        destination = directory / EXE_NAME
        if destination.exists() and not os.access(destination, os.W_OK):
            return {'success': False, 'message': f'没有写入权限：{destination}'}
        script = _installer_script(source, directory, os.getpid(), restart, version)
        creation_flags = 0
        if os.name == 'nt':
            # 只给 CREATE_NEW_PROCESS_GROUP：保证主进程退出后安装器继续运行。
            # 千万别加 DETACHED_PROCESS——实测它会让子进程脱离控制台会话，
            # 结果是 tasklist 查不到目标进程、管道命令直接挂死，更新装不上。
            creation_flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        subprocess.Popen(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA',
             '-WindowStyle', 'Hidden', '-File', str(script)],
            creationflags=creation_flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info('[updater] 安装器已启动: %s -> %s', source, destination)
        logger.info('[updater] 安装器脚本: %s；运行日志: %s',
                    script, Path(tempfile.gettempdir()) / UPDATER_LOG_NAME)
        return {'success': True, 'message': '安装程序已启动', 'target': str(destination)}
    except Exception as exc:
        logger.exception('[updater] 启动安装器失败')
        return {'success': False, 'message': str(exc)}


def validate_install_dir(candidate: str | Path) -> tuple[bool, str]:
    """校验一个目录是否可作为更新安装目录。

    返回 (是否可用, 说明)。要求：非空、是目录（不存在时尝试创建）、可写。
    """
    text = str(candidate or '').strip()
    if not text:
        return False, '未选择目录'
    path = Path(text)
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False, '选择的路径不是目录'
        if not os.access(path, os.W_OK):
            return False, f'没有写入权限：{path}'
    except PermissionError:
        return False, f'没有写入权限：{path}'
    except OSError as exc:
        return False, f'目录不可用：{exc}'
    return True, str(path)


def resolve_install_dir(recorded: str | None = None) -> Path:
    """确定安装目录：优先使用记录的首次安装位置，失效时回退到当前 exe 所在目录。"""
    if recorded:
        try:
            candidate = Path(recorded)
            if candidate.is_dir():
                return candidate
        except (OSError, ValueError):
            pass
    if getattr(__import__('sys'), 'frozen', False):
        return Path(__import__('sys').executable).parent
    # 开发环境：源码根目录
    return Path(__file__).resolve().parents[1]


def cleanup_temp_files() -> None:
    """清理遗留的临时更新包（避免长期占用磁盘）。"""
    try:
        temp_dir = Path(tempfile.gettempdir())
        for pattern in (f'{REPO_NAME}_update_*.exe', f'{REPO_NAME}_updater.cmd'):
            for path in temp_dir.glob(pattern):
                try:
                    if path.name.endswith('.cmd') and time.time() - path.stat().st_mtime < 600:
                        continue  # 更新脚本可能正在运行
                    path.unlink()
                except OSError:
                    continue
    except Exception:
        logger.debug('[updater] temp cleanup skipped', exc_info=True)
