import unittest
from unittest.mock import patch

from core.warp_manager import connect_warp_result, probe_warp_status


class WarpStatusTests(unittest.TestCase):
    def test_connected_status(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Status update: Connected', '')):
            result = probe_warp_status('warp-cli')
        self.assertTrue(result.success)
        self.assertEqual(result.code, 'connected')

    def test_registration_error_is_actionable_and_not_retryable(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Registration Missing', '')):
            result = probe_warp_status('warp-cli')
        self.assertEqual(result.code, 'registration_required')
        self.assertFalse(result.retryable)
        self.assertIn('注册', result.message)

    def test_no_network_is_retryable(self):
        with patch('core.warp_manager.run_command', return_value=(0, 'Unable: No Network', '')):
            result = probe_warp_status('warp-cli')
        self.assertEqual(result.code, 'no_network')
        self.assertTrue(result.retryable)


class ConnectWarpTests(unittest.TestCase):
    def _scripted_run_command(self, statuses, commands):
        """构造 run_command 替身：按序返回 status 探测结果，记录其余命令。"""
        iterator = iter(statuses)

        def fake(command, timeout=None, shell=True):
            text = ' '.join(command) if isinstance(command, (list, tuple)) else str(command)
            commands.append(text)
            if text.endswith(' status'):
                try:
                    return 0, next(iterator), ''
                except StopIteration:
                    return 0, 'Status update: Connected', ''
            if 'sc query' in text:
                return 0, 'STATE : 4  RUNNING', ''
            return 0, '', ''

        return fake

    def test_connect_succeeds_when_manual_disconnection_lags_after_connect(self):
        # 复现 2026-09-01 日志：服务刚启动时，connect 已发出但 warp-cli status
        # 仍短暂停留在 "Manual disconnection"。此时不应提前放弃，
        # 应补发连接命令并继续等待，直到连接真正建立。
        commands = []
        fake = self._scripted_run_command(
            ['Manual disconnection', 'Manual disconnection', 'Status update: Connected'],
            commands)
        with patch('core.warp_manager.get_warp_cli', return_value='warp-cli'), \
             patch('core.warp_manager.run_command', side_effect=fake):
            result = connect_warp_result(timeout=10, max_attempts=1)
        self.assertTrue(result.success, result.message)
        self.assertGreaterEqual(commands.count('warp-cli connect'), 2)

    def test_manual_disconnection_still_fails_if_deadline_exceeded(self):
        # 若直到截止仍未连接，应返回失败而不是无限等待
        commands = []
        fake = self._scripted_run_command(['Manual disconnection'] * 50, commands)
        with patch('core.warp_manager.get_warp_cli', return_value='warp-cli'), \
             patch('core.warp_manager.run_command', side_effect=fake):
            result = connect_warp_result(timeout=2, max_attempts=1)
        self.assertFalse(result.success)


if __name__ == '__main__':
    unittest.main()
