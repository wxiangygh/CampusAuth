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

from core.version import EXE_NAME, REPO_NAME, REPO_OWNER, __version__

logger = logging.getLogger('wifi_tray')

API_LATEST = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest'
USER_AGENT = f'{REPO_NAME}/{__version__}'
CHECK_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 30.0
DOWNLOAD_CHUNK = 64 * 1024


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

def _updater_script(new_exe: Path, install_dir: Path, pid: int, restart: bool) -> Path:
    """生成等待主进程退出后覆盖安装并（可选）重启的 cmd 脚本。"""
    script = Path(tempfile.gettempdir()) / f'{REPO_NAME}_updater.cmd'
    destination = install_dir / EXE_NAME
    lines = [
        '@echo off',
        'chcp 65001 >nul',
        'setlocal',
        f'set "SRC={new_exe}"',
        f'set "DST={destination}"',
        f'set "APPDIR={install_dir}"',
        f'set "PID={pid}"',
        '',
        ':wait',
        'tasklist /FI "PID eq %PID%" /NH 2>nul | find "%PID%" >nul',
        'if errorlevel 1 goto install',
        'timeout /t 1 /nobreak >nul',
        'goto wait',
        '',
        ':install',
        'timeout /t 1 /nobreak >nul',
        'if not exist "%SRC%" goto cleanup',
        'move /Y "%SRC%" "%DST%" >nul',
        'if errorlevel 1 copy /Y "%SRC%" "%DST%" >nul',
    ]
    if restart:
        # 先切到 exe 所在目录再启动：脚本本身躺在 %TEMP% 下，直接 start 会让新进程
        # 把临时目录当成工作目录。
        # 刻意不带 --silent：静默启动只影响开机自启那一次，更新后的重启应当像普通
        # 启动一样弹出主窗口，让用户能立刻看到更新结果。
        lines += [
            'cd /d "%APPDIR%"',
            'start "" "%DST%"',
        ]
    lines += [
        ':cleanup',
        'if exist "%SRC%" del /F /Q "%SRC%" >nul',
        'del /F /Q "%~f0" >nul & exit /b 0',
    ]
    script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')
    return script


def install_update(new_exe: str | Path, install_dir: str | Path,
                   restart: bool = True) -> dict[str, Any]:
    """启动独立更新进程完成覆盖安装。

    - 只替换 exe，绝不动同目录的配置文件（tray_config.json / warp_exclusion_config.json）
    - 更新脚本等待当前进程结束后才执行 move，避免文件占用
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
        script = _updater_script(source, directory, os.getpid(), restart)
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = (subprocess.DETACHED_PROCESS
                              | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        subprocess.Popen(
            ['cmd', '/c', str(script)],
            creationflags=creation_flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info('[updater] 更新脚本已启动: %s -> %s', source, destination)
        return {'success': True, 'message': '安装程序已启动', 'target': str(destination)}
    except Exception as exc:
        logger.exception('[updater] 启动更新程序失败')
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
