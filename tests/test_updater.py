import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

from core import updater
from core.updater import (check_for_update, is_newer, parse_release,
                          resolve_install_dir, validate_install_dir, version_key,
                          _installer_script)
from core.version import __version__


def _release(tag='v9.9.9', asset_name='CampusAuth.exe'):
    return {
        'tag_name': tag,
        'name': f'CampusAuth {tag}',
        'body': '更新内容',
        'assets': [{'name': asset_name, 'size': 1024,
                    'browser_download_url': 'https://example.com/CampusAuth.exe'}],
    }


class VersionCompareTests(unittest.TestCase):
    def test_newer_versions_are_detected(self):
        self.assertTrue(is_newer('1.1.0', '1.0.0'))
        self.assertTrue(is_newer('1.0.1', '1.0.0'))
        self.assertTrue(is_newer('2.0.0', '1.9.9'))
        self.assertTrue(is_newer('v1.2.0', '1.1.9'))

    def test_semver_ordering_is_not_lexicographic(self):
        # 字符串比较会得出 1.9.0 > 1.10.0，语义化比较必须相反
        self.assertTrue(is_newer('1.10.0', '1.9.0'))
        self.assertFalse(is_newer('1.9.0', '1.10.0'))

    def test_same_or_older_version_is_not_newer(self):
        self.assertFalse(is_newer('1.0.0', '1.0.0'))
        self.assertFalse(is_newer('0.9.0', '1.0.0'))

    def test_malformed_versions_never_raise(self):
        # 非法/空版本号一律视为"无更新"，且不得抛异常
        for bad in ('', 'v', 'abc', None, 'latest', 'v.v.v'):
            self.assertFalse(is_newer(bad, __version__))
            self.assertIsNotNone(version_key(bad))

    def test_extra_long_version_segments_still_compare(self):
        # packaging 支持多段 release，1.2.3.4.5.6 合法且大于 1.1.0
        self.assertTrue(is_newer('1.2.3.4.5.6', '1.1.0'))


class ReleaseParsingTests(unittest.TestCase):
    def test_picks_windows_exe_asset(self):
        parsed = parse_release(_release())
        self.assertEqual(parsed['version'], '9.9.9')
        self.assertEqual(parsed['download_url'], 'https://example.com/CampusAuth.exe')
        self.assertEqual(parsed['notes'], '更新内容')

    def test_release_without_exe_asset_is_ignored(self):
        self.assertIsNone(parse_release(_release(asset_name='notes.txt')))
        self.assertIsNone(parse_release({'tag_name': 'v1.0.0', 'assets': []}))
        self.assertIsNone(parse_release({}))


class CheckForUpdateTests(unittest.TestCase):
    def test_network_failure_is_silent(self):
        original = updater.fetch_latest_release

        def boom(*args, **kwargs):
            raise urllib.error.URLError('offline')

        updater.fetch_latest_release = boom
        try:
            result = check_for_update('1.0.0')
        finally:
            updater.fetch_latest_release = original
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'network')

    def test_timeout_is_silent(self):
        original = updater.fetch_latest_release

        def slow(*args, **kwargs):
            raise TimeoutError('timeout')

        updater.fetch_latest_release = slow
        try:
            result = check_for_update('1.0.0')
        finally:
            updater.fetch_latest_release = original
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'timeout')

    def test_up_to_date_is_not_available(self):
        original = updater.fetch_latest_release
        updater.fetch_latest_release = lambda *a, **k: _release(tag=f'v{__version__}')
        try:
            result = check_for_update()
        finally:
            updater.fetch_latest_release = original
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'up_to_date')

    def test_newer_release_is_reported_with_notes(self):
        original = updater.fetch_latest_release
        updater.fetch_latest_release = lambda *a, **k: _release(tag='v99.0.0')
        try:
            result = check_for_update('1.0.0')
        finally:
            updater.fetch_latest_release = original
        self.assertTrue(result['available'])
        self.assertEqual(result['latest']['version'], '99.0.0')
        self.assertEqual(result['latest']['notes'], '更新内容')


class InstallDirTests(unittest.TestCase):
    def test_recorded_dir_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_install_dir(tmp), Path(tmp))

    def test_invalid_record_falls_back(self):
        resolved = resolve_install_dir('/definitely/not/a/real/path')
        self.assertTrue(resolved.is_dir())


