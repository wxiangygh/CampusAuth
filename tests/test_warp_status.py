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

    def test_dns_lookup_failed_is_specific_and_not_retryable(self):
        # 复现 2026-09-01 故障：校园网封锁 Cloudflare DoH 服务器（1.1.1.1、
        # 162.159.36.x）的 TCP 443 后，WARP 连通性检查报
        # CF_DNS_LOOKUP_FAILURE。应给出精确诊断而非笼统的 cli_error。
        text = ('Status update: Unable\n'
                'Reason: Connectivity check failed due to DNS Lookup Failed')
        with patch('core.warp_manager.run_command', return_value=(0, text, '')):
            result = probe_warp_status('warp-cli')
        self.assertEqual(result.code, 'dns_lookup_failed')
        self.assertFalse(result.retryable)
        self.assertIn('DoH', result.message)


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

    def test_dns_lookup_failure_returns_promptly_after_connect(self):
        # 校园网封锁 DoH 导致的 CF_DNS_LOOKUP_FAILURE 是环境性故障，
        # connect 发出后若状态仍是 DNS Lookup Failed 应立即返回明确错误，
        # 不应把整个超时预算耗在注定失败的重试上。
        commands = []
        dns_text = ('Status update: Unable\n'
                    'Reason: Connectivity check failed due to DNS Lookup Failed')
        fake = self._scripted_run_command([dns_text, dns_text], commands)
        with patch('core.warp_manager.get_warp_cli', return_value='warp-cli'), \
             patch('core.warp_manager.run_command', side_effect=fake):
            result = connect_warp_result(timeout=30, max_attempts=1)
        self.assertFalse(result.success)
        self.assertEqual(result.code, 'dns_lookup_failed')
        # connect 命令确实发出过（初始探测的失败不阻止发起连接）
        self.assertIn('warp-cli connect', commands)


if __name__ == '__main__':
    unittest.main()
