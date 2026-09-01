import unittest

from core.reconnect_watchdog import evaluate_reconnect


class EvaluateReconnectTests(unittest.TestCase):
    def test_connected_resets_timer(self):
        should, since = evaluate_reconnect('connected', 100.0, 130.0, 20.0)
        self.assertFalse(should)
        self.assertIsNone(since)

    def test_manual_disconnection_never_reconnects(self):
        should, since = evaluate_reconnect('manual_disconnection', None, 10.0, 20.0)
        self.assertFalse(should)
        self.assertIsNone(since)

    def test_registration_required_never_reconnects(self):
        should, since = evaluate_reconnect('registration_required', None, 10.0, 20.0)
        self.assertFalse(should)
        self.assertIsNone(since)

    def test_dns_lookup_failed_never_reconnects(self):
        # 校园网封锁 DoH 是环境性故障，重连无益
        should, since = evaluate_reconnect('dns_lookup_failed', None, 10.0, 20.0)
        self.assertFalse(should)
        self.assertIsNone(since)

    def test_manual_disconnect_marker_suppresses_reconnect(self):
        # 用户主动断开（标记非 0）时，即使状态看起来像意外断开也不重连
        should, since = evaluate_reconnect('cli_error', 100.0, 130.0, 20.0,
                                           manual_disconnect_at=110.0)
        self.assertFalse(should)
        self.assertIsNone(since)

    def test_first_unexpected_disconnect_starts_timer(self):
        should, since = evaluate_reconnect('no_network', None, 10.0, 20.0)
        self.assertFalse(should)
        self.assertEqual(since, 10.0)

    def test_below_threshold_no_reconnect(self):
        should, since = evaluate_reconnect('no_network', 100.0, 115.0, 20.0)
        self.assertFalse(should)
        self.assertEqual(since, 100.0)

    def test_at_threshold_reconnects(self):
        should, since = evaluate_reconnect('no_network', 100.0, 120.0, 20.0)
        self.assertTrue(should)
        self.assertIsNone(since)

    def test_cli_error_reconnects_after_threshold(self):
        should, since = evaluate_reconnect('cli_error', 100.0, 135.0, 20.0)
        self.assertTrue(should)
        self.assertIsNone(since)


if __name__ == '__main__':
    unittest.main()