class ValidateInstallDirTests(unittest.TestCase):
    def test_empty_choice_is_rejected(self):
        for bad in ('', '   ', None):
            ok, message = validate_install_dir(bad)
            self.assertFalse(ok)
            self.assertTrue(message)

    def test_existing_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = validate_install_dir(tmp)
            self.assertTrue(ok, message)
            self.assertEqual(Path(message), Path(tmp))

    def test_missing_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'nested' / 'install'
            ok, message = validate_install_dir(target)
            self.assertTrue(ok, message)
            self.assertTrue(target.is_dir())

    def test_file_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'not-a-dir.txt'
            target.write_text('x', encoding='utf-8')
            ok, _ = validate_install_dir(target)
            self.assertFalse(ok)

    def test_malformed_path_never_raises(self):
        # 非法字符/保留名/超长路径等输入必须被捕获并给出说明，不能抛未处理异常
        for bad in ('C:\\a<>|b', '\\\\?\\Bogus\\<bad>', 'C:\\CON', '   ', 'x' * 500):
            ok, message = validate_install_dir(bad)
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(message, str)


class InstallerScriptTests(unittest.TestCase):
    """图形安装器（PowerShell + WinForms）的生成逻辑。

    守住四条产品约定：
    1. 更新后必须重启，且以非静默方式启动（会弹出主窗口）
    2. 只替换 exe，脚本里绝不出现配置文件名
    3. 等待主进程退出必须有上限，不能无限等
    4. 生成的 ps1 语法必须有效（用 PowerShell 自带解析器校验）
    """

    def _script(self, restart, keep=False):
        tmp_dir = Path(tempfile.mkdtemp())
        new_exe = tmp_dir / 'CampusAuth_update_123.exe'
        new_exe.write_bytes(b'x')
        install_dir = tmp_dir / 'install'
        install_dir.mkdir()
        destination = install_dir / 'CampusAuth.exe'
        script = _installer_script(new_exe, install_dir, 4321, restart, '1.2.1')
        if not keep:
            self.addCleanup(lambda: script.unlink(missing_ok=True))
        return script, script.read_text(encoding='utf-8-sig'), destination, install_dir

    def test_is_a_powershell_script(self):
        script, content, _, _ = self._script(restart=True)
        self.assertEqual(script.suffix, '.ps1')
        self.assertIn('Add-Type -AssemblyName System.Windows.Forms', content)

    def test_paths_are_embedded(self):
        # 参数写进脚本而不是走命令行，避免命令行上的中文编码问题
        _, content, destination, _ = self._script(restart=True)
        self.assertIn(str(destination), content)

    def test_waits_for_main_process(self):
        _, content, _, _ = self._script(restart=True)
        self.assertIn('$targetPid = 4321', content)
        self.assertIn('Get-Process -Id $targetPid', content)

    def test_wait_is_bounded(self):
        # 目标进程因故不退出时，安装器不能无限等下去
        _, content, _, _ = self._script(restart=True)
        self.assertIn(f'AddSeconds({updater.WAIT_TRIES})', content)

    def test_restart_enabled(self):
        _, content, _, _ = self._script(restart=True)
        self.assertIn('$shouldRestart = $true', content)
        self.assertIn('Start-Process -FilePath $dst', content)

    def test_restart_disabled(self):
        _, content, _, _ = self._script(restart=False)
        self.assertIn('$shouldRestart = $false', content)

    def test_restart_is_never_silent(self):
        # 更新后的重启必须像普通启动一样弹出主窗口，不能沿用静默启动
        _, content, _, _ = self._script(restart=True)
        self.assertNotIn('--silent', content)

    def test_restarts_from_install_dir(self):
        # 安装器脚本躺在 %TEMP% 下，不切目录的话新进程会把临时目录当成工作目录
        _, content, _, install_dir = self._script(restart=True)
        self.assertIn(str(install_dir), content)
        self.assertIn('Set-Location -LiteralPath $appDir', content)

    def test_script_only_replaces_the_exe(self):
        _, content, _, _ = self._script(restart=True)
        self.assertIn('Copy-Item -LiteralPath $src -Destination $dst -Force', content)
        for config_name in ('tray_config.json', 'warp_exclusion_config.json'):
            self.assertNotIn(config_name, content)

    def test_script_writes_a_log(self):
        _, content, _, _ = self._script(restart=True)
        self.assertIn(updater.UPDATER_LOG_NAME, content)

    def test_script_is_valid_powershell(self):
        # 手写拼出来的 ps1 很容易出语法错，用 PowerShell 自带解析器兜底
        if os.name != 'nt':
            self.skipTest('仅 Windows 需要校验 PowerShell 语法')
        script, _, _, _ = self._script(restart=True, keep=True)
        probe = (
            '$tokens = $null; $errors = $null; '
            '[System.Management.Automation.Language.Parser]::ParseFile('
            f"'{script}', [ref]$tokens, [ref]$errors) | Out-Null; "
            'if ($errors.Count -gt 0) '
            '{ $errors | ForEach-Object { Write-Output $_.Message }; exit 1 } '
            'else { exit 0 }'
        )
        try:
            done = subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-Command', probe],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f'无法调用 PowerShell 进行语法校验: {exc}')
        self.assertEqual(done.returncode, 0,
                         f'生成的安装器脚本存在 PowerShell 语法错误：\n{done.stdout}')


if __name__ == '__main__':
    unittest.main()
