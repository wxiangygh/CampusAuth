import tempfile
import unittest
import urllib.error
from pathlib import Path

from core import updater
from core.updater import (check_for_update, is_newer, parse_release,
                          resolve_install_dir, version_key)
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


if __name__ == '__main__':
    unittest.main()
