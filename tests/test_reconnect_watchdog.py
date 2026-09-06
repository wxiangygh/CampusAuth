import time
import unittest
from unittest import mock

from core.reconnect_watchdog import ReconnectWatchdog, evaluate_reconnect


class TickRunsBoundWorkflowTests(unittest.TestCase):
    """_tick：重连改为运行绑定的 reconnect_workflow 并开启操作纪元。"""

    def _make_watchdog(self):
        wd = ReconnectWatchdog()
        # 已断开超过阈值，本 tick 应触发重连
        wd._disconnected_since = time.monotonic() - 100
        return wd

    def _patch_env(self, wd, wf_id='default_auth', workflow_result=(True, 'ok')):
        cfg = {'warp_auto_reconnect': True, 'warp_reconnect_delay': 20,
               'reconnect_workflow': wf_id}
        status = mock.Mock(code='no_network')
        run_wf = mock.MagicMock(return_value=workflow_result)
        app_state = mock.MagicMock()
        patches = [
            mock.patch('core.reconnect_watchdog.get_config', return_value=cfg),
            mock.patch('core.reconnect_watchdog.probe_warp_status', return_value=status),
            mock.patch('core.reconnect_watchdog.app_state', app_state),
            mock.patch('core.auth_workflow.run_workflow_by_id', run_wf),
            mock.patch.object(ReconnectWatchdog, '_refresh_status'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return run_wf, app_state

    def test_runs_reconnect_workflow_and_starts_epoch(self):
        wd = self._make_watchdog()
        run_wf, app_state = self._patch_env(wd, wf_id='my_flow')
        wd._tick()
        run_wf.assert_called_once_with('my_flow')
        # 开启新纪元让前端显示本次重连的进度
        app_state.start_operation.assert_called_once_with('auth')
        # 触发成功后清零计时
        self.assertIsNone(wd._disconnected_since)

    def test_failed_workflow_restarts_timer(self):
        wd = self._make_watchdog()
        self._patch_env(wd, workflow_result=(False, 'portal failed'))
        wd._tick()
        self.assertIsNotNone(wd._disconnected_since)

    def test_busy_lock_skips_without_running_workflow(self):
        import core.state
        wd = self._make_watchdog()
        run_wf, _ = self._patch_env(wd)
        with core.state._auth_lock:
            wd._tick()
        run_wf.assert_not_called()
        # 忙碌跳过后重新计时
        self.assertIsNotNone(wd._disconnected_since)


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


class HiddenIntervalTests(unittest.TestCase):
    """托盘隐藏时的看门狗降频：hidden_check_interval 不低于 15s。"""

    def test_default_hidden_interval(self):
        self.assertGreaterEqual(ReconnectWatchdog().hidden_check_interval, 15.0)

    def test_custom_interval_not_shortened(self):
        self.assertEqual(ReconnectWatchdog(check_interval=20).hidden_check_interval, 20.0)
